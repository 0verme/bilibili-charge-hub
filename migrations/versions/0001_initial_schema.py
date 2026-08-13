"""Create the initial multi-tenant schema with auditable DDL."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(5), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_table("user_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name, cols in [("ix_user_sessions_user_id", ["user_id"]), ("ix_user_sessions_token_hash", ["token_hash"]), ("ix_user_sessions_expires_at", ["expires_at"])]:
        op.create_index(name, "user_sessions", cols)
    op.create_table("bili_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bili_uid", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("encrypted_cookie", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "bili_uid", name="uq_bili_account_tenant_uid"),
    )
    op.create_index("ix_bili_accounts_user_id", "bili_accounts", ["user_id"])
    op.create_index("ix_bili_accounts_bili_uid", "bili_accounts", ["bili_uid"])
    op.create_table("qr_login_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("qrcode_key", sa.String(255), nullable=False, unique=True),
        sa.Column("qr_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name, cols in [("ix_qr_login_sessions_user_id", ["user_id"]), ("ix_qr_login_sessions_status", ["status"]), ("ix_qr_login_sessions_expires_at", ["expires_at"])]:
        op.create_index(name, "qr_login_sessions", cols)
    op.create_table("charge_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bili_account_id", sa.String(36), sa.ForeignKey("bili_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("supporter_uid", sa.String(32), nullable=False),
        sa.Column("supporter_name", sa.String(128), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(14,2), nullable=False),
        sa.Column("brokerage", sa.Numeric(14,2), nullable=False),
        sa.Column("remark", sa.Text(), nullable=False),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bili_account_id", "event_id", name="uq_charge_account_event"),
    )
    for name, cols in [("ix_charge_records_user_id", ["user_id"]), ("ix_charge_records_bili_account_id", ["bili_account_id"]), ("ix_charge_records_supporter_uid", ["supporter_uid"]), ("ix_charge_records_supporter_name", ["supporter_name"]), ("ix_charge_records_charged_at", ["charged_at"]), ("ix_charge_tenant_time", ["user_id", "charged_at"])]:
        op.create_index(name, "charge_records", cols)
    op.create_table("schedule_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bili_account_id", sa.String(36), sa.ForeignKey("bili_accounts.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("trigger_config", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name, cols in [("ix_schedule_jobs_user_id", ["user_id"]), ("ix_schedule_jobs_bili_account_id", ["bili_account_id"]), ("ix_schedule_jobs_kind", ["kind"]), ("ix_schedule_jobs_next_run_at", ["next_run_at"])]:
        op.create_index(name, "schedule_jobs", cols)
    op.create_table("job_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schedule_job_id", sa.String(36), sa.ForeignKey("schedule_jobs.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(9), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
    )
    for name, cols in [("ix_job_runs_user_id", ["user_id"]), ("ix_job_runs_schedule_job_id", ["schedule_job_id"]), ("ix_job_runs_status", ["status"]), ("ix_job_run_tenant_started", ["user_id", "started_at"])]:
        op.create_index(name, "job_runs", cols)
    op.create_table("notification_channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False), sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("encrypted_config", sa.Text(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notification_channels_user_id", "notification_channels", ["user_id"])
    op.create_index("ix_notification_channels_provider", "notification_channels", ["provider"])
    op.create_table("notification_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("channel_id", "event_type", name="uq_subscription_channel_event"),
    )
    for name, cols in [("ix_notification_subscriptions_user_id", ["user_id"]), ("ix_notification_subscriptions_channel_id", ["channel_id"]), ("ix_notification_subscriptions_event_type", ["event_type"])]:
        op.create_index(name, "notification_subscriptions", cols)
    op.create_table("notification_outbox",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False), sa.Column("dedupe_key", sa.String(128), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
    )
    for name, cols in [("ix_notification_outbox_user_id", ["user_id"]), ("ix_notification_outbox_event_type", ["event_type"]), ("ix_notification_outbox_status", ["status"]), ("ix_notification_outbox_available_at", ["available_at"])]:
        op.create_index(name, "notification_outbox", cols)
    op.create_table("notification_deliveries",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outbox_id", sa.String(36), sa.ForeignKey("notification_outbox.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(36), sa.ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("response_summary", sa.Text()), sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("outbox_id", "channel_id", name="uq_delivery_outbox_channel"),
    )
    for name, cols in [("ix_notification_deliveries_user_id", ["user_id"]), ("ix_notification_deliveries_outbox_id", ["outbox_id"]), ("ix_notification_deliveries_channel_id", ["channel_id"]), ("ix_notification_deliveries_status", ["status"])]:
        op.create_index(name, "notification_deliveries", cols)
    op.create_table("coupon_claims",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bili_account_id", sa.String(36), sa.ForeignKey("bili_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("claim_month", sa.String(7), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("result_code", sa.String(64)),
        sa.Column("message", sa.Text(), nullable=False), sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bili_account_id", "claim_month", name="uq_coupon_account_month"),
    )
    for name, cols in [("ix_coupon_claims_user_id", ["user_id"]), ("ix_coupon_claims_bili_account_id", ["bili_account_id"]), ("ix_coupon_claims_claim_month", ["claim_month"]), ("ix_coupon_claims_status", ["status"])]:
        op.create_index(name, "coupon_claims", cols)


def downgrade() -> None:
    for table in ["coupon_claims", "notification_deliveries", "notification_outbox", "notification_subscriptions", "notification_channels", "job_runs", "schedule_jobs", "charge_records", "qr_login_sessions", "bili_accounts", "user_sessions", "users"]:
        op.drop_table(table)
