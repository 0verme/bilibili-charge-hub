import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import BiliAccount, ChargeRecord, User
from app.notifications.service import enqueue_event
from app.security import hash_password

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
    assert revision == "0003_single_instance_hardening"

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
