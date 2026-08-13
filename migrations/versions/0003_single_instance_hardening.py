"""Harden sessions, collection state, jobs, and notification retries."""

import sqlalchemy as sa
from alembic import op

revision = "0003_single_instance_hardening"
down_revision = "0002_dashboard_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLAlchemy Enum persists member names by default. Accept the lowercase form too because
    # early hand-written databases and fixtures used the public enum value instead.
    op.execute("DELETE FROM schedule_jobs WHERE kind IN ('COOKIE_CHECK', 'cookie_check')")
    if op.get_bind().dialect.name == "postgresql":
        with op.batch_alter_table("users") as batch:
            batch.drop_constraint("users_username_key", type_="unique")
        with op.batch_alter_table("user_sessions") as batch:
            batch.drop_constraint("user_sessions_token_hash_key", type_="unique")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_username")
        batch.create_index("ix_users_username", ["username"], unique=True)
    with op.batch_alter_table("user_sessions") as batch:
        batch.drop_index("ix_user_sessions_token_hash")
        batch.create_index("ix_user_sessions_token_hash", ["token_hash"], unique=True)
    with op.batch_alter_table("schedule_jobs") as batch:
        batch.alter_column(
            "kind",
            existing_type=sa.String(20),
            type_=sa.Enum(
                "CHARGE_COLLECTION",
                "COUPON_CLAIM",
                "NOTIFICATION_RETRY",
                name="jobkind",
                native_enum=False,
            ),
            existing_nullable=False,
        )
    with op.batch_alter_table("bili_accounts") as batch:
        batch.add_column(sa.Column("collection_watermark_at", sa.DateTime(timezone=True)))
        batch.drop_column("encrypted_refresh_token")
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.add_column(sa.Column("available_at", sa.DateTime(timezone=True), nullable=False,
                                   server_default=sa.func.now()))
        batch.add_column(sa.Column("error_type", sa.String(64)))
        batch.create_index("ix_notification_deliveries_available_at", ["available_at"])


def downgrade() -> None:
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_index("ix_notification_deliveries_available_at")
        batch.drop_column("error_type")
        batch.drop_column("available_at")
    with op.batch_alter_table("bili_accounts") as batch:
        batch.add_column(sa.Column("encrypted_refresh_token", sa.Text()))
        batch.drop_column("collection_watermark_at")
    with op.batch_alter_table("schedule_jobs") as batch:
        batch.alter_column(
            "kind",
            existing_type=sa.Enum(
                "CHARGE_COLLECTION",
                "COUPON_CLAIM",
                "NOTIFICATION_RETRY",
                name="jobkind",
                native_enum=False,
            ),
            type_=sa.String(20),
            existing_nullable=False,
        )
    with op.batch_alter_table("user_sessions") as batch:
        batch.drop_index("ix_user_sessions_token_hash")
        batch.create_index("ix_user_sessions_token_hash", ["token_hash"], unique=False)
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_username")
        batch.create_index("ix_users_username", ["username"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        with op.batch_alter_table("user_sessions") as batch:
            batch.create_unique_constraint(
                "user_sessions_token_hash_key", ["token_hash"]
            )
        with op.batch_alter_table("users") as batch:
            batch.create_unique_constraint("users_username_key", ["username"])
