import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as app_main
import app.routers.notifications as notification_router
from app.crypto import get_credential_cipher
from app.database import get_db
from app.main import create_app
from app.models import (
    Base,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    User,
    UserRole,
)
from app.security import hash_password


@pytest.fixture
def notification_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    application = create_app()

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(app_main, "get_session_factory", lambda: factory)
    with TestClient(application) as client:
        setup = client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "owner-password-42"},
        )
        assert setup.status_code == 201
        client.headers.update(
            {
                "Origin": "http://testserver",
                "X-CSRF-Token": client.cookies["csrf_token"],
            }
        )
        yield client, factory
    engine.dispose()


def _channel_and_delivery(
    factory: sessionmaker[Session], *, username: str, status: str = "failed"
) -> tuple[str, str]:
    with factory() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user is not None
        channel = NotificationChannel(
            user_id=user.id,
            name=f"{username}-channel",
            provider="telegram",
            encrypted_config=get_credential_cipher().encrypt_json(
                {"bot_token": "other-bot-token", "chat_id": "9988"}
            ),
        )
        db.add(channel)
        db.flush()
        event = NotificationOutbox(
            user_id=user.id,
            event_type="cookie_expired",
            dedupe_key=f"http:{username}:{uuid.uuid4()}",
            payload={"account": "测试账号", "reason": "expired"},
            status="failed",
            attempts=1,
            available_at=datetime.now(UTC),
        )
        db.add(event)
        db.flush()
        delivery = NotificationDelivery(
            user_id=user.id,
            outbox_id=event.id,
            channel_id=channel.id,
            status=status,
            attempts=1,
            response_summary="HTTP 503",
            error_type="provider_rejected",
            available_at=datetime.now(UTC),
        )
        db.add(delivery)
        db.commit()
        return channel.id, delivery.id


def test_notification_http_workflow_and_audit_preservation(
    notification_http_client: tuple[TestClient, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = notification_http_client

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert all(label in dashboard.text for label in ("通知渠道", "通知规则", "发送记录"))

    catalog = client.get("/api/notifications/catalog")
    assert catalog.status_code == 200
    assert {item["id"] for item in catalog.json()["providers"]} == {
        "feishu",
        "telegram",
        "serverchan",
        "webhook",
    }
    assert catalog.json()["events"][0]["type"] == "new_charge"

    created = client.post(
        "/api/notifications/channels",
        json={
            "name": "我的 Telegram",
            "provider": "telegram",
            "config": {"bot_token": "real-bot-token-value", "chat_id": "1234"},
            "event_types": ["new_charge", "cookie_expired"],
        },
    )
    assert created.status_code == 201
    channel = created.json()
    channel_id = channel["id"]
    assert channel["config_masked"]["bot_token"] != "real-bot-token-value"
    assert channel["config_masked"]["chat_id"] == "1234"

    disabled = client.patch(f"/api/notifications/channels/{channel_id}/enabled?enabled=false")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = client.patch(f"/api/notifications/channels/{channel_id}/enabled?enabled=true")
    assert enabled.status_code == 200

    updated = client.put(
        f"/api/notifications/channels/{channel_id}",
        json={
            "name": "我的 Telegram 2",
            "provider": "telegram",
            "config": {"bot_token": "***alue", "chat_id": "5678"},
            "event_types": ["cookie_expired"],
            "enabled": True,
        },
    )
    assert updated.status_code == 200
    with factory() as db:
        stored = db.get(NotificationChannel, channel_id)
        assert stored is not None
        assert get_credential_cipher().decrypt_json(stored.encrypted_config) == {
            "bot_token": "real-bot-token-value",
            "chat_id": "5678",
        }

    class SuccessfulTestDelivery:
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
                    delivered_at=datetime.now(UTC),
                )
            )
            event.status = "delivered"
            db.commit()

        async def close(self) -> None:
            return None

    fake = SuccessfulTestDelivery()
    monkeypatch.setattr(notification_router, "NotificationDeliveryService", lambda: fake)
    tested = client.post(f"/api/notifications/channels/{channel_id}/test")
    assert tested.status_code == 200
    assert tested.json()["success"] is True
    assert fake.target_channel_id == channel_id
    test_delivery_id = tested.json()["delivery_id"]

    rules = client.get("/api/notifications/subscriptions")
    assert rules.status_code == 200
    assert rules.json()["channels"][0]["id"] == channel_id
    saved_rules = client.put(
        "/api/notifications/subscriptions",
        json={
            "rules": [
                {"event_type": "new_charge", "channel_ids": []},
                {"event_type": "cookie_expired", "channel_ids": [channel_id]},
            ]
        },
    )
    assert saved_rules.status_code == 200
    by_event = {item["event_type"]: item["channel_ids"] for item in saved_rules.json()["rules"]}
    assert by_event["new_charge"] == []
    assert by_event["cookie_expired"] == [channel_id]

    _own_channel_id, failed_delivery_id = _channel_and_delivery(factory, username="owner")
    deliveries = client.get("/api/notifications/deliveries?status=failed")
    assert deliveries.status_code == 200
    failed = next(item for item in deliveries.json() if item["id"] == failed_delivery_id)
    assert failed["event_label"] == "Cookie 已失效"
    assert failed["channel_name"] == "owner-channel"
    assert failed["status_label"] == "失败"
    assert failed["attempts"] == 1
    assert failed["can_retry"] is True
    detail = client.get(f"/api/notifications/deliveries/{failed_delivery_id}")
    assert detail.status_code == 200
    assert detail.json()["response_summary"] == "HTTP 503"
    assert "other-bot-token" not in str(detail.json())

    retry = client.post(f"/api/notifications/deliveries/{failed_delivery_id}/retry")
    assert retry.status_code == 202
    with factory() as db:
        retried = db.get(NotificationDelivery, failed_delivery_id)
        assert retried is not None and retried.status == "pending"

    successful_retry = client.post(f"/api/notifications/deliveries/{test_delivery_id}/retry")
    assert successful_retry.status_code == 409

    deleted = client.delete(f"/api/notifications/channels/{channel_id}")
    assert deleted.status_code == 204
    with factory() as db:
        preserved = db.get(NotificationDelivery, test_delivery_id)
        assert preserved is not None
        assert preserved.channel_id is None
    historical = client.get("/api/notifications/deliveries")
    assert historical.status_code == 200
    deleted_view = next(item for item in historical.json() if item["id"] == test_delivery_id)
    assert deleted_view["channel_id"] is None
    assert deleted_view["channel_name"] == "渠道已删除"


def test_notification_http_tenant_isolation(
    notification_http_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = notification_http_client
    with factory() as db:
        other = User(
            username="other-user",
            password_hash=hash_password("other-user-password-42"),
            role=UserRole.USER,
        )
        db.add(other)
        db.commit()
    other_channel_id, other_delivery_id = _channel_and_delivery(factory, username="other-user")

    assert (
        client.patch(
            f"/api/notifications/channels/{other_channel_id}/enabled?enabled=false"
        ).status_code
        == 404
    )
    assert client.post(f"/api/notifications/channels/{other_channel_id}/test").status_code == 404
    assert client.delete(f"/api/notifications/channels/{other_channel_id}").status_code == 404
    assert client.get(f"/api/notifications/deliveries/{other_delivery_id}").status_code == 404
    assert (
        client.post(f"/api/notifications/deliveries/{other_delivery_id}/retry").status_code == 404
    )
