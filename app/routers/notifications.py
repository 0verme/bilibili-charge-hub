from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, delete, func, or_, select, update

from app.auth import AdminUser, CurrentUser, DbSession
from app.crypto import get_credential_cipher
from app.models import (
    JobKind,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
    ScheduleJob,
)
from app.notifications.providers import FORBIDDEN_HEADERS, validate_webhook_url
from app.notifications.service import (
    EVENT_TYPES,
    MAX_ATTEMPTS,
    SUCCESS_DELIVERY_STATUSES,
    NotificationDeliveryService,
    enqueue_event,
    reset_delivery_for_retry,
)
from app.services.reconciliation import NotificationReconciliationService
from app.services.scheduler import SchedulerManager

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
ProviderName = Literal["feishu", "telegram", "serverchan", "webhook"]

EVENT_TYPE_ORDER = (
    "new_charge",
    "collection_failed",
    "cookie_expired",
    "coupon_claim_succeeded",
    "coupon_claim_failed",
    "scheduled_job_failed",
    "daily_task_succeeded",
    "daily_task_failed",
)
EVENT_LABELS = {
    "new_charge": "新充电",
    "collection_failed": "采集失败",
    "cookie_expired": "Cookie 已失效",
    "coupon_claim_succeeded": "优惠券领取成功",
    "coupon_claim_failed": "优惠券领取失败",
    "scheduled_job_failed": "定时任务失败",
    "daily_task_succeeded": "每日任务成功",
    "daily_task_failed": "每日任务失败",
}
EVENT_DESCRIPTIONS = {
    "new_charge": "收到新的充电记录",
    "collection_failed": "充电记录采集异常",
    "cookie_expired": "B 站登录状态需要重新绑定",
    "coupon_claim_succeeded": "每月 B 币券领取成功",
    "coupon_claim_failed": "每月 B 币券领取失败",
    "scheduled_job_failed": "定时任务执行异常",
    "daily_task_succeeded": "每日任务有新的完成结果",
    "daily_task_failed": "每日任务执行失败或未完成",
}
PROVIDER_CATALOG = (
    {
        "id": "feishu",
        "name": "飞书",
        "description": "飞书机器人",
        "icon": "🪽",
        "fields": (
            {
                "key": "webhook_url",
                "label": "Webhook URL",
                "type": "url",
                "required": True,
                "secret": True,
                "placeholder": "https://open.feishu.cn/...",
                "help": "仅支持公开的 HTTP(S) 地址",
            },
        ),
    },
    {
        "id": "telegram",
        "name": "Telegram",
        "description": "Bot 消息",
        "icon": "✈",
        "fields": (
            {
                "key": "bot_token",
                "label": "Bot Token",
                "type": "password",
                "required": True,
                "secret": True,
                "placeholder": "输入 Telegram Bot Token",
                "help": "保存后不会完整回显",
            },
            {
                "key": "chat_id",
                "label": "Chat ID",
                "type": "text",
                "required": True,
                "secret": False,
                "placeholder": "例如 123456789",
                "help": "目标会话或群组 ID",
            },
        ),
    },
    {
        "id": "serverchan",
        "name": "Server酱",
        "description": "微信通知",
        "icon": "📣",
        "fields": (
            {
                "key": "send_key",
                "label": "SendKey",
                "type": "password",
                "required": True,
                "secret": True,
                "placeholder": "输入 Server酱 SendKey",
                "help": "保存后不会完整回显",
            },
        ),
    },
    {
        "id": "webhook",
        "name": "Webhook",
        "description": "自定义 HTTP",
        "icon": "🔗",
        "fields": (
            {
                "key": "url",
                "label": "请求 URL",
                "type": "url",
                "required": True,
                "secret": True,
                "placeholder": "https://example.com/notify",
                "help": "发送前会再次校验 DNS 公网地址",
            },
            {
                "key": "method",
                "label": "请求方法",
                "type": "select",
                "required": False,
                "secret": False,
                "placeholder": "",
                "help": "允许 POST、PUT 或 PATCH",
                "options": ("POST", "PUT", "PATCH"),
            },
            {
                "key": "headers",
                "label": "请求头 JSON（可选）",
                "type": "json",
                "required": False,
                "secret": True,
                "placeholder": '{"X-Notify-Type":"charge"}',
                "help": "保存后不会回显值；填写后会替换原请求头",
            },
            {
                "key": "json_template",
                "label": "JSON 模板（可选）",
                "type": "json",
                "required": False,
                "secret": True,
                "placeholder": '{"message":"{{message}}"}',
                "help": "使用 {{message}} 插入通知文本",
            },
        ),
    },
)
PROVIDER_LABELS = {item["id"]: item["name"] for item in PROVIDER_CATALOG}
SECRET_CONFIG_KEYS = frozenset(
    {"webhook_url", "bot_token", "send_key", "url", "headers", "json_template"}
)
SENSITIVE_KEY_PARTS = ("token", "secret", "password", "cookie", "authorization")
RETRYING_STATUSES = {"retry", "retrying"}
STATUS_LABELS = {
    "succeeded": "成功",
    "failed": "失败",
    "pending": "等待发送",
    "retrying": "等待重试",
    "merged": "已合并",
    "unknown": "未知状态",
}


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
        return sorted(set(values), key=_event_sort_key)


class ChannelView(BaseModel):
    id: str
    name: str
    provider: str
    provider_name: str
    enabled: bool
    config_masked: dict
    event_types: list[str]


class ProviderFieldView(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    secret: bool
    placeholder: str = ""
    help: str = ""
    options: list[str] = Field(default_factory=list)


class ProviderView(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    fields: list[ProviderFieldView]


class EventTypeView(BaseModel):
    type: str
    label: str
    description: str


class NotificationCatalogView(BaseModel):
    providers: list[ProviderView]
    events: list[EventTypeView]
    max_attempts: int


class SubscriptionRuleInput(BaseModel):
    event_type: str
    channel_ids: list[str] = Field(default_factory=list)


class SubscriptionRulesInput(BaseModel):
    rules: list[SubscriptionRuleInput]


class SubscriptionChannelView(BaseModel):
    id: str
    name: str
    provider: str
    provider_name: str
    enabled: bool


class SubscriptionRuleView(BaseModel):
    event_type: str
    channel_ids: list[str]


class SubscriptionRulesView(BaseModel):
    events: list[EventTypeView]
    channels: list[SubscriptionChannelView]
    rules: list[SubscriptionRuleView]


class DeliveryView(BaseModel):
    id: str
    delivery_id: str
    notification_id: str | None
    outbox_id: str | None
    channel_id: str | None
    channel_name: str
    channel_provider: str | None
    channel_provider_name: str
    event_type: str | None
    event_label: str
    event_summary: str
    account_name: str | None
    account_uid: str | None
    status: str
    display_status: str
    status_label: str
    attempts: int
    max_attempts: int
    response_summary: str | None
    error_type: str | None
    error_summary: str | None
    notification_created_at: datetime | None
    activity_at: datetime | None
    available_at: datetime | None
    delivered_at: datetime | None
    can_retry: bool
    merged_into_outbox_id: str | None
    payload_summary: dict[str, object]


class ChannelTestView(BaseModel):
    success: bool
    status: str
    status_label: str
    detail: str | None = None
    delivery_id: str | None = None


def _event_sort_key(event_type: str) -> int:
    try:
        return EVENT_TYPE_ORDER.index(event_type)
    except ValueError:
        return len(EVENT_TYPE_ORDER)


def provider_label(provider: str | None) -> str:
    if not provider:
        return "渠道已删除"
    return PROVIDER_LABELS.get(provider, provider)


def event_label(event_type: str | None) -> str:
    if not event_type:
        return "通知事件已清理"
    return EVENT_LABELS.get(event_type, event_type)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower()
    return normalized in SECRET_CONFIG_KEYS or any(
        part in normalized for part in SENSITIVE_KEY_PARTS
    )


def _looks_masked(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith("***")
    if isinstance(value, dict):
        return bool(value) and all(_looks_masked(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and all(_looks_masked(item) for item in value)
    return False


def _mask_value(value: object) -> object:
    if isinstance(value, str):
        return "***" + value[-4:] if len(value) > 4 else "***"
    if isinstance(value, dict):
        return {str(key): "***" for key in value}
    if isinstance(value, list):
        return ["***"]
    return "***"


def mask_config(config: dict) -> dict:
    masked = {}
    for key, value in config.items():
        masked[key] = _mask_value(value) if _is_sensitive_key(key) else value
    return masked


def _masked_config_values(config: dict) -> bool:
    return any(_is_sensitive_key(key) and _looks_masked(value) for key, value in config.items())


def validate_channel_config(provider: str, config: dict) -> None:
    required_by_provider = {
        "feishu": {"webhook_url"},
        "telegram": {"bot_token", "chat_id"},
        "serverchan": {"send_key"},
        "webhook": {"url"},
    }
    required = required_by_provider.get(provider)
    if required is None:
        raise ValueError("unsupported notification provider")
    if not required <= config.keys() or any(not str(config[key]).strip() for key in required):
        raise ValueError("notification config is incomplete")
    if _masked_config_values(config):
        raise ValueError("secret config must be replaced instead of masked text")
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
        template = config.get("json_template")
        if template is not None and not isinstance(template, (dict, list)):
            raise ValueError("webhook JSON template must be an object or array")


def _merge_channel_config(channel: NotificationChannel, provider: str, submitted: dict) -> dict:
    existing = get_credential_cipher().decrypt_json(channel.encrypted_config)
    if provider != channel.provider:
        return submitted
    merged = dict(existing)
    for key, value in submitted.items():
        if _is_sensitive_key(key) and _looks_masked(value):
            continue
        merged[key] = value
    return merged


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
    events.sort(key=_event_sort_key)
    return ChannelView(
        id=channel.id,
        name=channel.name,
        provider=channel.provider,
        provider_name=provider_label(channel.provider),
        enabled=channel.enabled,
        config_masked=mask_config(config),
        event_types=events,
    )


def get_channel(db: DbSession, user_id: str, channel_id: str) -> NotificationChannel:
    channel = db.scalar(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.user_id == user_id,
        )
    )
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification channel not found")
    return channel


def get_scheduler(request: Request) -> SchedulerManager:
    return request.app.state.scheduler


SchedulerDep = Annotated[SchedulerManager, Depends(get_scheduler)]


@router.post("/reconcile")
def reconcile_notifications(admin: AdminUser, db: DbSession) -> dict:
    """Manually trigger one notification reconciliation pass (admin only)."""
    service = NotificationReconciliationService()
    summary = service.run(db)
    return summary.to_dict()


@router.get("/catalog", response_model=NotificationCatalogView)
def notification_catalog(user: CurrentUser) -> NotificationCatalogView:
    del user
    providers = []
    for item in PROVIDER_CATALOG:
        fields = [
            ProviderFieldView(
                key=field["key"],
                label=field["label"],
                type=field["type"],
                required=field["required"],
                secret=field["secret"],
                placeholder=field.get("placeholder", ""),
                help=field.get("help", ""),
                options=list(field.get("options", ())),
            )
            for field in item["fields"]
        ]
        providers.append(
            ProviderView(
                id=item["id"],
                name=item["name"],
                description=item["description"],
                icon=item["icon"],
                fields=fields,
            )
        )
    return NotificationCatalogView(
        providers=providers,
        events=[
            EventTypeView(
                type=event_type,
                label=EVENT_LABELS.get(event_type, event_type),
                description=EVENT_DESCRIPTIONS.get(event_type, "通知事件"),
            )
            for event_type in EVENT_TYPE_ORDER
            if event_type in EVENT_TYPES
        ],
        max_attempts=MAX_ATTEMPTS,
    )


@router.get("/channels", response_model=list[ChannelView])
def list_channels(user: CurrentUser, db: DbSession) -> list[ChannelView]:
    channels = db.scalars(
        select(NotificationChannel)
        .where(NotificationChannel.user_id == user.id)
        .order_by(NotificationChannel.created_at)
    )
    return [channel_view(db, channel) for channel in channels]


def _ensure_retry_job(db: DbSession, user_id: str, scheduler: SchedulerManager) -> None:
    retry_job = db.scalar(
        select(ScheduleJob).where(
            ScheduleJob.user_id == user_id,
            ScheduleJob.kind == JobKind.NOTIFICATION_RETRY,
        )
    )
    if retry_job is not None:
        return
    retry_job = ScheduleJob(
        user_id=user_id,
        kind=JobKind.NOTIFICATION_RETRY,
        trigger_type="interval",
        trigger_config={"seconds": 60},
    )
    db.add(retry_job)
    db.flush()
    scheduler.sync_job(retry_job, db)


def _replace_channel_subscriptions(
    db: DbSession, user_id: str, channel_id: str, event_types: list[str]
) -> None:
    selected = set(event_types)
    subscriptions = list(
        db.scalars(
            select(NotificationSubscription).where(
                NotificationSubscription.channel_id == channel_id,
                NotificationSubscription.user_id == user_id,
            )
        )
    )
    by_event = {subscription.event_type: subscription for subscription in subscriptions}
    for subscription in subscriptions:
        subscription.enabled = subscription.event_type in selected
    for event_type in selected - by_event.keys():
        db.add(
            NotificationSubscription(
                user_id=user_id,
                channel_id=channel_id,
                event_type=event_type,
                enabled=True,
            )
        )


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
    _replace_channel_subscriptions(db, user.id, channel.id, payload.event_types)
    _ensure_retry_job(db, user.id, scheduler)
    db.commit()
    db.refresh(channel)
    return channel_view(db, channel)


@router.put("/channels/{channel_id}", response_model=ChannelView)
def update_channel(
    channel_id: str, payload: ChannelInput, user: CurrentUser, db: DbSession
) -> ChannelView:
    channel = get_channel(db, user.id, channel_id)
    config = _merge_channel_config(channel, payload.provider, payload.config)
    try:
        validate_channel_config(payload.provider, config)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    channel.name = payload.name
    channel.provider = payload.provider
    channel.encrypted_config = get_credential_cipher().encrypt_json(config)
    channel.enabled = payload.enabled
    _replace_channel_subscriptions(db, user.id, channel.id, payload.event_types)
    db.commit()
    return channel_view(db, channel)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(channel_id: str, user: CurrentUser, db: DbSession) -> None:
    channel = get_channel(db, user.id, channel_id)
    # Delivery is an audit trail. Null the optional channel reference before the
    # channel delete so both SQLite and PostgreSQL keep historical deliveries.
    db.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.channel_id == channel.id,
            NotificationDelivery.user_id == user.id,
        )
        .values(channel_id=None)
    )
    db.execute(
        delete(NotificationSubscription).where(
            NotificationSubscription.channel_id == channel.id,
            NotificationSubscription.user_id == user.id,
        )
    )
    db.delete(channel)
    db.commit()


@router.patch("/channels/{channel_id}/enabled", response_model=ChannelView)
def set_channel_enabled(
    channel_id: str,
    enabled: bool,
    user: CurrentUser,
    db: DbSession,
) -> ChannelView:
    channel = get_channel(db, user.id, channel_id)
    channel.enabled = enabled
    db.commit()
    return channel_view(db, channel)


@router.post("/channels/{channel_id}/test", response_model=ChannelTestView)
async def test_channel(channel_id: str, user: CurrentUser, db: DbSession) -> ChannelTestView:
    channel = get_channel(db, user.id, channel_id)
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
    subscription = db.scalar(
        select(NotificationSubscription).where(
            NotificationSubscription.user_id == user.id,
            NotificationSubscription.channel_id == channel.id,
            NotificationSubscription.event_type == "scheduled_job_failed",
        )
    )
    temporary = None
    restore_disabled = False
    if subscription is None:
        temporary = NotificationSubscription(
            user_id=user.id,
            channel_id=channel.id,
            event_type="scheduled_job_failed",
        )
        db.add(temporary)
        db.flush()
    elif not subscription.enabled:
        subscription.enabled = True
        restore_disabled = True
    delivery_service = NotificationDeliveryService()
    try:
        await delivery_service.deliver_event(db, event, channel_id=channel.id)
    finally:
        await delivery_service.close()
    if temporary is not None:
        db.delete(temporary)
    if restore_disabled and subscription is not None:
        subscription.enabled = False
    db.commit()
    delivery = db.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.user_id == user.id,
            NotificationDelivery.outbox_id == event.id,
            NotificationDelivery.channel_id == channel.id,
        )
    )
    success = delivery is not None and (delivery.status or "").lower() in SUCCESS_DELIVERY_STATUSES
    raw_status = delivery.status if delivery is not None else event.status
    display_status = delivery_display_status(delivery, event) if delivery is not None else "unknown"
    return ChannelTestView(
        success=success,
        status=raw_status,
        status_label=STATUS_LABELS.get(display_status, "未知状态"),
        detail=(delivery.response_summary or delivery.error_type) if delivery else "未生成投递记录",
        delivery_id=delivery.id if delivery else None,
    )


def _delivery_query(user_id: str):
    timeline = func.coalesce(
        NotificationDelivery.delivered_at,
        NotificationOutbox.created_at,
        NotificationDelivery.available_at,
    )
    return (
        select(NotificationDelivery, NotificationOutbox, NotificationChannel)
        .select_from(NotificationDelivery)
        .outerjoin(
            NotificationOutbox,
            and_(
                NotificationOutbox.id == NotificationDelivery.outbox_id,
                NotificationOutbox.user_id == user_id,
            ),
        )
        .outerjoin(
            NotificationChannel,
            and_(
                NotificationChannel.id == NotificationDelivery.channel_id,
                NotificationChannel.user_id == user_id,
            ),
        )
        .where(NotificationDelivery.user_id == user_id)
        .order_by(timeline.desc().nullslast(), NotificationDelivery.id.desc())
    )


def _apply_delivery_filters(
    query,
    event_type: str | None,
    channel_id: str | None,
    status_filter: str | None,
):
    if event_type:
        if event_type not in EVENT_TYPES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported event type")
        query = query.where(NotificationOutbox.event_type == event_type)
    if channel_id:
        query = query.where(NotificationDelivery.channel_id == channel_id)
    normalized = (status_filter or "").strip().lower()
    if not normalized:
        return query
    if normalized in {"success", "succeeded", "delivered"}:
        return query.where(NotificationDelivery.status.in_(SUCCESS_DELIVERY_STATUSES))
    if normalized in {"failed", "failure"}:
        return query.where(
            NotificationDelivery.status == "failed",
            or_(NotificationOutbox.id.is_(None), NotificationOutbox.status != "retry"),
        )
    if normalized in {"waiting", "pending", "retry", "retrying"}:
        return query.where(
            or_(
                NotificationDelivery.status.in_({"pending", "retry", "retrying"}),
                and_(
                    NotificationDelivery.status == "failed",
                    NotificationOutbox.status == "retry",
                ),
            )
        )
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported delivery status")


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC)


def _safe_payload_value(key: object, value: object) -> object:
    if _is_sensitive_key(key):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_payload_value(item_key, item) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_payload_value(key, item) for item in value[:20]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:240] + "…" if len(text) > 240 else text


def _safe_payload(payload: dict | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    return {str(key): _safe_payload_value(key, value) for key, value in payload.items()}


def _payload_account(payload: dict[str, object]) -> tuple[str | None, str | None]:
    name = payload.get("account") or payload.get("account_name")
    uid = payload.get("account_uid") or payload.get("bili_uid")
    return (
        str(name)[:128] if name is not None else None,
        str(uid)[:64] if uid is not None else None,
    )


def _event_summary(event: NotificationOutbox | None, payload: dict[str, object]) -> str:
    if event is None:
        return "通知事件已清理"
    if event.event_type == "new_charge":
        supporter = str(payload.get("supporter") or "未知用户")
        amount = str(payload.get("amount") or "0")
        return f"{supporter} · {amount} B币"
    details = [
        f"{key}: {value}"
        for key, value in payload.items()
        if key not in {"message"} and value not in (None, "")
    ]
    return " · ".join(details[:4]) or event_label(event.event_type)


def delivery_display_status(
    delivery: NotificationDelivery | None, event: NotificationOutbox | None
) -> str:
    raw_status = (delivery.status if delivery else "") or ""
    raw_status = raw_status.strip().lower()
    if raw_status in SUCCESS_DELIVERY_STATUSES:
        return "succeeded"
    if event is not None and event.status == "merged":
        return "merged"
    if raw_status in RETRYING_STATUSES or (
        raw_status in {"pending", "failed"} and event is not None and event.status == "retry"
    ):
        return "retrying"
    if raw_status == "failed":
        return "failed"
    if raw_status == "pending":
        return "pending"
    return raw_status or "unknown"


def _delivery_error_summary(
    delivery: NotificationDelivery | None, display_status: str
) -> str | None:
    if delivery is None or display_status == "succeeded":
        return None
    summary = (delivery.response_summary or delivery.error_type or "未提供错误摘要").strip()
    return summary[:500]


def delivery_view(
    delivery: NotificationDelivery,
    event: NotificationOutbox | None,
    channel: NotificationChannel | None,
) -> DeliveryView:
    payload = _safe_payload(event.payload if event is not None else None)
    account_name, account_uid = _payload_account(payload)
    display_status = delivery_display_status(delivery, event)
    channel_name = channel.name.strip() if channel and channel.name.strip() else "渠道已删除"
    provider = channel.provider if channel else None
    notification_created_at = _utc_datetime(event.created_at) if event else None
    delivered_at = _utc_datetime(delivery.delivered_at)
    available_at = _utc_datetime(delivery.available_at)
    activity_at = delivered_at or notification_created_at
    raw_status = (delivery.status or "unknown").strip().lower()
    can_retry = (
        raw_status == "failed"
        and event is not None
        and event.status != "merged"
        and channel is not None
        and channel.enabled
    )
    return DeliveryView(
        id=delivery.id,
        delivery_id=delivery.id,
        notification_id=event.id if event else None,
        outbox_id=delivery.outbox_id,
        channel_id=delivery.channel_id,
        channel_name=channel_name,
        channel_provider=provider,
        channel_provider_name=provider_label(provider),
        event_type=event.event_type if event else None,
        event_label=event_label(event.event_type if event else None),
        event_summary=_event_summary(event, payload),
        account_name=account_name,
        account_uid=account_uid,
        status=delivery.status,
        display_status=display_status,
        status_label=STATUS_LABELS.get(display_status, "未知状态"),
        attempts=delivery.attempts or 0,
        max_attempts=MAX_ATTEMPTS,
        response_summary=delivery.response_summary,
        error_type=delivery.error_type,
        error_summary=_delivery_error_summary(delivery, display_status),
        notification_created_at=notification_created_at,
        activity_at=activity_at,
        available_at=available_at,
        delivered_at=delivered_at,
        can_retry=can_retry,
        merged_into_outbox_id=event.merged_into_outbox_id if event else None,
        payload_summary=payload,
    )


@router.get("/subscriptions", response_model=SubscriptionRulesView)
def list_subscriptions(user: CurrentUser, db: DbSession) -> SubscriptionRulesView:
    channels = list(
        db.scalars(
            select(NotificationChannel)
            .where(NotificationChannel.user_id == user.id)
            .order_by(NotificationChannel.created_at)
        )
    )
    subscriptions = list(
        db.scalars(
            select(NotificationSubscription)
            .join(
                NotificationChannel,
                NotificationChannel.id == NotificationSubscription.channel_id,
            )
            .where(
                NotificationSubscription.user_id == user.id,
                NotificationChannel.user_id == user.id,
            )
        )
    )
    channel_ids_by_event: dict[str, list[str]] = {event_type: [] for event_type in EVENT_TYPE_ORDER}
    for subscription in subscriptions:
        if subscription.enabled and subscription.event_type in channel_ids_by_event:
            channel_ids_by_event[subscription.event_type].append(subscription.channel_id)
    for channel_ids in channel_ids_by_event.values():
        channel_ids.sort()
    return SubscriptionRulesView(
        events=[
            EventTypeView(
                type=event_type,
                label=EVENT_LABELS.get(event_type, event_type),
                description=EVENT_DESCRIPTIONS.get(event_type, "通知事件"),
            )
            for event_type in EVENT_TYPE_ORDER
            if event_type in EVENT_TYPES
        ],
        channels=[
            SubscriptionChannelView(
                id=channel.id,
                name=channel.name,
                provider=channel.provider,
                provider_name=provider_label(channel.provider),
                enabled=channel.enabled,
            )
            for channel in channels
        ],
        rules=[
            SubscriptionRuleView(
                event_type=event_type, channel_ids=channel_ids_by_event[event_type]
            )
            for event_type in EVENT_TYPE_ORDER
            if event_type in EVENT_TYPES
        ],
    )


@router.put("/subscriptions", response_model=SubscriptionRulesView)
def update_subscriptions(
    payload: SubscriptionRulesInput, user: CurrentUser, db: DbSession
) -> SubscriptionRulesView:
    desired: dict[str, set[str]] = {}
    try:
        for rule in payload.rules:
            if rule.event_type not in EVENT_TYPES:
                raise ValueError("unsupported event type")
            if rule.event_type in desired:
                raise ValueError("duplicate event type")
            desired[rule.event_type] = set(rule.channel_ids)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    channels = list(
        db.scalars(select(NotificationChannel).where(NotificationChannel.user_id == user.id))
    )
    channel_ids = {channel.id for channel in channels}
    selected_ids = set().union(*desired.values()) if desired else set()
    if not selected_ids <= channel_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "channel does not belong to user")
    subscriptions = list(
        db.scalars(
            select(NotificationSubscription).where(
                NotificationSubscription.user_id == user.id,
                NotificationSubscription.channel_id.in_(channel_ids) if channel_ids else False,
            )
        )
    )
    existing = {
        (subscription.event_type, subscription.channel_id): subscription
        for subscription in subscriptions
    }
    for subscription in subscriptions:
        selected_channels = desired.get(subscription.event_type, set())
        subscription.enabled = (
            subscription.event_type in desired and subscription.channel_id in selected_channels
        )
    for event_type, selected_channels in desired.items():
        for channel_id in selected_channels:
            if (event_type, channel_id) not in existing:
                db.add(
                    NotificationSubscription(
                        user_id=user.id,
                        channel_id=channel_id,
                        event_type=event_type,
                        enabled=True,
                    )
                )
    db.commit()
    return list_subscriptions(user, db)


@router.get("/deliveries", response_model=list[DeliveryView])
def list_deliveries(
    user: CurrentUser,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    event_type: str | None = None,
    channel_id: str | None = None,
) -> list[DeliveryView]:
    query = _apply_delivery_filters(_delivery_query(user.id), event_type, channel_id, status_filter)
    rows = db.execute(query.limit(100)).all()
    return [delivery_view(*row) for row in rows]


@router.get("/deliveries/{delivery_id}", response_model=DeliveryView)
def get_delivery(delivery_id: str, user: CurrentUser, db: DbSession) -> DeliveryView:
    row = db.execute(_delivery_query(user.id).where(NotificationDelivery.id == delivery_id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification delivery not found")
    return delivery_view(*row)


@router.post("/deliveries/{delivery_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_delivery(delivery_id: str, user: CurrentUser, db: DbSession) -> dict[str, str]:
    delivery = db.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.user_id == user.id,
        )
    )
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification delivery not found")
    event = db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.id == delivery.outbox_id,
            NotificationOutbox.user_id == user.id,
        )
    )
    if event is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "notification event no longer exists")
    if event.status == "merged":
        raise HTTPException(status.HTTP_409_CONFLICT, "notification event was merged")
    channel = None
    if delivery.channel_id:
        channel = db.scalar(
            select(NotificationChannel).where(
                NotificationChannel.id == delivery.channel_id,
                NotificationChannel.user_id == user.id,
            )
        )
    if channel is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "notification channel no longer exists")
    raw_status = (delivery.status or "").strip().lower()
    if raw_status in SUCCESS_DELIVERY_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "notification delivery already succeeded")
    if raw_status != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, "notification delivery is not retryable")
    if not channel.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "notification channel is disabled")
    reset_delivery_for_retry(delivery, event, datetime.now(UTC))
    db.commit()
    return {"status": "queued"}
