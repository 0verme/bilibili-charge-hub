import asyncio

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
