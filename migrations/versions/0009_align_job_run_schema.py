"""Align job run storage with the execution audit model."""

import sqlalchemy as sa
from alembic import op

revision = "0009_align_job_run_schema"
down_revision = "0008_job_run_audit_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_runs") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(length=9),
            type_=sa.String(length=14),
            existing_nullable=False,
            nullable=False,
        )
        batch.alter_column(
            "trigger_type",
            existing_type=sa.String(length=24),
            existing_nullable=True,
            nullable=False,
            existing_server_default=sa.text("'scheduled'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("job_runs") as batch:
        batch.alter_column(
            "trigger_type",
            existing_type=sa.String(length=24),
            existing_nullable=False,
            nullable=True,
            existing_server_default=sa.text("'scheduled'"),
        )
        batch.alter_column(
            "status",
            existing_type=sa.String(length=14),
            type_=sa.String(length=9),
            existing_nullable=False,
            nullable=False,
        )
