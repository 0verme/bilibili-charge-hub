import asyncio
import logging
from asyncio import CancelledError
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.bilibili.client import BilibiliClient
from app.models import (
    AccountStatus,
    BiliAccount,
    JobKind,
    JobRun,
    NotificationOutbox,
    QrLoginSession,
    RunStatus,
    ScheduleJob,
    UserSession,
)
from app.notifications.service import NotificationDeliveryService, enqueue_event
from app.services.collection import ChargeCollectionService
from app.services.coupon import CouponClaimService
from app.services.daily_task import DailyTaskService
from app.services.reconciliation import NotificationReconciliationService
from app.settings import get_settings

logger = logging.getLogger(__name__)


class SchedulerManager:
    """Rebuilds runtime jobs from durable configuration on every process start.

    APScheduler's max_instances protects one process. The dispatch boundary is intentionally
    centralized so a PostgreSQL advisory-lock provider can guard multiple replicas later.
    """

    def __init__(self, factory: sessionmaker[Session], timezone: str) -> None:
        self.factory = factory
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        self.scheduler.start()
        self.scheduler.add_job(
            self.cleanup_expired,
            trigger=IntervalTrigger(hours=24),
            id="system-maintenance",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.run_notification_reconciliation,
            trigger=IntervalTrigger(minutes=60),
            id="notification-reconciliation",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        with self.factory() as db:
            self.recover_interrupted_runs(db)
            for job in db.scalars(select(ScheduleJob).where(ScheduleJob.enabled.is_(True))):
                self.sync_job(job, db)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def shutdown_gracefully(self, timeout_seconds: float = 10.0) -> int:
        """Pause scheduling, wait briefly for dispatches, then cancel unfinished work.

        The return value is the number of dispatch tasks that had to be cancelled. Lifespan
        owners can await this method before process shutdown without waiting indefinitely.
        """
        if self.scheduler.running:
            self.scheduler.pause()
        cancelled = await self.wait_for_dispatches(timeout_seconds)
        self.shutdown()
        return cancelled

    async def wait_for_dispatches(self, timeout_seconds: float = 10.0) -> int:
        """Wait up to ``timeout_seconds`` and cancel dispatches still running afterwards."""
        timeout_seconds = max(0.0, timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while self._dispatch_tasks:
            active = {task for task in self._dispatch_tasks if not task.done()}
            if not active:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            _, pending = await asyncio.wait(active, timeout=remaining)
            if pending:
                break
        pending = {task for task in self._dispatch_tasks if not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return len(pending)

    def submit_dispatch(self, job_id: str, run_id: str | None = None) -> asyncio.Task[None]:
        """Start a manually requested dispatch and include it in graceful shutdown tracking."""
        task = asyncio.create_task(self.dispatch(job_id, run_id))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)
        return task

    @staticmethod
    def recover_interrupted_runs(db: Session) -> int:
        """Fail non-terminal runs left behind by an earlier process immediately on startup."""
        now = datetime.now(UTC)
        runs = list(
            db.scalars(
                select(JobRun).where(JobRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            )
        )
        for run in runs:
            run.status = RunStatus.FAILED
            run.error = "run interrupted by process restart"
            run.finished_at = now
            started_at = run.started_at.replace(tzinfo=run.started_at.tzinfo or UTC)
            run.duration_ms = max(0, int((now - started_at).total_seconds() * 1000))
        if runs:
            db.commit()
        return len(runs)

    def sync_job(self, job: ScheduleJob, db: Session | None = None) -> None:
        if not job.enabled:
            self.scheduler.remove_job(job.id) if self.scheduler.get_job(job.id) else None
            job.next_run_at = None
            if db is not None:
                db.commit()
            return
        trigger = self._build_trigger(job)
        if not self.scheduler.running:
            job.next_run_at = trigger.get_next_fire_time(None, datetime.now(UTC))
            if db is not None:
                db.commit()
            return
        runtime_job = self.scheduler.add_job(
            self.dispatch,
            trigger=trigger,
            id=job.id,
            args=[job.id],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        job.next_run_at = runtime_job.next_run_time
        if db is not None:
            db.commit()

    def remove_job(self, job_id: str) -> bool:
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            return True
        return False

    def remove_account_jobs(self, account_id: str) -> int:
        """Remove every runtime job for an account whose authentication became invalid."""
        with self.factory() as db:
            job_ids = list(
                db.scalars(select(ScheduleJob.id).where(ScheduleJob.bili_account_id == account_id))
            )
        return sum(self.remove_job(job_id) for job_id in job_ids)

    async def run_notification_reconciliation(self) -> None:
        """Periodic system job: repair silent notification gaps after interruptions.

        Runs as an in-memory scheduler job (like cleanup) because reconciliation is
        a cross-tenant system concern, not a per-user schedule. The audit summary is
        written as structured logs; manual admin runs can also read it from the API.
        """
        started = datetime.now(UTC)
        service = NotificationReconciliationService()
        logger.info(
            "notification_reconciliation_started",
            extra={
                "lookback_hours": service.lookback_hours,
                "max_records": service.max_records,
            },
        )
        try:
            with self.factory() as db:
                summary = service.run(db)
            logger.info(
                "notification_reconciliation_completed",
                extra={
                    **summary.to_dict(),
                    "duration_ms": max(
                        0, int((datetime.now(UTC) - started).total_seconds() * 1000)
                    ),
                },
            )
        except Exception:
            logger.exception("notification_reconciliation_failed")
            raise

    async def cleanup_expired(self) -> None:
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=get_settings().retention_days)
        with self.factory() as db:
            db.execute(delete(UserSession).where(UserSession.expires_at < now))
            db.execute(delete(QrLoginSession).where(QrLoginSession.expires_at < now))
            db.execute(delete(NotificationOutbox).where(
                NotificationOutbox.created_at < cutoff,
                NotificationOutbox.status.in_(["delivered", "failed"]),
            ))
            db.execute(delete(JobRun).where(
                JobRun.started_at < cutoff,
                JobRun.status.in_([RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.SKIPPED]),
            ))
            stale = db.scalars(select(JobRun).where(
                JobRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                JobRun.started_at < now - timedelta(hours=6),
            ))
            for run in stale:
                run.status = RunStatus.FAILED
                run.error = "run interrupted or timed out"
                run.finished_at = now
            db.commit()

    def _build_trigger(self, job: ScheduleJob) -> IntervalTrigger | CronTrigger:
        if job.trigger_type == "interval":
            return IntervalTrigger(seconds=int(job.trigger_config["seconds"]))
        if job.trigger_type == "cron":
            return CronTrigger.from_crontab(
                job.trigger_config["expression"], timezone=self.scheduler.timezone
            )
        raise ValueError(f"unsupported trigger type: {job.trigger_type}")

    async def dispatch(self, job_id: str, run_id: str | None = None) -> None:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._dispatch_tasks.add(current_task)
        client = BilibiliClient()
        try:
            with self.factory() as db:
                job = db.get(ScheduleJob, job_id)
                if job is None or not job.enabled:
                    self._finish_queued_run(db, run_id, "job is disabled")
                    return
                if run_id is None:
                    run = JobRun(
                        user_id=job.user_id,
                        schedule_job_id=job.id,
                        bili_account_id=job.bili_account_id,
                        trigger_type="scheduled",
                        scheduled_at=datetime.now(UTC),
                        status=RunStatus.QUEUED,
                        started_at=datetime.now(UTC),
                    )
                    db.add(run)
                    db.commit()
                    run_id = run.id
                if job.kind == JobKind.NOTIFICATION_RETRY:
                    delivery = NotificationDeliveryService()
                    try:
                        result = await delivery.process_pending_summary(db, job.user_id)
                        result["conclusion"] = (
                            "扫描 0 条，无需重试"
                            if not result["retried"]
                            else f"已重试 {result['retried']} 个通知事件"
                        )
                        self._finish_queued_run(db, run_id, result=result)
                    finally:
                        await delivery.close()
                    return
                if not job.bili_account_id:
                    self._finish_queued_run(db, run_id, "job has no account")
                    return
                account = db.scalar(
                    select(BiliAccount).where(
                        BiliAccount.id == job.bili_account_id,
                        BiliAccount.user_id == job.user_id,
                    )
                )
                if account is None:
                    self._finish_queued_run(db, run_id, "account not found")
                    return
                if account.status != AccountStatus.ACTIVE:
                    self._finish_queued_run(db, run_id, "account is not active")
                    return
                handlers: dict[JobKind, Callable] = {
                    JobKind.CHARGE_COLLECTION: ChargeCollectionService(client).collect,
                    JobKind.COUPON_CLAIM: CouponClaimService(client).claim,
                    JobKind.DAILY_TASK: DailyTaskService(client).run,
                }
                handler = handlers.get(job.kind)
                if handler:
                    await handler(db, account, job.id, run_id=run_id)
                else:
                    self._finish_queued_run(db, run_id, "unsupported job kind")
        except CancelledError:
            self._fail_run(run_id, "run interrupted during shutdown")
            raise
        except Exception:
            logger.exception("scheduled job failed", extra={"job_id": job_id})
            with self.factory() as db:
                self._fail_run(run_id, "job execution failed", db)
                failed_job = db.get(ScheduleJob, job_id)
                if failed_job:
                    if not failed_job.enabled:
                        self.remove_job(failed_job.id)
                    enqueue_event(
                        db,
                        failed_job.user_id,
                        "scheduled_job_failed",
                        f"scheduled:{job_id}",
                        {"job_id": job_id, "kind": failed_job.kind.value},
                    )
                    db.commit()
        finally:
            await client.close()
            try:
                self._persist_next_run_at(job_id)
            except SQLAlchemyError:
                logger.warning(
                    "could not persist the next scheduled runtime",
                    extra={"job_id": job_id},
                    exc_info=True,
                )
            if current_task is not None:
                self._dispatch_tasks.discard(current_task)

    def _fail_run(self, run_id: str | None, error: str, db: Session | None = None) -> None:
        if not run_id:
            return
        owned_db = db is None
        session = db or self.factory()
        try:
            run = session.get(JobRun, run_id)
            if run and run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                finished_at = datetime.now(UTC)
                run.status = RunStatus.FAILED
                run.error_type = "scheduler_error"
                run.error = error
                run.finished_at = finished_at
                started_at = run.started_at.replace(tzinfo=run.started_at.tzinfo or UTC)
                run.duration_ms = max(
                    0, int((finished_at - started_at).total_seconds() * 1000)
                )
                session.commit()
        finally:
            if owned_db:
                session.close()

    def _persist_next_run_at(self, job_id: str) -> None:
        with self.factory() as db:
            job = db.get(ScheduleJob, job_id)
            if job is None:
                self.remove_job(job_id)
                return
            if job.bili_account_id:
                account = db.get(BiliAccount, job.bili_account_id)
                if account is None or account.status != AccountStatus.ACTIVE:
                    account_jobs = list(
                        db.scalars(
                            select(ScheduleJob).where(
                                ScheduleJob.bili_account_id == job.bili_account_id
                            )
                        )
                    )
                    for account_job in account_jobs:
                        account_job.next_run_at = None
                    db.commit()
                    self.remove_account_jobs(job.bili_account_id)
                    return
            runtime_job = self.scheduler.get_job(job.id)
            if not job.enabled:
                self.remove_job(job.id)
                job.next_run_at = None
            else:
                job.next_run_at = runtime_job.next_run_time if runtime_job else None
            db.commit()

    @staticmethod
    def _finish_queued_run(
        db: Session,
        run_id: str | None,
        error: str | None = None,
        result: dict | None = None,
    ) -> None:
        if not run_id:
            return
        run = db.get(JobRun, run_id)
        if run and run.status == RunStatus.QUEUED:
            run.status = RunStatus.FAILED if error else RunStatus.SUCCEEDED
            run.error = error
            run.result = result or {}
            run.finished_at = datetime.now(UTC)
            started_at = run.started_at.replace(tzinfo=run.started_at.tzinfo or UTC)
            try:
                run.duration_ms = max(
                    0, int((run.finished_at - started_at).total_seconds() * 1000)
                )
            except (TypeError, ValueError, OverflowError):
                run.duration_ms = 0
            db.commit()
