"""Add dashboard share links with auditable, migration-local DDL."""

import sqlalchemy as sa
from alembic import op

revision = "0002_dashboard_shares"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mask_names", sa.Boolean(), nullable=False),
        sa.Column("mask_uids", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_dashboard_shares_token_hash", "dashboard_shares", ["token_hash"], unique=True
    )
    op.create_index("ix_dashboard_shares_user_id", "dashboard_shares", ["user_id"])
    op.create_index("ix_dashboard_shares_expires_at", "dashboard_shares", ["expires_at"])


def downgrade() -> None:
    op.drop_table("dashboard_shares")
