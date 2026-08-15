import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.crypto import get_credential_cipher
from app.models import (
    Base,
    BiliAccount,
    ChargeRecord,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
    User,
    UserRole,
)
from app.notifications.service import enqueue_event, new_charge_payload, tenant_dedupe_key
from app.routers.notifications import reconcile_notifications
from app.security import hash_password
from app.services import reconciliation as reconciliation_module
from app.services.reconciliation import NotificationReconciliationService, ReconciliationSummary
from app.services.scheduler import SchedulerManager


@pytest.fixture
def reconciliation_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db


@pytest.fixture
def scheduler_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_user(db: Session, username: str, role: UserRole = UserRole.USER) -> User:
    user = User(
        username=username,
        password_hash=hash_password(f"{username}-password-42"),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def make_account(db: Session, user: User, bili_uid: str = "123456") -> BiliAccount:
    account = BiliAccount(
        user_id=user.id,
        bili_uid=bili_uid,
        encrypted_cookie=get_credential_cipher().encrypt("SESSDATA=fake-cookie"),
    )
    db.add(account)
    db.commit()
    return account


def make_charge(
    db: Session,
    account: BiliAccount,
    event_id: str,
    created_at: datetime | None = None,
) -> ChargeRecord:
    record = ChargeRecord(
        user_id=account.user_id,
        bili_account_id=account.id,
        event_id=event_id,
        supporter_uid="10001",
        supporter_name="Alice",
        avatar_url="",
        amount=Decimal("10.50"),
        brokerage=Decimal("7.00"),
        remark="",
        charged_at=datetime.now(UTC),
        raw_data={"schema_version": 1},
    )
    if created_at is not None:
        record.created_at = created_at
    db.add(record)
    db.commit()
    return record


def make_channel(db: Session, user: User, name: str) -> str:
    channel = NotificationChannel(
        user_id=user.id,
        name=name,
        provider="webhook",
        encrypted_config=get_credential_cipher().encrypt_json({"url": "https://example.com/hook"}),
    )
    db.add(channel)
    db.flush()
    db.add(
        NotificationSubscription(
            user_id=user.id,
            channel_id=channel.id,
            event_type="new_charge",
        )
    )
    db.commit()
    return channel.id


def run_reconciliation(db: Session, **kwargs) -> ReconciliationSummary:
    return NotificationReconciliationService(**kwargs).run(db)


def outbox_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0


def delivery_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(NotificationDelivery)) or 0


def find_outbox(db: Session, record: ChargeRecord) -> NotificationOutbox | None:
    key = tenant_dedupe_key(
        record.user_id, f"charge:{record.bili_account_id}:{record.event_id}"
    )
    return db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.user_id == record.user_id,
            NotificationOutbox.dedupe_key == key,
        )
    )


def test_r01_missing_outbox_is_rebuilt_exactly_once(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "r01")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r01")
    assert outbox_count(reconciliation_db) == 0

    first = run_reconciliation(reconciliation_db)
    assert first.missing_outbox == 1 and first.outbox_rebuilt == 1
    assert outbox_count(reconciliation_db) == 1

    outbox = find_outbox(reconciliation_db, record)
    assert outbox is not None
    assert outbox.event_type == "new_charge"
    assert outbox.user_id == user.id
    assert outbox.payload == new_charge_payload(record)

    second = run_reconciliation(reconciliation_db)
    assert outbox_count(reconciliation_db) == 1
    assert second.missing_outbox == 0 and second.outbox_rebuilt == 0
    assert second.errors == 0


def test_r02_existing_outbox_is_never_duplicated(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "r02")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r02")
    enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    reconciliation_db.commit()
    assert outbox_count(reconciliation_db) == 1

    run_reconciliation(reconciliation_db)

    assert outbox_count(reconciliation_db) == 1
    assert reconciliation_db.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.dedupe_key == tenant_dedupe_key(
            user.id, f"charge:{account.id}:{record.event_id}"
        ))
    ) == 1


def test_r03_delivered_outbox_requeues_for_missing_channel(
    reconciliation_db: Session,
) -> None:
    user = make_user(reconciliation_db, "r03")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r03")
    channel_a = make_channel(reconciliation_db, user, "existing-channel")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    reconciliation_db.add(
        NotificationDelivery(
            user_id=user.id,
            outbox_id=outbox.id,
            channel_id=channel_a,
            status="succeeded",
            attempts=1,
            delivered_at=datetime.now(UTC),
        )
    )
    outbox.status = "delivered"
    outbox.attempts = 1
    reconciliation_db.commit()

    channel_b = make_channel(reconciliation_db, user, "new-channel")

    summary = run_reconciliation(reconciliation_db)
    reconciliation_db.refresh(outbox)

    assert summary.missing_deliveries == 1
    assert summary.deliveries_created == 1
    assert summary.requeued == 1
    assert outbox.status == "retry"
    assert delivery_count(reconciliation_db) == 2
    created = reconciliation_db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel_id == channel_b)
    )
    assert created is not None and created.status == "pending"
    assert created.user_id == user.id


def test_r03b_pending_outbox_gets_missing_delivery_without_requeue(
    reconciliation_db: Session,
) -> None:
    user = make_user(reconciliation_db, "r03b")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r03b")
    channel = make_channel(reconciliation_db, user, "pending-channel")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    reconciliation_db.commit()

    summary = run_reconciliation(reconciliation_db)

    assert summary.missing_deliveries == 1
    assert summary.deliveries_created == 1
    assert summary.requeued == 0
    reconciliation_db.refresh(outbox)
    assert outbox.status == "pending"
    created = reconciliation_db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel_id == channel)
    )
    assert created is not None and created.status == "pending"


def test_r04_succeeded_delivery_is_never_resent(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "r04")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r04")
    channel = make_channel(reconciliation_db, user, "success-channel")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    reconciliation_db.add(
        NotificationDelivery(
            user_id=user.id,
            outbox_id=outbox.id,
            channel_id=channel,
            status="succeeded",
            attempts=1,
            delivered_at=datetime.now(UTC),
        )
    )
    outbox.status = "delivered"
    outbox.attempts = 1
    reconciliation_db.commit()

    summary = run_reconciliation(reconciliation_db)

    assert summary.deliveries_created == 0
    assert summary.requeued == 0
    assert summary.already_complete == 1
    delivery = reconciliation_db.scalar(select(NotificationDelivery))
    assert delivery is not None
    assert delivery.status == "succeeded" and delivery.attempts == 1


def test_r05_failed_delivery_respects_retry_policy(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "r05")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r05")
    channel = make_channel(reconciliation_db, user, "failing-channel")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    future = datetime.now(UTC) + timedelta(minutes=10)
    reconciliation_db.add(
        NotificationDelivery(
            user_id=user.id,
            outbox_id=outbox.id,
            channel_id=channel,
            status="failed",
            attempts=3,
            available_at=future,
            error_type="provider_rejected",
            response_summary="HTTP 503",
        )
    )
    outbox.status = "retry"
    outbox.attempts = 3
    outbox.available_at = future
    reconciliation_db.commit()

    summary = run_reconciliation(reconciliation_db)

    assert summary.deliveries_created == 0
    assert summary.requeued == 0
    reconciliation_db.refresh(outbox)
    delivery = reconciliation_db.scalar(select(NotificationDelivery))
    assert delivery is not None
    assert delivery.status == "failed" and delivery.attempts == 3
    assert delivery.available_at == future.replace(tzinfo=None)


def test_r05b_exhausted_outbox_budget_is_audited_not_forced(
    reconciliation_db: Session,
) -> None:
    user = make_user(reconciliation_db, "r05b")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r05b")
    make_channel(reconciliation_db, user, "never-delivered-channel")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    outbox.status = "failed"
    outbox.attempts = 5
    reconciliation_db.commit()

    summary = run_reconciliation(reconciliation_db)

    assert summary.skipped == 1
    assert summary.deliveries_created == 0
    assert delivery_count(reconciliation_db) == 0


def test_r06_no_subscription_is_safe_and_not_a_failure(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "r06")
    account = make_account(reconciliation_db, user)
    record = make_charge(reconciliation_db, account, "evt-r06")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    reconciliation_db.commit()

    summary = run_reconciliation(reconciliation_db)

    assert summary.deliveries_created == 0
    assert summary.errors == 0
    assert delivery_count(reconciliation_db) == 0
    reconciliation_db.refresh(outbox)
    assert outbox.status == "pending"


def test_r07_tenant_isolation_never_crosses_users(reconciliation_db: Session) -> None:
    user_a = make_user(reconciliation_db, "tenant-a")
    account_a = make_account(reconciliation_db, user_a, bili_uid="111")
    record_a = make_charge(reconciliation_db, account_a, "evt-a")
    channel_a = make_channel(reconciliation_db, user_a, "a-channel")

    user_b = make_user(reconciliation_db, "tenant-b")
    account_b = make_account(reconciliation_db, user_b, bili_uid="222")
    record_b = make_charge(reconciliation_db, account_b, "evt-b")
    channel_b = make_channel(reconciliation_db, user_b, "b-channel")

    summary = run_reconciliation(reconciliation_db)

    assert summary.missing_outbox == 2
    assert summary.outbox_rebuilt == 2
    assert summary.deliveries_created == 2

    outboxes = list(reconciliation_db.scalars(select(NotificationOutbox)))
    by_record = {
        find_outbox(reconciliation_db, record_a).id: record_a,
        find_outbox(reconciliation_db, record_b).id: record_b,
    }
    for outbox in outboxes:
        assert outbox.user_id == by_record[outbox.id].user_id

    deliveries = list(reconciliation_db.scalars(select(NotificationDelivery)))
    assert len(deliveries) == 2
    delivery_by_channel = {item.channel_id: item for item in deliveries}
    assert delivery_by_channel[channel_a].user_id == user_a.id
    assert delivery_by_channel[channel_b].user_id == user_b.id
    assert delivery_by_channel[channel_a].outbox_id in {
        o.id for o in outboxes if o.user_id == user_a.id
    }
    assert delivery_by_channel[channel_b].outbox_id in {
        o.id for o in outboxes if o.user_id == user_b.id
    }


def test_r08_reconciliation_is_idempotent_over_three_runs(
    reconciliation_db: Session,
) -> None:
    user = make_user(reconciliation_db, "r08")
    account = make_account(reconciliation_db, user)
    make_charge(reconciliation_db, account, "evt-missing-outbox")
    make_channel(reconciliation_db, user, "channel")

    record = make_charge(reconciliation_db, account, "evt-missing-delivery")
    outbox = enqueue_event(
        reconciliation_db,
        user.id,
        "new_charge",
        f"charge:{account.id}:{record.event_id}",
        new_charge_payload(record),
    )
    assert outbox is not None
    outbox.status = "delivered"
    outbox.attempts = 1
    reconciliation_db.commit()

    first = run_reconciliation(reconciliation_db)
    second = run_reconciliation(reconciliation_db)
    third = run_reconciliation(reconciliation_db)

    assert first.missing_outbox == 1 and first.deliveries_created == 2
    assert second.missing_outbox == 0 and second.deliveries_created == 0
    assert third.missing_outbox == 0 and third.deliveries_created == 0
    assert outbox_count(reconciliation_db) == 2
    assert delivery_count(reconciliation_db) == 2
    assert first.requeued == 1
    assert second.requeued == 0 and third.requeued == 0


def test_r09_crash_gap_charge_without_notification_is_recovered(
    reconciliation_db: Session,
) -> None:
    # Simulates: ChargeRecord committed, process crashed before the outbox row.
    user = make_user(reconciliation_db, "r09")
    account = make_account(reconciliation_db, user)
    make_charge(reconciliation_db, account, "evt-crash")

    summary = run_reconciliation(reconciliation_db)

    assert summary.missing_outbox == 1
    assert summary.outbox_rebuilt == 1
    assert summary.errors == 0
    assert (
        find_outbox(reconciliation_db, reconciliation_db.scalar(select(ChargeRecord)))
        is not None
    )


def test_r10_lookback_window_respects_cutoff_and_timezone(
    reconciliation_db: Session,
) -> None:
    user = make_user(reconciliation_db, "r10")
    account = make_account(reconciliation_db, user)
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    inside = make_charge(
        reconciliation_db, account, "evt-inside", created_at=now - timedelta(hours=1)
    )
    outside = make_charge(
        reconciliation_db, account, "evt-outside", created_at=now - timedelta(hours=25)
    )
    boundary = make_charge(
        reconciliation_db, account, "evt-boundary", created_at=now - timedelta(hours=24)
    )

    service = NotificationReconciliationService(lookback_hours=24)
    summary = service.run(reconciliation_db, now=now)

    assert summary.scanned == 2
    assert find_outbox(reconciliation_db, inside) is not None
    assert find_outbox(reconciliation_db, outside) is None
    assert find_outbox(reconciliation_db, boundary) is not None
    assert summary.errors == 0


def test_r11_max_scan_limit_bounds_a_run(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "r11")
    account = make_account(reconciliation_db, user)
    make_charge(reconciliation_db, account, "evt-1")
    make_charge(reconciliation_db, account, "evt-2")
    make_charge(reconciliation_db, account, "evt-3")

    service = NotificationReconciliationService(max_records=2)
    summary = service.run(reconciliation_db)

    assert summary.scanned == 2
    assert summary.missing_outbox == 2
    assert outbox_count(reconciliation_db) == 2


def test_r12_reconciliation_makes_no_network_calls(
    reconciliation_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(reconciliation_db, "r12")
    account = make_account(reconciliation_db, user)
    make_charge(reconciliation_db, account, "evt-r12")
    make_channel(reconciliation_db, user, "r12-channel")

    def deny_network(*args, **kwargs):
        raise AssertionError("reconciliation must not open network connections")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", deny_network)
    summary = run_reconciliation(reconciliation_db)

    assert summary.missing_outbox == 1
    assert summary.outbox_rebuilt == 1
    assert summary.deliveries_created == 1
    assert summary.errors == 0


def test_concurrent_runs_do_not_overlap(reconciliation_db: Session) -> None:
    user = make_user(reconciliation_db, "concurrent")
    account = make_account(reconciliation_db, user)
    make_charge(reconciliation_db, account, "evt-concurrent")

    reconciliation_module._reconciliation_lock.acquire()
    try:
        summary = run_reconciliation(reconciliation_db)
        assert summary.concurrency_skipped is True
        assert outbox_count(reconciliation_db) == 0
    finally:
        reconciliation_module._reconciliation_lock.release()


def test_admin_reconcile_endpoint_returns_summary(reconciliation_db: Session) -> None:
    admin = make_user(reconciliation_db, "admin", role=UserRole.ADMIN)
    account = make_account(reconciliation_db, admin)
    make_charge(reconciliation_db, account, "evt-admin")

    result = reconcile_notifications(admin, reconciliation_db)

    assert result == {
        "scanned": 1,
        "missing_outbox": 1,
        "outbox_rebuilt": 1,
        "missing_deliveries": 0,
        "deliveries_created": 0,
        "requeued": 0,
        "already_complete": 0,
        "skipped": 0,
        "errors": 0,
        "concurrency_skipped": False,
    }


def test_scheduler_registers_and_runs_reconciliation_system_job(
    scheduler_factory: sessionmaker[Session],
) -> None:
    with scheduler_factory() as db:
        user = make_user(db, "sched-recon")
        account = make_account(db, user)
        make_charge(db, account, "evt-sched-recon")

    async def exercise() -> None:
        manager = SchedulerManager(scheduler_factory, "Asia/Shanghai")
        manager.start()
        try:
            runtime = manager.scheduler.get_job("notification-reconciliation")
            assert runtime is not None
            assert runtime.max_instances == 1
            await manager.run_notification_reconciliation()
            with scheduler_factory() as db:
                assert outbox_count(db) == 1
                record = db.scalar(select(ChargeRecord))
                assert record is not None
                assert find_outbox(db, record) is not None
        finally:
            manager.shutdown()

    asyncio.run(exercise())
