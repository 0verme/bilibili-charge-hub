import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


class JobKind(StrEnum):
    CHARGE_COLLECTION = "charge_collection"
    COUPON_CLAIM = "coupon_claim"
    NOTIFICATION_RETRY = "notification_retry"
    DAILY_TASK = "daily_task"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL_SUCCESS = "partial_success"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        SqlEnum(UserRole, native_enum=False), default=UserRole.USER
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BiliAccount(Base, TimestampMixin):
    __tablename__ = "bili_accounts"
    __table_args__ = (UniqueConstraint("user_id", "bili_uid", name="uq_bili_account_tenant_uid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bili_uid: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[AccountStatus] = mapped_column(
        SqlEnum(AccountStatus, native_enum=False), default=AccountStatus.ACTIVE
    )
    encrypted_cookie: Mapped[str] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collection_watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QrLoginSession(Base):
    __tablename__ = "qr_login_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    qrcode_key: Mapped[str] = mapped_column(String(255), unique=True)
    qr_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChargeRecord(Base):
    __tablename__ = "charge_records"
    __table_args__ = (
        UniqueConstraint("bili_account_id", "event_id", name="uq_charge_account_event"),
        UniqueConstraint("bili_account_id", "record_key", name="uq_charge_account_record_key"),
        Index("ix_charge_tenant_time", "user_id", "charged_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bili_account_id: Mapped[str] = mapped_column(
        ForeignKey("bili_accounts.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(64))
    record_key: Mapped[str] = mapped_column(String(64), index=True)
    notification_eligible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    supporter_uid: Mapped[str] = mapped_column(String(32), index=True)
    supporter_name: Mapped[str] = mapped_column(String(128), index=True)
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    brokerage: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    remark: Mapped[str] = mapped_column(Text, default="")
    charged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduleJob(Base, TimestampMixin):
    __tablename__ = "schedule_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bili_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("bili_accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[JobKind] = mapped_column(SqlEnum(JobKind, native_enum=False), index=True)
    trigger_type: Mapped[str] = mapped_column(String(16))
    trigger_config: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_run_tenant_started", "user_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    schedule_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("schedule_jobs.id", ondelete="SET NULL"), index=True
    )
    # Snapshot execution ownership and cause so history remains auditable after a job changes.
    bili_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("bili_accounts.id", ondelete="SET NULL"), index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(24), default="scheduled")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RunStatus] = mapped_column(SqlEnum(RunStatus, native_enum=False), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)


class NotificationChannel(Base, TimestampMixin):
    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32), index=True)
    encrypted_config: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"
    __table_args__ = (
        UniqueConstraint("channel_id", "event_type", name="uq_subscription_channel_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    merged_into_outbox_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("outbox_id", "channel_id", name="uq_delivery_outbox_channel"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    outbox_id: Mapped[str] = mapped_column(
        ForeignKey("notification_outbox.id", ondelete="CASCADE"), index=True
    )
    # A deleted channel must not erase the delivery audit trail. The API nulls this
    # reference before deletion and PostgreSQL migrations use SET NULL as a guardrail.
    channel_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    error_type: Mapped[str | None] = mapped_column(String(64))
    response_summary: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CouponClaim(Base):
    __tablename__ = "coupon_claims"
    __table_args__ = (
        UniqueConstraint("bili_account_id", "claim_month", name="uq_coupon_account_month"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bili_account_id: Mapped[str] = mapped_column(
        ForeignKey("bili_accounts.id", ondelete="CASCADE"), index=True
    )
    claim_month: Mapped[str] = mapped_column(String(7), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    result_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DashboardShare(Base):
    __tablename__ = "dashboard_shares"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_scheme: Mapped[str | None] = mapped_column(String(16))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    mask_names: Mapped[bool] = mapped_column(Boolean, default=True)
    mask_uids: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyTaskProfile(Base):
    """Per-account daily-task configuration.

    Donating coins is a consumptive action, so the task is disabled by default and
    requires an explicit opt-in per account.
    """

    __tablename__ = "daily_task_profiles"
    __table_args__ = (UniqueConstraint("bili_account_id", name="uq_daily_profile_account"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bili_account_id: Mapped[str] = mapped_column(
        ForeignKey("bili_accounts.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    target_coins: Mapped[int] = mapped_column(Integer, default=2)
    protected_coins: Mapped[int] = mapped_column(Integer, default=50)
    select_like: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_when_lv6: Mapped[bool] = mapped_column(Boolean, default=True)
    share_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    watch_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    support_up_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailyTaskRecord(Base):
    """Daily-task execution result, one row per account per local day."""

    __tablename__ = "daily_task_records"
    __table_args__ = (
        UniqueConstraint("bili_account_id", "task_date", name="uq_daily_record_account_date"),
        Index("ix_daily_record_tenant_date", "user_id", "task_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bili_account_id: Mapped[str] = mapped_column(
        ForeignKey("bili_accounts.id", ondelete="CASCADE"), index=True
    )
    task_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    login_done: Mapped[bool] = mapped_column(Boolean, default=False)
    watch_done: Mapped[bool] = mapped_column(Boolean, default=False)
    share_done: Mapped[bool] = mapped_column(Boolean, default=False)
    coins_donated: Mapped[int] = mapped_column(Integer, default=0)
    target_coins: Mapped[int] = mapped_column(Integer, default=0)
    balance_before: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    share_video: Mapped[str] = mapped_column(Text, default="")
    donated_videos: Mapped[list] = mapped_column(JSON, default=list)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
