import logging
from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.bilibili.client import BilibiliClient
from app.models import BiliAccount, JobKind, ScheduleJob
from app.notifications.service import NotificationDeliveryService, enqueue_event
from app.services.collection import ChargeCollectionService
from app.services.coupon import CouponClaimService

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

    def _build_trigger(self, job: ScheduleJob) -> IntervalTrigger | CronTrigger:
        if job.trigger_type == "interval":
            return IntervalTrigger(seconds=int(job.trigger_config["seconds"]))
        if job.trigger_type == "cron":
            return CronTrigger.from_crontab(
                job.trigger_config["expression"], timezone=self.scheduler.timezone
            )
        raise ValueError(f"unsupported trigger type: {job.trigger_type}")

    async def dispatch(self, job_id: str) -> None:
        client = BilibiliClient()
        try:
            with self.factory() as db:
                job = db.get(ScheduleJob, job_id)
                if job is None or not job.enabled:
                    return
                if job.kind == JobKind.NOTIFICATION_RETRY:
                    delivery = NotificationDeliveryService()
                    try:
                        await delivery.process_pending(db, job.user_id)
                    finally:
                        await delivery.close()
                    return
                if not job.bili_account_id:
                    return
                account = db.scalar(
                    select(BiliAccount).where(
                        BiliAccount.id == job.bili_account_id,
                        BiliAccount.user_id == job.user_id,
                    )
                )
                if account is None:
                    return
                handlers: dict[JobKind, Callable] = {
                    JobKind.CHARGE_COLLECTION: ChargeCollectionService(client).collect,
                    JobKind.COUPON_CLAIM: CouponClaimService(client).claim,
                }
                handler = handlers.get(job.kind)
                if handler:
                    await handler(db, account, job.id)
        except Exception:
            logger.exception("scheduled job failed", extra={"job_id": job_id})
            with self.factory() as db:
                failed_job = db.get(ScheduleJob, job_id)
                if failed_job:
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
