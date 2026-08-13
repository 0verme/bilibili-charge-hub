"""Harden sessions, collection state, jobs, and notification retries."""

import sqlalchemy as sa
from alembic import op

revision = "0003_single_instance_hardening"
down_revision = "0002_dashboard_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM schedule_jobs WHERE kind = 'cookie_check'")
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
