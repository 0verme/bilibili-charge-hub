import logging
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
        try:
            with self.factory() as db:
                for job in db.scalars(select(ScheduleJob).where(ScheduleJob.enabled.is_(True))):
                    self.sync_job(job, db)
        except SQLAlchemyError:
            logger.warning("scheduler started before database migrations were available")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def sync_job(self, job: ScheduleJob, db: Session | None = None) -> None:
        if not job.enabled:
            self.scheduler.remove_job(job.id) if self.scheduler.get_job(job.id) else None
            job.next_run_at = None
            if db is not None:
                db.commit()
            return
        trigger = self._build_trigger(job)
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

    def remove_job(self, job_id: str) -> None:
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

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
        client = BilibiliClient()
        try:
            with self.factory() as db:
                job = db.get(ScheduleJob, job_id)
                if job is None or not job.enabled:
                    self._finish_queued_run(db, run_id, "job is disabled")
                    return
                if job.kind == JobKind.NOTIFICATION_RETRY:
                    delivery = NotificationDeliveryService()
                    try:
                        await delivery.process_pending(db, job.user_id)
                        self._finish_queued_run(db, run_id)
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
                }
                handler = handlers.get(job.kind)
                if handler:
                    await handler(db, account, job.id, run_id=run_id)
                else:
                    self._finish_queued_run(db, run_id, "unsupported job kind")
        except Exception:
            logger.exception("scheduled job failed", extra={"job_id": job_id})
            with self.factory() as db:
                if run_id:
                    queued_run = db.get(JobRun, run_id)
                    if queued_run and queued_run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                        queued_run.status = RunStatus.FAILED
                        queued_run.error = "job execution failed"
                        queued_run.finished_at = datetime.now(UTC)
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

    @staticmethod
    def _finish_queued_run(db: Session, run_id: str | None, error: str | None = None) -> None:
        if not run_id:
            return
        run = db.get(JobRun, run_id)
        if run and run.status == RunStatus.QUEUED:
            run.status = RunStatus.FAILED if error else RunStatus.SUCCEEDED
            run.error = error
            run.finished_at = datetime.now(UTC)
            run.duration_ms = 0
            db.commit()
