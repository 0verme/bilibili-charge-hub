import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.auth import AdminUser, CurrentUser, DbSession
from app.crypto import get_credential_cipher
from app.models import (
    JobKind,
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
    ScheduleJob,
)
from app.notifications.providers import FORBIDDEN_HEADERS, validate_webhook_url
from app.notifications.service import (
    EVENT_TYPES,
    NotificationDeliveryService,
    enqueue_event,
    reset_delivery_for_retry,
)
from app.services.reconciliation import NotificationReconciliationService
from app.services.scheduler import SchedulerManager

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
ProviderName = Literal["feishu", "telegram", "serverchan", "webhook"]


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider: ProviderName
    config: dict
    event_types: list[str]
    enabled: bool = True

    @field_validator("event_types")
    @classmethod
    def validate_events(cls, values: list[str]) -> list[str]:
        if not values or not set(values) <= EVENT_TYPES:
            raise ValueError("event_types contains an unsupported event")
        return sorted(set(values))


class ChannelView(BaseModel):
    id: str
    name: str
    provider: str
    enabled: bool
    config_masked: dict
    event_types: list[str]


class DeliveryView(BaseModel):
    id: str
    channel_id: str
    status: str
    attempts: int
    response_summary: str | None
    error_type: str | None

    model_config = {"from_attributes": True}


def validate_channel_config(provider: str, config: dict) -> None:
    required = {
        "feishu": {"webhook_url"},
        "telegram": {"bot_token", "chat_id"},
        "serverchan": {"send_key"},
        "webhook": {"url"},
    }[provider]
    if not required <= config.keys() or any(not str(config[key]).strip() for key in required):
        raise ValueError("notification config is incomplete")
    if provider == "feishu":
        validate_webhook_url(str(config["webhook_url"]))
    if provider == "webhook":
        validate_webhook_url(str(config["url"]))
        if str(config.get("method", "POST")).upper() not in {"POST", "PUT", "PATCH"}:
            raise ValueError("webhook method is not allowed")
        headers = config.get("headers") or {}
        if not isinstance(headers, dict) or FORBIDDEN_HEADERS & {
            str(key).lower() for key in headers
        }:
            raise ValueError("webhook headers are invalid")


def mask_config(config: dict) -> dict:
    masked = {}
    for key, value in config.items():
        if isinstance(value, str):
            masked[key] = "***" + value[-4:] if len(value) > 4 else "***"
        elif isinstance(value, dict):
            masked[key] = {str(item): "***" for item in value}
        else:
            masked[key] = "***"
    return masked


def channel_view(db: DbSession, channel: NotificationChannel) -> ChannelView:
    config = get_credential_cipher().decrypt_json(channel.encrypted_config)
    events = list(
        db.scalars(
            select(NotificationSubscription.event_type).where(
                NotificationSubscription.channel_id == channel.id,
                NotificationSubscription.user_id == channel.user_id,
                NotificationSubscription.enabled.is_(True),
            )
        )
    )
    return ChannelView(
        id=channel.id,
        name=channel.name,
        provider=channel.provider,
        enabled=channel.enabled,
        config_masked=mask_config(config),
        event_types=events,
    )


def get_channel(db: DbSession, user_id: str, channel_id: str) -> NotificationChannel:
    channel = db.scalar(select(NotificationChannel).where(
        NotificationChannel.id == channel_id, NotificationChannel.user_id == user_id
    ))
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification channel not found")
    return channel


def get_scheduler(request: Request) -> SchedulerManager:
    return request.app.state.scheduler


SchedulerDep = Annotated[SchedulerManager, Depends(get_scheduler)]


@router.post("/reconcile")
def reconcile_notifications(admin: AdminUser, db: DbSession) -> dict:
    """Manually trigger one notification reconciliation pass (admin only).

    CSRF and same-origin protection are enforced by the global browser security
    middleware for every authenticated write request; the service itself also
    refuses to overlap with a concurrently running scheduled pass.
    """
    service = NotificationReconciliationService()
    summary = service.run(db)
    return summary.to_dict()


@router.get("/channels", response_model=list[ChannelView])
def list_channels(user: CurrentUser, db: DbSession) -> list[ChannelView]:
    channels = db.scalars(
        select(NotificationChannel)
        .where(NotificationChannel.user_id == user.id)
        .order_by(NotificationChannel.created_at)
    )
    return [channel_view(db, channel) for channel in channels]


@router.post("/channels", response_model=ChannelView, status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: ChannelInput,
    user: CurrentUser,
    db: DbSession,
    scheduler: SchedulerDep,
) -> ChannelView:
    try:
        validate_channel_config(payload.provider, payload.config)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    channel = NotificationChannel(
        user_id=user.id,
        name=payload.name,
        provider=payload.provider,
        encrypted_config=get_credential_cipher().encrypt_json(payload.config),
        enabled=payload.enabled,
    )
    db.add(channel)
    db.flush()
    db.add_all(
        NotificationSubscription(user_id=user.id, channel_id=channel.id, event_type=event)
        for event in payload.event_types
    )
    retry_job = db.scalar(
        select(ScheduleJob).where(
            ScheduleJob.user_id == user.id,
            ScheduleJob.kind == JobKind.NOTIFICATION_RETRY,
        )
    )
    if retry_job is None:
        retry_job = ScheduleJob(
            user_id=user.id,
            kind=JobKind.NOTIFICATION_RETRY,
            trigger_type="interval",
            trigger_config={"seconds": 60},
        )
        db.add(retry_job)
        db.flush()
        scheduler.sync_job(retry_job, db)
    db.commit()
    db.refresh(channel)
    return channel_view(db, channel)


@router.put("/channels/{channel_id}", response_model=ChannelView)
def update_channel(
    channel_id: str, payload: ChannelInput, user: CurrentUser, db: DbSession
) -> ChannelView:
    channel = get_channel(db, user.id, channel_id)
    try:
        validate_channel_config(payload.provider, payload.config)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    channel.name = payload.name
    channel.provider = payload.provider
    channel.encrypted_config = get_credential_cipher().encrypt_json(payload.config)
    channel.enabled = payload.enabled
    for subscription in db.scalars(select(NotificationSubscription).where(
        NotificationSubscription.channel_id == channel.id,
        NotificationSubscription.user_id == user.id,
    )):
        db.delete(subscription)
    db.flush()
    db.add_all(NotificationSubscription(user_id=user.id, channel_id=channel.id, event_type=event)
               for event in payload.event_types)
    db.commit()
    return channel_view(db, channel)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(channel_id: str, user: CurrentUser, db: DbSession) -> None:
    channel = get_channel(db, user.id, channel_id)
    db.delete(channel)
    db.commit()


@router.patch("/channels/{channel_id}/enabled", response_model=ChannelView)
def set_channel_enabled(
    channel_id: str,
    enabled: bool,
    user: CurrentUser,
    db: DbSession,
) -> ChannelView:
    channel = db.scalar(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.user_id == user.id,
        )
    )
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification channel not found")
    channel.enabled = enabled
    db.commit()
    return channel_view(db, channel)


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, user: CurrentUser, db: DbSession) -> dict[str, str]:
    channel = db.scalar(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.user_id == user.id,
        )
    )
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification channel not found")
    if not channel.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "notification channel is disabled")
    event = enqueue_event(
        db,
        user.id,
        "scheduled_job_failed",
        f"test:{channel.id}:{uuid.uuid4()}",
        {"message": "这是一条测试消息"},
    )
    assert event is not None
    db.flush()
    existing = db.scalar(
        select(NotificationSubscription.id).where(
            NotificationSubscription.channel_id == channel.id,
            NotificationSubscription.event_type == "scheduled_job_failed",
        )
    )
    temporary = None
    if existing is None:
        temporary = NotificationSubscription(
                user_id=user.id,
                channel_id=channel.id,
                event_type="scheduled_job_failed",
            )
        db.add(temporary)
        db.flush()
    delivery = NotificationDeliveryService()
    try:
        await delivery.deliver_event(db, event)
    finally:
        await delivery.close()
    if temporary is not None:
        db.delete(temporary)
        db.commit()
    return {"status": event.status}


@router.get("/deliveries", response_model=list[DeliveryView])
def list_deliveries(user: CurrentUser, db: DbSession) -> list[NotificationDelivery]:
    return list(
        db.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.user_id == user.id)
            .order_by(NotificationDelivery.id.desc())
            .limit(100)
        )
    )


@router.post("/deliveries/{delivery_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_delivery(delivery_id: str, user: CurrentUser, db: DbSession) -> dict[str, str]:
    delivery = db.scalar(select(NotificationDelivery).where(
        NotificationDelivery.id == delivery_id, NotificationDelivery.user_id == user.id
    ))
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification delivery not found")
    from datetime import UTC, datetime

    from app.models import NotificationOutbox

    event = db.get(NotificationOutbox, delivery.outbox_id)
    if event is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "notification event no longer exists")
    if event.status == "merged":
        raise HTTPException(status.HTTP_409_CONFLICT, "notification event was merged")
    reset_delivery_for_retry(delivery, event, datetime.now(UTC))
    db.commit()
    return {"status": "queued"}
