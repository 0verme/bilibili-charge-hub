"""Interpret legacy naive Bilibili charge times as Asia/Shanghai."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "0004_fix_naive_charge_times"
down_revision = "0003_single_instance_hardening"
branch_labels = None
depends_on = None

BILIBILI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _corrected_charge_time(raw_data: object) -> datetime | None:
    if not isinstance(raw_data, dict):
        return None
    value = raw_data.get("ctime", raw_data.get("charge_time"))
    text = str(value or "").strip()
    if not text or text.isdigit():
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed.replace(tzinfo=BILIBILI_TIMEZONE).astimezone(UTC)


def upgrade() -> None:
    connection = op.get_bind()
    charge_records = sa.table(
        "charge_records",
        sa.column("id", sa.String()),
        sa.column("bili_account_id", sa.String()),
        sa.column("charged_at", sa.DateTime(timezone=True)),
        sa.column("raw_data", sa.JSON()),
    )
    bili_accounts = sa.table(
        "bili_accounts",
        sa.column("id", sa.String()),
        sa.column("collection_watermark_at", sa.DateTime(timezone=True)),
    )

    affected_accounts: set[str] = set()
    for row in connection.execute(
        sa.select(charge_records.c.id, charge_records.c.bili_account_id, charge_records.c.raw_data)
    ).mappings():
        corrected = _corrected_charge_time(row["raw_data"])
        if corrected is None:
            continue
        connection.execute(
            charge_records.update()
            .where(charge_records.c.id == row["id"])
            .values(charged_at=corrected)
        )
        affected_accounts.add(row["bili_account_id"])

    for account_id in affected_accounts:
        watermark = connection.execute(
            sa.select(sa.func.max(charge_records.c.charged_at)).where(
                charge_records.c.bili_account_id == account_id
            )
        ).scalar_one()
        connection.execute(
            bili_accounts.update()
            .where(bili_accounts.c.id == account_id)
            .values(collection_watermark_at=watermark)
        )


def downgrade() -> None:
    # The original UTC assumption cannot be restored without reintroducing incorrect data.
    pass
