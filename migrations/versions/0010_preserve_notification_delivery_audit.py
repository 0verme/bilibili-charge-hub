"""Keep notification delivery audit rows when a channel is deleted."""

import sqlalchemy as sa
from alembic import op

revision = "0010_preserve_delivery_audit"
down_revision = "0009_align_job_run_schema"
branch_labels = None
depends_on = None


def _channel_foreign_key_name() -> str | None:
    inspector = sa.inspect(op.get_bind())
    for foreign_key in inspector.get_foreign_keys("notification_deliveries"):
        if (
            foreign_key.get("constrained_columns") == ["channel_id"]
            and foreign_key.get("referred_table") == "notification_channels"
        ):
            return foreign_key.get("name")
    return None


def _delivery_table(metadata: sa.MetaData, *, nullable_channel: bool, ondelete: str) -> sa.Table:
    return sa.Table(
        "notification_deliveries",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "outbox_id",
            sa.String(36),
            sa.ForeignKey("notification_outbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel_id",
            sa.String(36),
            sa.ForeignKey("notification_channels.id", ondelete=ondelete),
            nullable=nullable_channel,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_type", sa.String(64)),
        sa.Column("response_summary", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("outbox_id", "channel_id", name="uq_delivery_outbox_channel"),
        sa.Index("ix_notification_deliveries_user_id", "user_id"),
        sa.Index("ix_notification_deliveries_outbox_id", "outbox_id"),
        sa.Index("ix_notification_deliveries_channel_id", "channel_id"),
        sa.Index("ix_notification_deliveries_status", "status"),
        sa.Index("ix_notification_deliveries_available_at", "available_at"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        metadata = sa.MetaData()
        with op.batch_alter_table(
            "notification_deliveries",
            copy_from=_delivery_table(metadata, nullable_channel=True, ondelete="SET NULL"),
            recreate="always",
        ):
            pass
        return

    foreign_key_name = _channel_foreign_key_name()
    if foreign_key_name:
        op.drop_constraint(foreign_key_name, "notification_deliveries", type_="foreignkey")
    op.alter_column(
        "notification_deliveries",
        "channel_id",
        existing_type=sa.String(36),
        existing_nullable=False,
        nullable=True,
    )
    op.create_foreign_key(
        "fk_notification_deliveries_channel_id",
        "notification_deliveries",
        "notification_channels",
        ["channel_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT COUNT(*) FROM notification_deliveries WHERE channel_id IS NULL")
    ).scalar_one():
        raise RuntimeError(
            "cannot downgrade notification audit migration while deleted-channel deliveries exist"
        )
    if bind.dialect.name == "sqlite":
        metadata = sa.MetaData()
        with op.batch_alter_table(
            "notification_deliveries",
            copy_from=_delivery_table(metadata, nullable_channel=False, ondelete="CASCADE"),
            recreate="always",
        ):
            pass
        return

    op.drop_constraint(
        "fk_notification_deliveries_channel_id",
        "notification_deliveries",
        type_="foreignkey",
    )
    op.alter_column(
        "notification_deliveries",
        "channel_id",
        existing_type=sa.String(36),
        existing_nullable=True,
        nullable=False,
    )
    op.create_foreign_key(
        "fk_notification_deliveries_channel_id",
        "notification_deliveries",
        "notification_channels",
        ["channel_id"],
        ["id"],
        ondelete="CASCADE",
    )
