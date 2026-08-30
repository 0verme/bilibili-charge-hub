import hashlib
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import get_credential_cipher
from app.models import (
    ChargeRecord,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
)
from app.notifications.providers import NotificationProvider, provider_registry
from app.settings import get_settings

EVENT_TYPES = {
    "new_charge",
    "collection_failed",
    "cookie_expired",
    "coupon_claim_succeeded",
    "coupon_claim_failed",
    "scheduled_job_failed",
    "daily_task_succeeded",
    "daily_task_failed",
}
MAX_ATTEMPTS = 5


def reset_delivery_for_retry(
    delivery: NotificationDelivery,
    event: NotificationOutbox,
    now: datetime | None = None,
) -> None:
    """Reset both retry budgets so a manually retried terminal event is selectable again."""
    available_at = now or datetime.now(UTC)
    delivery.status = "pending"
    delivery.attempts = 0
    delivery.available_at = available_at
    delivery.delivered_at = None
    delivery.error_type = None
    delivery.response_summary = None
    event.status = "retry"
    event.attempts = 0
    event.available_at = available_at


def tenant_dedupe_key(user_id: str, dedupe_key: str) -> str:
    """Scoped dedupe key that keeps identical event keys isolated per tenant."""
    return hashlib.sha256(f"{user_id}|{dedupe_key}".encode()).hexdigest()


def enqueue_event(
    db: Session,
    user_id: str,
    event_type: str,
    dedupe_key: str,
    payload: dict,
) -> NotificationOutbox | None:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported notification event: {event_type}")
    event = NotificationOutbox(
        user_id=user_id,
        event_type=event_type,
        dedupe_key=tenant_dedupe_key(user_id, dedupe_key),
        payload=payload,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        return event
    except IntegrityError:
        return None


def new_charge_payload(record: ChargeRecord) -> dict[str, str]:
    """Build the new_charge event payload exactly as the collection flow does.

    Reconciliation must reuse this builder so recovered notifications render with
    the same template as freshly collected ones.
    """
    return {
        "supporter": record.supporter_name,
        "amount": str(record.amount),
        "brokerage": str(record.brokerage),
        "charged_at": record.charged_at.isoformat(),
    }


def render_message(event: NotificationOutbox) -> str:
    if event.event_type == "new_charge":
        supporter = str(event.payload.get("supporter") or "未知用户")
        amount = str(event.payload.get("amount", "0"))
        brokerage = str(event.payload.get("brokerage", "0"))
        charged_at = str(event.payload.get("charged_at") or "未知时间")
        try:
            parsed = datetime.fromisoformat(charged_at.replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=parsed.tzinfo or UTC)
            charged_at = parsed.astimezone(ZoneInfo(get_settings().app_timezone)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            pass
        return f"【{supporter}】 在 【{charged_at}】\n冲了 {amount}B币 实际到账 {brokerage} 元"
    title = {
        "collection_failed": "充电记录采集失败",
        "cookie_expired": "B 站登录状态已失效",
        "coupon_claim_succeeded": "B 币券领取成功",
        "coupon_claim_failed": "B 币券领取失败",
        "scheduled_job_failed": "定时任务执行异常",
        "daily_task_succeeded": "每日任务完成",
        "daily_task_failed": "每日任务失败",
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
        """Deliver pending events; kept as an int API for callers that only need a count."""
        summary = await self.process_pending_summary(db, user_id)
        return cast(int, summary["scanned"])

    async def process_pending_summary(
        self, db: Session, user_id: str | None = None
    ) -> dict[str, object]:
        query = select(NotificationOutbox).where(
            NotificationOutbox.status.in_(["pending", "retry"]),
            NotificationOutbox.available_at <= datetime.now(UTC),
            NotificationOutbox.attempts < MAX_ATTEMPTS,
        )
        if user_id:
            query = query.where(NotificationOutbox.user_id == user_id)
        events = list(db.scalars(query.order_by(NotificationOutbox.created_at).limit(100)))
        succeeded = still_failed = budget_exceeded = 0
        outbox_ids: list[str] = []
        for event in events:
            before = event.attempts
            await self.deliver_event(db, event)
            outbox_ids.append(event.id)
            if event.status == "delivered":
                succeeded += 1
            elif event.status == "failed":
                still_failed += 1
                if event.attempts >= MAX_ATTEMPTS and before < MAX_ATTEMPTS:
                    budget_exceeded += 1
        return {
            "scanned": len(events),
            "retry_eligible": len(events),
            "retried": len(events),
            "succeeded": succeeded,
            "still_failed": still_failed,
            "retry_budget_exceeded": budget_exceeded,
            "skipped": 0,
            "outbox_ids": outbox_ids,
        }

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
        if not subscriptions:
            event.status = "pending"
            db.commit()
            return
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
        if failures == 0:
            event.status = "delivered"
        elif event.attempts >= MAX_ATTEMPTS:
            event.status = "failed"
        else:
            event.status = "retry"
            event.available_at = datetime.now(UTC) + timedelta(seconds=2**event.attempts * 30)
        db.commit()
