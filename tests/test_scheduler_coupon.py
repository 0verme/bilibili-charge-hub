import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bilibili.client import BilibiliAuthenticationError, CouponResult
from app.crypto import get_credential_cipher
from app.models import (
    AccountStatus,
    Base,
    BiliAccount,
    CouponClaim,
    JobKind,
    JobRun,
    RunStatus,
    ScheduleJob,
    User,
    UserRole,
)
from app.notifications.service import NotificationDeliveryService
from app.security import hash_password
from app.services.coupon import CouponClaimService
from app.services.scheduler import SchedulerManager


class SuccessfulCouponClient:
    def __init__(self) -> None:
        self.calls = 0

    async def claim_coupon(self, cookie_header: str, csrf: str) -> CouponResult:
        assert "SESSDATA=fake-session" in cookie_header
        assert csrf == "fake-csrf"
        self.calls += 1
        return CouponResult(status="success", code="0", message="claimed")


class ExpiredCouponClient:
    async def claim_coupon(self, cookie_header: str, csrf: str) -> CouponResult:
        raise BilibiliAuthenticationError("expired")


@pytest.fixture
def scheduler_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_scheduler_account(factory: sessionmaker[Session]) -> str:
    with factory() as db:
        user = User(
            username="scheduler",
            password_hash=hash_password("scheduler-password-42"),
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        account = BiliAccount(
            user_id=user.id,
            bili_uid="123456",
            encrypted_cookie=get_credential_cipher().encrypt(
                "SESSDATA=fake-session; bili_jct=fake-csrf"
            ),
        )
        db.add(account)
        db.commit()
        return account.id


def test_scheduler_restores_persistent_jobs(scheduler_factory: sessionmaker[Session]) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        job = ScheduleJob(
            user_id=account.user_id,
            bili_account_id=account.id,
            kind=JobKind.CHARGE_COLLECTION,
            trigger_type="interval",
            trigger_config={"seconds": 60},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    async def exercise() -> None:
        manager = SchedulerManager(scheduler_factory, "Asia/Shanghai")
        manager.start()
        try:
            runtime = manager.scheduler.get_job(job_id)
            assert runtime is not None
            assert runtime.max_instances == 1
            with scheduler_factory() as db:
                stored = db.get(ScheduleJob, job_id)
                assert stored is not None and stored.next_run_at is not None
                stored.enabled = False
                db.commit()
                manager.sync_job(stored, db)
            assert manager.scheduler.get_job(job_id) is None
        finally:
            manager.shutdown()

    asyncio.run(exercise())


def test_scheduler_fails_interrupted_runs_immediately_on_start(
    scheduler_factory: sessionmaker[Session],
) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        started_at = datetime.now(UTC) - timedelta(minutes=2)
        runs = [
            JobRun(user_id=account.user_id, status=RunStatus.QUEUED, started_at=started_at),
            JobRun(user_id=account.user_id, status=RunStatus.RUNNING, started_at=started_at),
        ]
        db.add_all(runs)
        db.commit()
        run_ids = [run.id for run in runs]

    async def exercise() -> None:
        manager = SchedulerManager(scheduler_factory, "Asia/Shanghai")
        manager.start()
        try:
            with scheduler_factory() as db:
                recovered = [db.get(JobRun, run_id) for run_id in run_ids]
                assert all(run is not None for run in recovered)
                assert all(run.status == RunStatus.FAILED for run in recovered if run)
                assert all(run.finished_at is not None for run in recovered if run)
                assert all("process restart" in (run.error or "") for run in recovered if run)
        finally:
            manager.shutdown()

    asyncio.run(exercise())


def test_dispatch_persists_the_following_runtime_after_each_execution(
    scheduler_factory: sessionmaker[Session],
) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        job = ScheduleJob(
            user_id=account.user_id,
            kind=JobKind.NOTIFICATION_RETRY,
            trigger_type="interval",
            trigger_config={"seconds": 3600},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    async def exercise() -> None:
        manager = SchedulerManager(scheduler_factory, "Asia/Shanghai")
        manager.start()
        try:
            with scheduler_factory() as db:
                stored = db.get(ScheduleJob, job_id)
                assert stored is not None
                stored.next_run_at = None
                db.commit()
            await manager.dispatch(job_id)
            with scheduler_factory() as db:
                stored = db.get(ScheduleJob, job_id)
                assert stored is not None and stored.next_run_at is not None
                run = db.scalar(select(JobRun).where(JobRun.schedule_job_id == job_id))
                assert run is not None and run.status == RunStatus.SUCCEEDED
        finally:
            manager.shutdown()

    asyncio.run(exercise())


def test_account_runtime_jobs_can_be_removed_together(
    scheduler_factory: sessionmaker[Session],
) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        jobs = [
            ScheduleJob(
                user_id=account.user_id,
                bili_account_id=account.id,
                kind=kind,
                trigger_type="interval",
                trigger_config={"seconds": 3600},
            )
            for kind in (JobKind.CHARGE_COLLECTION, JobKind.COUPON_CLAIM)
        ]
        db.add_all(jobs)
        db.commit()
        job_ids = [job.id for job in jobs]

    async def exercise() -> None:
        manager = SchedulerManager(scheduler_factory, "Asia/Shanghai")
        manager.start()
        try:
            assert all(manager.scheduler.get_job(job_id) is not None for job_id in job_ids)
            assert manager.remove_account_jobs(account_id) == 2
            assert all(manager.scheduler.get_job(job_id) is None for job_id in job_ids)
        finally:
            manager.shutdown()

    asyncio.run(exercise())


def test_graceful_shutdown_cancels_dispatches_after_deadline(
    scheduler_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        job = ScheduleJob(
            user_id=account.user_id,
            kind=JobKind.NOTIFICATION_RETRY,
            trigger_type="interval",
            trigger_config={"seconds": 3600},
        )
        db.add(job)
        db.commit()
        job_id = job.id
        user_id = account.user_id

    async def exercise() -> None:
        manager = SchedulerManager(scheduler_factory, "Asia/Shanghai")
        started = asyncio.Event()

        async def slow_delivery(
            _service: NotificationDeliveryService,
            _db: Session,
            _user_id: str | None = None,
        ) -> int:
            started.set()
            await asyncio.Event().wait()
            return 0

        monkeypatch.setattr(NotificationDeliveryService, "process_pending", slow_delivery)
        manager.start()
        with scheduler_factory() as db:
            run = JobRun(
                user_id=user_id,
                schedule_job_id=job_id,
                status=RunStatus.QUEUED,
            )
            db.add(run)
            db.commit()
            run_id = run.id
        task = manager.submit_dispatch(job_id, run_id)
        try:
            await started.wait()
            cancelled = await manager.shutdown_gracefully(timeout_seconds=0)
            assert cancelled == 1
            assert task.cancelled()
            with scheduler_factory() as db:
                interrupted = db.get(JobRun, run_id)
                assert interrupted is not None
                assert interrupted.status == RunStatus.FAILED
                assert interrupted.error == "run interrupted during shutdown"
                assert interrupted.finished_at is not None
        finally:
            manager.shutdown()

    asyncio.run(exercise())


def test_coupon_claim_is_idempotent_per_month(scheduler_factory: sessionmaker[Session]) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    client = SuccessfulCouponClient()
    service = CouponClaimService(client)  # type: ignore[arg-type]
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        first = asyncio.run(service.claim(db, account))
        second = asyncio.run(service.claim(db, account))

        assert first.status == second.status == "success"
        assert client.calls == 1
        assert db.scalar(select(func.count()).select_from(CouponClaim)) == 1
        runs = list(db.scalars(select(JobRun).order_by(JobRun.started_at)))
        assert [run.status for run in runs] == [RunStatus.SUCCEEDED, RunStatus.SKIPPED]


def test_coupon_auth_failure_expires_account(scheduler_factory: sessionmaker[Session]) -> None:
    account_id = make_scheduler_account(scheduler_factory)
    service = CouponClaimService(ExpiredCouponClient())  # type: ignore[arg-type]
    with scheduler_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        with pytest.raises(BilibiliAuthenticationError):
            asyncio.run(service.claim(db, account))
        db.refresh(account)
        assert account.status == AccountStatus.EXPIRED
