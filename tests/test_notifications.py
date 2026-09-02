import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.routers.notifications as notification_router
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
from app.notifications.service import (
    MAX_ATTEMPTS,
    NotificationDeliveryService,
    enqueue_event,
    render_message,
)
from app.routers.notifications import (
    ChannelInput,
    SubscriptionRuleInput,
    SubscriptionRulesInput,
    delete_channel,
    delivery_view,
    list_deliveries,
    list_subscriptions,
    mask_config,
    retry_delivery,
    update_channel,
    update_subscriptions,
    validate_channel_config,
)
from app.routers.notifications import test_channel as send_test_channel
from app.security import hash_password


class SuccessfulProvider:
    async def send(self, message: str, config: dict) -> SendResult:
        assert "冲了" in message
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


def test_new_charge_message_matches_legacy_format() -> None:
    event = NotificationOutbox(
        user_id="user",
        event_type="new_charge",
        dedupe_key="charge:legacy-format",
        payload={
            "supporter": "娇羞大学长",
            "amount": "5.00",
            "brokerage": "3.36",
            "charged_at": "2026-08-14T00:12:37+00:00",
        },
    )

    assert render_message(event) == (
        "【娇羞大学长】 在 【2026-08-14 08:12:37】\n冲了 5.00B币 实际到账 3.36 元"
    )


def test_event_waits_for_a_channel_without_consuming_retry_budget(
    notification_db: Session,
) -> None:
    user = make_notification_user(notification_db)
    event = enqueue_event(
        notification_db,
        user.id,
        "new_charge",
        "charge:before-channel",
        {"supporter": "Alice", "amount": "10.00"},
    )
    assert event is not None
    notification_db.commit()

    service = NotificationDeliveryService(providers={"good": SuccessfulProvider()})
    asyncio.run(service.deliver_event(notification_db, event))

    notification_db.refresh(event)
    assert event.status == "pending"
    assert event.attempts == 0
    assert notification_db.scalar(select(func.count()).select_from(NotificationDelivery)) == 0

    channel = add_channel(notification_db, user, "later", "good")
    assert asyncio.run(service.process_pending(notification_db, user.id)) == 1

    notification_db.refresh(event)
    delivery = notification_db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel_id == channel.id)
    )
    assert event.status == "delivered"
    assert event.attempts == 1
    assert delivery is not None and delivery.status == "succeeded"


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


def test_manual_retry_resets_terminal_event_budget_and_processes_again(
    notification_db: Session,
) -> None:
    user = make_notification_user(notification_db)
    channel = add_channel(notification_db, user, "success", "good")
    event = enqueue_event(
        notification_db,
        user.id,
        "new_charge",
        "charge:terminal",
        {"supporter": "Alice", "amount": "10.00"},
    )
    assert event is not None
    notification_db.flush()
    future = datetime.now(UTC) + timedelta(days=1)
    event.status = "failed"
    event.attempts = MAX_ATTEMPTS
    event.available_at = future
    delivery = NotificationDelivery(
        user_id=user.id,
        outbox_id=event.id,
        channel_id=channel.id,
        status="failed",
        attempts=MAX_ATTEMPTS,
        available_at=future,
        error_type="provider_rejected",
        response_summary="HTTP 503",
    )
    notification_db.add(delivery)
    notification_db.commit()

    assert retry_delivery(delivery.id, user, notification_db) == {"status": "queued"}
    notification_db.refresh(event)
    notification_db.refresh(delivery)
    assert event.status == "retry" and event.attempts == 0
    assert delivery.status == "pending" and delivery.attempts == 0
    assert event.available_at <= datetime.now(UTC).replace(tzinfo=None)

    service = NotificationDeliveryService(providers={"good": SuccessfulProvider()})
    assert asyncio.run(service.process_pending(notification_db, user.id)) == 1
    notification_db.refresh(event)
    notification_db.refresh(delivery)
    assert event.status == "delivered"
    assert delivery.status == "succeeded" and delivery.attempts == 1


def test_disabled_channel_cannot_report_a_successful_test(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    channel = add_channel(notification_db, user, "disabled", "good")
    channel.enabled = False
    notification_db.commit()

    with pytest.raises(HTTPException) as caught:
        asyncio.run(send_test_channel(channel.id, user, notification_db))

    assert caught.value.status_code == 409
    assert caught.value.detail == "notification channel is disabled"
    assert notification_db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_update_channel_preserves_masked_secret(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    channel = NotificationChannel(
        user_id=user.id,
        name="telegram-channel",
        provider="telegram",
        encrypted_config=get_credential_cipher().encrypt_json(
            {"bot_token": "real-bot-token-value", "chat_id": "old-chat"}
        ),
    )
    notification_db.add(channel)
    notification_db.flush()
    notification_db.add(
        NotificationSubscription(
            user_id=user.id,
            channel_id=channel.id,
            event_type="new_charge",
        )
    )
    notification_db.commit()

    result = update_channel(
        channel.id,
        ChannelInput(
            name="renamed",
            provider="telegram",
            config={"bot_token": "***alue", "chat_id": "new-chat"},
            event_types=["cookie_expired"],
        ),
        user,
        notification_db,
    )

    assert result.name == "renamed"
    stored = notification_db.get(NotificationChannel, channel.id)
    assert stored is not None
    assert get_credential_cipher().decrypt_json(stored.encrypted_config) == {
        "bot_token": "real-bot-token-value",
        "chat_id": "new-chat",
    }
    assert result.event_types == ["cookie_expired"]


def test_subscription_rules_are_editable_and_tenant_scoped(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    own_channel = add_channel(notification_db, user, "own", "good")
    other_user = User(
        username="other-notifier",
        password_hash=hash_password("other-notifier-password-42"),
        role=UserRole.USER,
    )
    notification_db.add(other_user)
    notification_db.commit()
    other_channel = add_channel(notification_db, other_user, "other", "good")

    initial = list_subscriptions(user, notification_db)
    assert initial.channels[0].id == own_channel.id
    assert any(
        rule.event_type == "new_charge" and rule.channel_ids == [own_channel.id]
        for rule in initial.rules
    )

    updated = update_subscriptions(
        SubscriptionRulesInput(
            rules=[
                SubscriptionRuleInput(event_type="new_charge", channel_ids=[]),
                SubscriptionRuleInput(event_type="cookie_expired", channel_ids=[own_channel.id]),
            ]
        ),
        user,
        notification_db,
    )
    by_event = {rule.event_type: rule.channel_ids for rule in updated.rules}
    assert by_event["new_charge"] == []
    assert by_event["cookie_expired"] == [own_channel.id]

    with pytest.raises(HTTPException) as caught:
        update_subscriptions(
            SubscriptionRulesInput(
                rules=[
                    SubscriptionRuleInput(event_type="new_charge", channel_ids=[other_channel.id])
                ]
            ),
            user,
            notification_db,
        )
    assert caught.value.status_code == 422


def test_delivery_view_exposes_safe_event_context(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    channel = add_channel(notification_db, user, "visible", "good")
    event = enqueue_event(
        notification_db,
        user.id,
        "cookie_expired",
        "cookie:view",
        {
            "account": "佳佳的B站号",
            "account_uid": "123456",
            "reason": "login expired",
            "access_token": "must-not-be-returned",
        },
    )
    assert event is not None
    event.status = "failed"
    delivery = NotificationDelivery(
        user_id=user.id,
        outbox_id=event.id,
        channel_id=channel.id,
        status="failed",
        attempts=2,
        error_type="provider_rejected",
        response_summary="HTTP 400",
    )
    notification_db.add(delivery)
    notification_db.commit()

    view = delivery_view(delivery, event, channel)

    assert view.event_type == "cookie_expired"
    assert view.event_label == "Cookie 已失效"
    assert view.channel_name == "visible"
    assert view.account_name == "佳佳的B站号"
    assert view.account_uid == "123456"
    assert view.display_status == "failed"
    assert view.attempts == 2
    assert view.can_retry is True
    assert view.payload_summary["access_token"] == "[已隐藏]"
    assert "must-not-be-returned" not in str(view.payload_summary)


def test_delivery_filters_and_channel_delete_preserve_audit(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    channel = add_channel(notification_db, user, "audit", "good")
    event = enqueue_event(
        notification_db,
        user.id,
        "new_charge",
        "charge:audit",
        {"supporter": "Alice", "amount": "30.00"},
    )
    assert event is not None
    event.status = "failed"
    delivery = NotificationDelivery(
        user_id=user.id,
        outbox_id=event.id,
        channel_id=channel.id,
        status="failed",
        attempts=2,
        error_type="provider_rejected",
        response_summary="HTTP 503",
    )
    notification_db.add(delivery)
    notification_db.commit()

    failed = list_deliveries(user, notification_db, status_filter="failed")
    assert [item.id for item in failed] == [delivery.id]
    assert failed[0].event_summary == "Alice · 30.00 B币"

    delete_channel(channel.id, user, notification_db)
    notification_db.refresh(delivery)
    assert notification_db.get(NotificationChannel, channel.id) is None
    assert delivery.channel_id is None
    assert notification_db.get(NotificationDelivery, delivery.id) is not None
    assert list_subscriptions(user, notification_db).channels == []


def test_retry_rejects_success_and_cross_tenant_access(notification_db: Session) -> None:
    user = make_notification_user(notification_db)
    channel = add_channel(notification_db, user, "retry", "good")
    event = enqueue_event(
        notification_db,
        user.id,
        "new_charge",
        "charge:retry-guard",
        {"supporter": "Alice", "amount": "1.00"},
    )
    assert event is not None
    delivery = NotificationDelivery(
        user_id=user.id,
        outbox_id=event.id,
        channel_id=channel.id,
        status="succeeded",
        attempts=1,
    )
    notification_db.add(delivery)
    notification_db.commit()

    with pytest.raises(HTTPException) as success_error:
        retry_delivery(delivery.id, user, notification_db)
    assert success_error.value.status_code == 409
    assert "already succeeded" in success_error.value.detail

    other_user = User(
        username="retry-other",
        password_hash=hash_password("retry-other-password-42"),
        role=UserRole.USER,
    )
    notification_db.add(other_user)
    notification_db.commit()
    with pytest.raises(HTTPException) as tenant_error:
        retry_delivery(delivery.id, other_user, notification_db)
    assert tenant_error.value.status_code == 404


def test_channel_test_targets_only_requested_channel(
    notification_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_notification_user(notification_db)
    target = add_channel(notification_db, user, "target", "good")
    other = add_channel(notification_db, user, "other", "good")
    target_subscription = notification_db.scalar(
        select(NotificationSubscription).where(
            NotificationSubscription.channel_id == target.id,
            NotificationSubscription.event_type == "scheduled_job_failed",
        )
    )
    notification_db.add(
        NotificationSubscription(
            user_id=user.id,
            channel_id=other.id,
            event_type="scheduled_job_failed",
        )
    )
    notification_db.commit()

    class TargetedDeliveryService:
        target_channel_id: str | None = None

        async def deliver_event(
            self, db: Session, event: NotificationOutbox, channel_id: str | None = None
        ) -> None:
            self.target_channel_id = channel_id
            db.add(
                NotificationDelivery(
                    user_id=event.user_id,
                    outbox_id=event.id,
                    channel_id=channel_id,
                    status="succeeded",
                    attempts=1,
                    response_summary="HTTP 200",
                )
            )
            event.status = "delivered"
            db.commit()

        async def close(self) -> None:
            return None

    fake = TargetedDeliveryService()
    monkeypatch.setattr(notification_router, "NotificationDeliveryService", lambda: fake)
    result = asyncio.run(send_test_channel(target.id, user, notification_db))

    assert result.success is True
    assert fake.target_channel_id == target.id
    assert target_subscription is None
    deliveries = list(notification_db.scalars(select(NotificationDelivery)))
    assert len(deliveries) == 1
    assert deliveries[0].channel_id == target.id
