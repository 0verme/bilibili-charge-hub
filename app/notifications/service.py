import hashlib
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import get_credential_cipher
from app.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
)
from app.notifications.providers import NotificationProvider, provider_registry

EVENT_TYPES = {
    "new_charge",
    "collection_failed",
    "cookie_expired",
    "coupon_claim_succeeded",
    "coupon_claim_failed",
    "scheduled_job_failed",
}
MAX_ATTEMPTS = 5


def enqueue_event(
    db: Session,
    user_id: str,
    event_type: str,
    dedupe_key: str,
    payload: dict,
) -> NotificationOutbox | None:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported notification event: {event_type}")
    tenant_key = hashlib.sha256(f"{user_id}|{dedupe_key}".encode()).hexdigest()
    event = NotificationOutbox(
        user_id=user_id,
        event_type=event_type,
        dedupe_key=tenant_key,
        payload=payload,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        return event
    except IntegrityError:
        return None


def render_message(event: NotificationOutbox) -> str:
    title = {
        "new_charge": "收到新的充电",
        "collection_failed": "充电记录采集失败",
        "cookie_expired": "B 站登录状态已失效",
        "coupon_claim_succeeded": "B 币券领取成功",
        "coupon_claim_failed": "B 币券领取失败",
        "scheduled_job_failed": "定时任务执行异常",
    }[event.event_type]
    details = " · ".join(f"{key}: {value}" for key, value in event.payload.items())
    return f"{title}\n{details}" if details else title


class NotificationDeliveryService:
    def __init__(
        self,
        providers: dict[str, NotificationProvider] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.owns_client = providers is None and client is None
        self.client = client
        if providers is not None:
            self.providers = providers
        else:
            self.client = client or httpx.AsyncClient(follow_redirects=False)
            self.providers = provider_registry(self.client)

    async def close(self) -> None:
        if self.owns_client and self.client is not None:
            await self.client.aclose()

    async def process_pending(self, db: Session, user_id: str | None = None) -> int:
        query = select(NotificationOutbox).where(
            NotificationOutbox.status.in_(["pending", "retry"]),
            NotificationOutbox.available_at <= datetime.now(UTC),
            NotificationOutbox.attempts < MAX_ATTEMPTS,
        )
        if user_id:
            query = query.where(NotificationOutbox.user_id == user_id)
        processed = 0
        for event in db.scalars(query.order_by(NotificationOutbox.created_at).limit(100)):
            await self.deliver_event(db, event)
            processed += 1
        return processed

    async def deliver_event(self, db: Session, event: NotificationOutbox) -> None:
        subscriptions = db.execute(
            select(NotificationSubscription, NotificationChannel)
            .join(
                NotificationChannel,
                NotificationChannel.id == NotificationSubscription.channel_id,
            )
            .where(
                NotificationSubscription.user_id == event.user_id,
                NotificationSubscription.event_type == event.event_type,
                NotificationSubscription.enabled.is_(True),
                NotificationChannel.user_id == event.user_id,
                NotificationChannel.enabled.is_(True),
            )
        ).all()
        successes = failures = 0
        for _subscription, channel in subscriptions:
            delivery = db.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.outbox_id == event.id,
                    NotificationDelivery.channel_id == channel.id,
                )
            )
            if delivery and delivery.status == "succeeded":
                successes += 1
                continue
            if delivery and delivery.available_at.replace(tzinfo=UTC) > datetime.now(UTC):
                failures += 1
                continue
            delivery = delivery or NotificationDelivery(
                user_id=event.user_id,
                outbox_id=event.id,
                channel_id=channel.id,
            )
            db.add(delivery)
            delivery.attempts = (delivery.attempts or 0) + 1
            try:
                config = get_credential_cipher().decrypt_json(channel.encrypted_config)
                provider = self.providers[channel.provider]
                result = await provider.send(render_message(event), config)
                delivery.status = "succeeded" if result.success else "failed"
                delivery.response_summary = result.detail[:500]
                delivery.error_type = None if result.success else "provider_rejected"
            except Exception as exc:
                delivery.status = "failed"
                delivery.error_type = type(exc).__name__[:64]
                delivery.response_summary = f"{type(exc).__name__}: {str(exc)[:300]}"
            if delivery.status == "succeeded":
                delivery.delivered_at = datetime.now(UTC)
                successes += 1
            else:
                failures += 1
                delivery.available_at = datetime.now(UTC) + timedelta(
                    seconds=2**delivery.attempts * 30
                )
        event.attempts += 1
        if not subscriptions or failures == 0:
            event.status = "delivered"
        elif event.attempts >= MAX_ATTEMPTS:
            event.status = "failed"
        else:
            event.status = "retry"
            event.available_at = datetime.now(UTC) + timedelta(seconds=2**event.attempts * 30)
        db.commit()
