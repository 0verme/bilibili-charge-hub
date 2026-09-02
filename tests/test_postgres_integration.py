import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    BiliAccount,
    ChargeRecord,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
    User,
)
from app.notifications.service import enqueue_event
from app.readiness import get_code_heads
from app.security import hash_password
from app.services.reconciliation import NotificationReconciliationService

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")


@pytest.fixture
def postgres_session() -> Session:
    assert POSTGRES_URL
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()
    engine.dispose()


def test_postgres_schema_timezone_json_and_tenant_constraints(
    postgres_session: Session,
) -> None:
    revision = postgres_session.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one()
    assert (revision,) == get_code_heads()

    user = User(username="pg-owner", password_hash=hash_password("postgres-password-42"))
    postgres_session.add(user)
    postgres_session.flush()
    account = BiliAccount(
        user_id=user.id,
        bili_uid="123456",
        encrypted_cookie="encrypted-placeholder",
    )
    postgres_session.add(account)
    postgres_session.flush()
    charged_at = datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)
    postgres_session.add(
        ChargeRecord(
            user_id=user.id,
            bili_account_id=account.id,
            event_id="pg-event-1",
            record_key="pg-record-1",
            supporter_uid="10001",
            supporter_name="Alice",
            amount=Decimal("10.00"),
            brokerage=Decimal("7.00"),
            charged_at=charged_at,
            raw_data={"schema_version": 1, "nested": {"ok": True}},
        )
    )
    postgres_session.flush()

    stored = postgres_session.scalar(
        select(ChargeRecord).where(ChargeRecord.event_id == "pg-event-1")
    )
    assert stored is not None
    assert stored.charged_at == charged_at
    assert stored.raw_data["nested"]["ok"] is True

    with pytest.raises(IntegrityError), postgres_session.begin_nested():
        postgres_session.add(
            BiliAccount(
                user_id=user.id,
                bili_uid=account.bili_uid,
                encrypted_cookie="another-placeholder",
            )
        )
        postgres_session.flush()


def test_postgres_outbox_deduplication_is_transactional(postgres_session: Session) -> None:
    user = User(username="pg-notifier", password_hash=hash_password("postgres-password-84"))
    postgres_session.add(user)
    postgres_session.flush()

    first = enqueue_event(postgres_session, user.id, "cookie_expired", "account:1", {})
    duplicate = enqueue_event(postgres_session, user.id, "cookie_expired", "account:1", {})
    assert first is not None
    assert duplicate is None


def _pg_account_and_charge(
    session: Session, username: str, event_id: str
) -> tuple[User, BiliAccount, ChargeRecord]:
    user = User(username=username, password_hash=hash_password(f"postgres-{username}"))
    session.add(user)
    session.flush()
    account = BiliAccount(
        user_id=user.id,
        bili_uid=f"uid-{username}",
        encrypted_cookie="encrypted-placeholder",
    )
    session.add(account)
    session.flush()
    record = ChargeRecord(
        user_id=user.id,
        bili_account_id=account.id,
        event_id=event_id,
        record_key=f"record:{event_id}",
        supporter_uid="10001",
        supporter_name="Alice",
        amount=Decimal("10.50"),
        brokerage=Decimal("7.00"),
        charged_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC),
        raw_data={"schema_version": 1},
    )
    session.add(record)
    session.flush()
    return user, account, record


def test_postgres_reconciliation_repairs_gaps_and_is_idempotent(
    postgres_session: Session,
) -> None:
    user_a, account_a, record_a = _pg_account_and_charge(
        postgres_session, "pg-recon-a", "pg-ev-a"
    )
    user_b, account_b, record_b = _pg_account_and_charge(
        postgres_session, "pg-recon-b", "pg-ev-b"
    )

    for user, suffix in ((user_a, "a"), (user_b, "b")):
        channel = NotificationChannel(
            user_id=user.id,
            name=f"pg-channel-{suffix}",
            provider="webhook",
            encrypted_config='{"url": "https://example.com/hook"}',
        )
        postgres_session.add(channel)
        postgres_session.flush()
        postgres_session.add(
            NotificationSubscription(
                user_id=user.id,
                channel_id=channel.id,
                event_type="new_charge",
            )
        )
    postgres_session.flush()

    service = NotificationReconciliationService()
    summary = service.run(postgres_session)
    assert summary.missing_outbox == 2
    assert summary.outbox_rebuilt == 2
    assert summary.deliveries_created == 2

    second = service.run(postgres_session)
    assert second.missing_outbox == 0
    assert second.deliveries_created == 0

    assert postgres_session.scalar(select(func.count()).select_from(NotificationOutbox)) == 2
    assert postgres_session.scalar(select(func.count()).select_from(NotificationDelivery)) == 2
    outboxes = list(postgres_session.scalars(select(NotificationOutbox)))
    assert {outbox.user_id for outbox in outboxes} == {user_a.id, user_b.id}
    deliveries = list(postgres_session.scalars(select(NotificationDelivery)))
    by_outbox = {outbox.id: outbox for outbox in outboxes}
    assert all(
        delivery.user_id == by_outbox[delivery.outbox_id].user_id
        for delivery in deliveries
    )
    assert {delivery.user_id for delivery in deliveries} == {user_a.id, user_b.id}
    assert record_a.id and record_b.id


def test_postgres_channel_delete_preserves_delivery_audit(postgres_session: Session) -> None:
    user = User(username="pg-channel-delete", password_hash=hash_password("postgres-delete-42"))
    postgres_session.add(user)
    postgres_session.flush()
    channel = NotificationChannel(
        user_id=user.id,
        name="pg-delete-channel",
        provider="webhook",
        encrypted_config='{"url": "https://example.com/hook"}',
    )
    postgres_session.add(channel)
    postgres_session.flush()
    outbox = NotificationOutbox(
        user_id=user.id,
        event_type="cookie_expired",
        dedupe_key="pg-channel-delete-event",
        payload={"account": "test"},
        status="delivered",
        attempts=1,
    )
    postgres_session.add(outbox)
    postgres_session.flush()
    delivery = NotificationDelivery(
        user_id=user.id,
        outbox_id=outbox.id,
        channel_id=channel.id,
        status="succeeded",
        attempts=1,
    )
    postgres_session.add(delivery)
    postgres_session.flush()

    postgres_session.delete(channel)
    postgres_session.flush()
    postgres_session.refresh(delivery)

    assert delivery.channel_id is None


def test_postgres_reconciliation_respects_delivery_unique_constraint(
    postgres_session: Session,
) -> None:
    """A concurrent repair must not 500 and must not duplicate deliveries."""
    user, _account, record = _pg_account_and_charge(
        postgres_session, "pg-recon-race", "pg-ev-race"
    )
    channel = NotificationChannel(
        user_id=user.id,
        name="pg-race-channel",
        provider="webhook",
        encrypted_config='{"url": "https://example.com/hook"}',
    )
    postgres_session.add(channel)
    postgres_session.flush()
    postgres_session.add(
        NotificationSubscription(
            user_id=user.id,
            channel_id=channel.id,
            event_type="new_charge",
        )
    )
    postgres_session.flush()

    service = NotificationReconciliationService()
    first = service.run(postgres_session)
    # Simulate a racing second pass re-running while the first already committed:
    # enqueue_event's tenant-scoped dedupe key and the (outbox_id, channel_id)
    # unique constraint make the second run a no-op instead of an error.
    second = service.run(postgres_session)

    assert first.deliveries_created == 1
    assert second.deliveries_created == 0
    assert second.errors == 0
    assert postgres_session.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
    assert postgres_session.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
