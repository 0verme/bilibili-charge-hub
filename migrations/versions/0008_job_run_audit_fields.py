"""Add durable execution audit context to job runs."""

import sqlalchemy as sa
from alembic import op

revision = "0008_job_run_audit_fields"
down_revision = "0007_repeatable_dashboard_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_runs", sa.Column("bili_account_id", sa.String(36), nullable=True))
    # SQLite cannot ALTER TABLE to add a foreign key; the ORM still enforces
    # ownership and production databases get the durable constraint.
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_job_runs_bili_account_id",
            "job_runs",
            "bili_accounts",
            ["bili_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_job_runs_bili_account_id", "job_runs", ["bili_account_id"])
    op.add_column(
        "job_runs",
        sa.Column("trigger_type", sa.String(24), nullable=True, server_default="scheduled"),
    )
    op.add_column("job_runs", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_runs", sa.Column("error_type", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("job_runs", "error_type")
    op.drop_column("job_runs", "scheduled_at")
    op.drop_column("job_runs", "trigger_type")
    op.drop_index("ix_job_runs_bili_account_id", table_name="job_runs")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_job_runs_bili_account_id", "job_runs", type_="foreignkey")
    op.drop_column("job_runs", "bili_account_id")
