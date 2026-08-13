import asyncio

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.crypto import get_credential_cipher
from app.models import (
    Base,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
    User,
    UserRole,
)
from app.notifications.providers import SendResult, validate_webhook_url
from app.notifications.service import NotificationDeliveryService, enqueue_event
from app.routers.notifications import mask_config, validate_channel_config
from app.security import hash_password


class SuccessfulProvider:
    async def send(self, message: str, config: dict) -> SendResult:
        assert "收到新的充电" in message
        return SendResult(True, "HTTP 200")


class FailingProvider:
    async def send(self, message: str, config: dict) -> SendResult:
        return SendResult(False, "HTTP 503")


@pytest.fixture
def notification_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db


def make_notification_user(db: Session) -> User:
    user = User(
        username="notifier",
        password_hash=hash_password("notifier-password-42"),
        role=UserRole.USER,
    )
    db.add(user)
    db.commit()
    return user


def add_channel(db: Session, user: User, name: str, provider: str) -> NotificationChannel:
    channel = NotificationChannel(
        user_id=user.id,
        name=name,
        provider=provider,
        encrypted_config=get_credential_cipher().encrypt_json({"key": f"{name}-secret"}),
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
    return channel


def test_outbox_dedupes_and_channels_fail_independently(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    successful = add_channel(notification_db, user, "success", "good")
    failing = add_channel(notification_db, user, "failure", "bad")
    event = enqueue_event(
        notification_db,
        user.id,
        "new_charge",
        "charge:1",
        {"supporter": "Alice", "amount": "10.00"},
    )
    duplicate = enqueue_event(
        notification_db,
        user.id,
        "new_charge",
        "charge:1",
        {"supporter": "Alice", "amount": "10.00"},
    )
    notification_db.commit()

    assert event is not None
    assert duplicate is None
    service = NotificationDeliveryService(
        providers={"good": SuccessfulProvider(), "bad": FailingProvider()}
    )
    asyncio.run(service.deliver_event(notification_db, event))

    deliveries = list(notification_db.scalars(select(NotificationDelivery)))
    by_channel = {item.channel_id: item for item in deliveries}
    assert by_channel[successful.id].status == "succeeded"
    assert by_channel[failing.id].status == "failed"
    assert event.status == "retry"
    assert event.attempts == 1

    event.available_at = event.created_at
    by_channel[failing.id].available_at = event.created_at
    asyncio.run(service.deliver_event(notification_db, event))
    notification_db.refresh(by_channel[successful.id])
    notification_db.refresh(by_channel[failing.id])
    assert by_channel[successful.id].attempts == 1
    assert by_channel[failing.id].attempts == 2


def test_tenant_dedupe_keys_do_not_collide(notification_db: Session) -> None:
    first = make_notification_user(notification_db)
    second = User(
        username="second",
        password_hash=hash_password("second-password-42"),
        role=UserRole.USER,
    )
    notification_db.add(second)
    notification_db.commit()

    assert enqueue_event(notification_db, first.id, "cookie_expired", "account:1", {})
    assert enqueue_event(notification_db, second.id, "cookie_expired", "account:1", {})
    notification_db.commit()
    assert notification_db.scalar(select(func.count()).select_from(NotificationOutbox)) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://[::1]/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/hook",
        "file:///etc/passwd",
        "http://user:password@example.com/hook",
    ],
)
def test_webhook_validation_rejects_ssrf_targets(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_channel_configuration_is_masked_and_dangerous_headers_are_rejected() -> None:
    config = {"bot_token": "123456789-secret", "chat_id": "99887766"}
    validate_channel_config("telegram", config)
    masked = mask_config(config)
    assert "secret" not in str(masked)
    assert masked["bot_token"].endswith("cret")

    with pytest.raises(ValueError):
        validate_channel_config(
            "webhook",
            {"url": "https://example.com/hook", "headers": {"Host": "internal"}},
        )
