"""Add daily-task profiles and per-day records for opt-in coin/share automation."""

import sqlalchemy as sa
from alembic import op

revision = "0005_daily_task"
down_revision = "0004_fix_naive_charge_times"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_task_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bili_account_id",
            sa.String(36),
            sa.ForeignKey("bili_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("target_coins", sa.Integer(), nullable=False),
        sa.Column("protected_coins", sa.Integer(), nullable=False),
        sa.Column("select_like", sa.Boolean(), nullable=False),
        sa.Column("skip_when_lv6", sa.Boolean(), nullable=False),
        sa.Column("share_enabled", sa.Boolean(), nullable=False),
        sa.Column("watch_enabled", sa.Boolean(), nullable=False),
        sa.Column("support_up_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bili_account_id", name="uq_daily_profile_account"),
    )
    op.create_index("ix_daily_task_profiles_user_id", "daily_task_profiles", ["user_id"])
    op.create_index(
        "ix_daily_task_profiles_bili_account_id", "daily_task_profiles", ["bili_account_id"]
    )
    op.create_table(
        "daily_task_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bili_account_id",
            sa.String(36),
            sa.ForeignKey("bili_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_date", sa.String(10), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("login_done", sa.Boolean(), nullable=False),
        sa.Column("watch_done", sa.Boolean(), nullable=False),
        sa.Column("share_done", sa.Boolean(), nullable=False),
        sa.Column("coins_donated", sa.Integer(), nullable=False),
        sa.Column("target_coins", sa.Integer(), nullable=False),
        sa.Column("balance_before", sa.Numeric(14, 2)),
        sa.Column("balance_after", sa.Numeric(14, 2)),
        sa.Column("share_video", sa.Text(), nullable=False),
        sa.Column("donated_videos", sa.JSON(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bili_account_id", "task_date", name="uq_daily_record_account_date"),
    )
    op.create_index("ix_daily_task_records_user_id", "daily_task_records", ["user_id"])
    op.create_index(
        "ix_daily_task_records_bili_account_id", "daily_task_records", ["bili_account_id"]
    )
    op.create_index("ix_daily_task_records_task_date", "daily_task_records", ["task_date"])
    op.create_index(
        "ix_daily_task_records_status", "daily_task_records", ["status"]
    )
    op.create_index(
        "ix_daily_record_tenant_date", "daily_task_records", ["user_id", "task_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_daily_record_tenant_date", table_name="daily_task_records")
    op.drop_index("ix_daily_task_records_status", table_name="daily_task_records")
    op.drop_index("ix_daily_task_records_task_date", table_name="daily_task_records")
    op.drop_index("ix_daily_task_records_bili_account_id", table_name="daily_task_records")
    op.drop_index("ix_daily_task_records_user_id", table_name="daily_task_records")
    op.drop_table("daily_task_records")
    op.drop_index("ix_daily_task_profiles_bili_account_id", table_name="daily_task_profiles")
    op.drop_index("ix_daily_task_profiles_user_id", table_name="daily_task_profiles")
    op.drop_table("daily_task_profiles")
