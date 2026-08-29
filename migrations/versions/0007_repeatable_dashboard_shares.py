"""Allow dashboard share URLs to be rebuilt without storing bearer tokens."""

import sqlalchemy as sa
from alembic import op

revision = "0007_repeatable_dashboard_shares"
down_revision = "0006_canonical_charge_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows are legacy hash-only shares and are intentionally left
    # without a token_scheme until the owner regenerates their URL.
    op.add_column("dashboard_shares", sa.Column("token_scheme", sa.String(16), nullable=True))
    op.add_column(
        "dashboard_shares",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dashboard_shares_revoked_at", "dashboard_shares", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_dashboard_shares_revoked_at", table_name="dashboard_shares")
    op.drop_column("dashboard_shares", "revoked_at")
    op.drop_column("dashboard_shares", "token_scheme")
