"""Add canonical charge keys and merge legacy duplicate charge records."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "0006_canonical_charge_keys"
down_revision = "0005_daily_task"
branch_labels = None
depends_on = None

BILIBILI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _source_id(raw_data: object) -> str | None:
    if not isinstance(raw_data, dict):
        return None
    for field_name in ("id", "orderNo", "tradeNo"):
        value = raw_data.get(field_name)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _record_key(row: dict[str, Any]) -> str:
    source_id = _source_id(row.get("raw_data"))
    if source_id:
        raw = f"{row['bili_account_id']}|source|{source_id}"
    else:
        raw_data = row.get("raw_data")
        supporter_uid = row["supporter_uid"]
        if isinstance(raw_data, dict):
            supporter_uid = raw_data.get("mid") or raw_data.get("uid") or supporter_uid
        raw = "|".join(
            str(value)
            for value in (
                row["bili_account_id"],
                supporter_uid,
                Decimal(str(row["amount"])).quantize(Decimal("0.01")),
                Decimal(str(row["brokerage"])).quantize(Decimal("0.01")),
                _as_utc(row["charged_at"]).isoformat(),
            )
        )
    return hashlib.sha256(raw.encode()).hexdigest()


def _tenant_dedupe_key(user_id: str, account_id: str, event_id: str) -> str:
    raw = f"{user_id}|charge:{account_id}:{event_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _uid_only_name(name: str, uid: str) -> bool:
    return not name.strip() or name.strip() == uid


def _merged_raw_data(canonical: dict[str, Any], duplicate: dict[str, Any]) -> dict[str, Any]:
    canonical_raw_data = canonical.get("raw_data")
    merged = dict(canonical_raw_data) if isinstance(canonical_raw_data, dict) else {}
    duplicate_raw_data = duplicate.get("raw_data")
    if not isinstance(duplicate_raw_data, dict):
        return merged
    for key, value in duplicate_raw_data.items():
        if key not in merged or not merged[key]:
            merged[key] = value
    return merged


def upgrade() -> None:
    with op.batch_alter_table("charge_records") as batch:
        batch.add_column(sa.Column("record_key", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "notification_eligible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.add_column(sa.Column("merged_into_outbox_id", sa.String(36), nullable=True))
        batch.create_index(
            "ix_notification_outbox_merged_into_outbox_id", ["merged_into_outbox_id"]
        )

    bind = op.get_bind()
    charge_records = sa.table(
        "charge_records",
        sa.column("id", sa.String(36)),
        sa.column("user_id", sa.String(36)),
        sa.column("bili_account_id", sa.String(36)),
        sa.column("event_id", sa.String(64)),
        sa.column("record_key", sa.String(64)),
        sa.column("notification_eligible", sa.Boolean()),
        sa.column("supporter_uid", sa.String(32)),
        sa.column("supporter_name", sa.String(128)),
        sa.column("avatar_url", sa.Text()),
        sa.column("remark", sa.Text()),
        sa.column("charged_at", sa.DateTime(timezone=True)),
        sa.column("raw_data", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("amount", sa.Numeric(14, 2)),
        sa.column("brokerage", sa.Numeric(14, 2)),
    )
    notification_outbox = sa.table(
        "notification_outbox",
        sa.column("id", sa.String(36)),
        sa.column("user_id", sa.String(36)),
        sa.column("dedupe_key", sa.String(128)),
        sa.column("status", sa.String(32)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("merged_into_outbox_id", sa.String(36)),
    )

    rows = [
        dict(row)
        for row in bind.execute(
            sa.select(charge_records).order_by(charge_records.c.created_at, charge_records.c.id)
        ).mappings()
    ]
    outboxes = [dict(row) for row in bind.execute(sa.select(notification_outbox)).mappings()]
    outboxes_by_dedupe = {row["dedupe_key"]: row for row in outboxes}

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = _record_key(row)
        row["record_key"] = key
        bind.execute(
            charge_records.update()
            .where(charge_records.c.id == row["id"])
            .values(record_key=key, notification_eligible=False)
        )
        groups.setdefault((row["bili_account_id"], key), []).append(row)

    for records in groups.values():
        records.sort(key=lambda row: (row["created_at"], row["id"]))
        canonical = records[0]
        for duplicate in records[1:]:
            if _uid_only_name(canonical["supporter_name"], canonical["supporter_uid"]):
                if not _uid_only_name(duplicate["supporter_name"], duplicate["supporter_uid"]):
                    canonical["supporter_name"] = duplicate["supporter_name"]
            if not canonical["avatar_url"] and duplicate["avatar_url"]:
                canonical["avatar_url"] = duplicate["avatar_url"]
            if not canonical["remark"] and duplicate["remark"]:
                canonical["remark"] = duplicate["remark"]
            canonical["raw_data"] = _merged_raw_data(canonical, duplicate)

        bind.execute(
            charge_records.update()
            .where(charge_records.c.id == canonical["id"])
            .values(
                supporter_name=canonical["supporter_name"],
                avatar_url=canonical["avatar_url"],
                remark=canonical["remark"],
                raw_data=canonical["raw_data"],
            )
        )

        canonical_dedupe = _tenant_dedupe_key(
            canonical["user_id"], canonical["bili_account_id"], canonical["event_id"]
        )
        candidate_outboxes = []
        for record in records:
            dedupe = _tenant_dedupe_key(
                record["user_id"], record["bili_account_id"], record["event_id"]
            )
            outbox = outboxes_by_dedupe.get(dedupe)
            if outbox is not None:
                candidate_outboxes.append(outbox)

        canonical_outbox = outboxes_by_dedupe.get(canonical_dedupe)
        if canonical_outbox is None and candidate_outboxes:
            candidate_outboxes.sort(key=lambda row: (row["created_at"], row["id"]))
            canonical_outbox = candidate_outboxes[0]
            bind.execute(
                notification_outbox.update()
                .where(notification_outbox.c.id == canonical_outbox["id"])
                .values(dedupe_key=canonical_dedupe, merged_into_outbox_id=None)
            )
            outboxes_by_dedupe[canonical_dedupe] = canonical_outbox

        if canonical_outbox is not None:
            for outbox in candidate_outboxes:
                if outbox["id"] == canonical_outbox["id"]:
                    continue
                bind.execute(
                    notification_outbox.update()
                    .where(notification_outbox.c.id == outbox["id"])
                    .values(
                        status="merged",
                        merged_into_outbox_id=canonical_outbox["id"],
                    )
                )

        duplicate_ids = [record["id"] for record in records[1:]]
        if duplicate_ids:
            bind.execute(charge_records.delete().where(charge_records.c.id.in_(duplicate_ids)))

    with op.batch_alter_table("charge_records") as batch:
        batch.alter_column(
            "record_key",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_charge_account_record_key", ["bili_account_id", "record_key"]
        )
        batch.create_index("ix_charge_records_record_key", ["record_key"])
        batch.create_index("ix_charge_records_notification_eligible", ["notification_eligible"])


def downgrade() -> None:
    with op.batch_alter_table("charge_records") as batch:
        batch.drop_index("ix_charge_records_notification_eligible")
        batch.drop_index("ix_charge_records_record_key")
        batch.drop_constraint("uq_charge_account_record_key", type_="unique")
        batch.drop_column("notification_eligible")
        batch.drop_column("record_key")
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_index("ix_notification_outbox_merged_into_outbox_id")
        batch.drop_column("merged_into_outbox_id")
