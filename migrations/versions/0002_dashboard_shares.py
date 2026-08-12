"""Add dashboard share links."""

from alembic import op

from app.models import DashboardShare

revision = "0002_dashboard_shares"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    DashboardShare.__table__.create(bind=op.get_bind())


def downgrade() -> None:
    DashboardShare.__table__.drop(bind=op.get_bind())
