from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.crypto import get_credential_cipher
from app.database import get_engine, get_session_factory
from app.main import create_app
from app.models import BiliAccount, ChargeRecord, JobKind, ScheduleJob
from app.readiness import check_migration_readiness, get_code_heads
from app.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def upgrade_database(monkeypatch: pytest.MonkeyPatch, path: Path, revision: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url(path))
    get_settings.cache_clear()
    command.upgrade(alembic_config(), revision)
    get_settings.cache_clear()


def clear_runtime_caches() -> None:
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    get_credential_cipher.cache_clear()
    get_settings.cache_clear()


def test_migration_readiness_requires_exactly_one_matching_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "readiness.sqlite3"
    engine = create_engine(database_url(path))
    try:
        missing = check_migration_readiness(engine)
        assert not missing.ready
        assert missing.reason == "missing_version_table"

        upgrade_database(monkeypatch, path, "0002_dashboard_shares")
        behind = check_migration_readiness(engine)
        assert not behind.ready
        assert behind.current_heads == ("0002_dashboard_shares",)
        assert behind.expected_heads == get_code_heads()
        assert behind.reason == "revision_mismatch"

        upgrade_database(monkeypatch, path, "head")
        current = check_migration_readiness(engine)
        assert current.ready
        assert current.current_heads == current.expected_heads == get_code_heads()

        code_multiple = check_migration_readiness(engine, expected_heads=("code-a", "code-b"))
        assert not code_multiple.ready
        assert code_multiple.reason == "code_has_multiple_heads"

        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('unexpected_second_head')")
            )
        multiple = check_migration_readiness(engine)
        assert not multiple.ready
        assert multiple.reason == "database_has_multiple_heads"
    finally:
        engine.dispose()
        clear_runtime_caches()


def test_lagging_database_keeps_scheduler_stopped_and_readyz_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lagging.sqlite3"
    upgrade_database(monkeypatch, path, "0002_dashboard_shares")
    clear_runtime_caches()
    monkeypatch.setenv("DATABASE_URL", database_url(path))
    get_settings.cache_clear()

    try:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/readyz")

            assert response.status_code == 503
            assert response.json()["status"] == "not_ready"
            assert response.json()["checks"]["migration"] == {
                "status": "not_ready",
                "current_heads": ["0002_dashboard_shares"],
                "expected_heads": list(get_code_heads()),
                "reason": "revision_mismatch",
            }
            assert response.json()["checks"]["scheduler"] == "unavailable"
            assert not app.state.scheduler.scheduler.running
    finally:
        get_engine().dispose()
        clear_runtime_caches()


def test_current_database_starts_scheduler_and_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "current.sqlite3"
    upgrade_database(monkeypatch, path, "head")
    clear_runtime_caches()
    monkeypatch.setenv("DATABASE_URL", database_url(path))
    get_settings.cache_clear()

    try:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/readyz")
            assert response.status_code == 200
            assert response.json()["status"] == "ready"
            assert response.json()["checks"]["migration"]["status"] == "ok"
            assert app.state.scheduler.scheduler.running
    finally:
        get_engine().dispose()
        clear_runtime_caches()


def test_0002_to_head_upgrade_preserves_existing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    upgrade_database(monkeypatch, path, "0002_dashboard_shares")
    engine = create_engine(database_url(path))
    now = datetime(2026, 8, 13, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """INSERT INTO users
                    (id, username, password_hash, role, is_active, created_at, updated_at)
                    VALUES
                    ('user-1', 'owner', 'hash', 'admin', 1, :now, :now)"""
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    """INSERT INTO bili_accounts
                    (id, user_id, bili_uid, display_name, status, encrypted_cookie,
                     encrypted_refresh_token, created_at, updated_at)
                    VALUES
                    ('account-1', 'user-1', '123', 'UP', 'active', 'cookie-cipher',
                     'unused-refresh-cipher', :now, :now)"""
                ),
                {"now": now},
            )
            jobs = [
                {
                    "id": "job-keep",
                    "kind": "CHARGE_COLLECTION",
                    "trigger": json.dumps({"seconds": 300}),
                },
                {
                    "id": "job-remove",
                    "kind": "COOKIE_CHECK",
                    "trigger": json.dumps({"seconds": 300}),
                },
                {
                    "id": "job-remove-legacy-value",
                    "kind": "cookie_check",
                    "trigger": json.dumps({"seconds": 300}),
                },
            ]
            for job in jobs:
                connection.execute(
                    text(
                        """INSERT INTO schedule_jobs
                        (id, user_id, bili_account_id, kind, trigger_type, trigger_config,
                         enabled, created_at, updated_at)
                        VALUES
                        (:id, 'user-1', 'account-1', :kind, 'interval', :trigger,
                         1, :now, :now)"""
                    ),
                    {**job, "now": now},
                )
            connection.execute(
                text(
                    """INSERT INTO notification_channels
                    (id, user_id, name, provider, encrypted_config, enabled, created_at, updated_at)
                    VALUES
                    ('channel-1', 'user-1', 'Webhook', 'webhook', 'config-cipher', 1, :now, :now)"""
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    """INSERT INTO notification_outbox
                    (id, user_id, event_type, dedupe_key, payload, status, attempts,
                     available_at, created_at)
                    VALUES
                    ('outbox-1', 'user-1', 'test', 'test:1', '{}', 'pending', 0, :now, :now)"""
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    """INSERT INTO notification_deliveries
                    (id, user_id, outbox_id, channel_id, status, attempts)
                    VALUES
                    ('delivery-1', 'user-1', 'outbox-1', 'channel-1', 'pending', 0)"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO dashboard_shares
                    (id, user_id, token_hash, expires_at, mask_names, mask_uids, created_at)
                    VALUES
                    ('share-1', 'user-1', 'token-hash', :now, 1, 1, :now)"""
                ),
                {"now": now},
            )

        upgrade_database(monkeypatch, path, "head")

        account_columns = {
            column["name"] for column in inspect(engine).get_columns("bili_accounts")
        }
        delivery_columns = {
            column["name"] for column in inspect(engine).get_columns("notification_deliveries")
        }
        assert "encrypted_refresh_token" not in account_columns
        assert "collection_watermark_at" in account_columns
        assert {"available_at", "error_type"} <= delivery_columns
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT encrypted_cookie FROM bili_accounts WHERE id = 'account-1'")
                ).scalar_one()
                == "cookie-cipher"
            )
            assert connection.execute(
                text("SELECT id FROM schedule_jobs ORDER BY id")
            ).scalars().all() == ["job-keep"]
            assert (
                connection.execute(
                    text("SELECT available_at FROM notification_deliveries WHERE id = 'delivery-1'")
                ).scalar_one()
                is not None
            )
            assert (
                connection.execute(
                    text("SELECT id FROM dashboard_shares WHERE id = 'share-1'")
                ).scalar_one()
                == "share-1"
            )
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == get_code_heads()[0]
        with Session(engine) as session:
            assert session.scalar(select(ScheduleJob).where(ScheduleJob.id == "job-keep")).kind == (
                JobKind.CHARGE_COLLECTION
            )
    finally:
        engine.dispose()
        clear_runtime_caches()


def test_0002_migration_does_not_import_live_models() -> None:
    source = (PROJECT_ROOT / "migrations/versions/0002_dashboard_shares.py").read_text(
        encoding="utf-8"
    )
    assert "app.models" not in source


def test_0004_corrects_legacy_naive_charge_times_and_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "charge-time-upgrade.sqlite3"
    upgrade_database(monkeypatch, path, "0003_single_instance_hardening")
    engine = create_engine(database_url(path))
    wrong_utc = datetime(2026, 8, 13, 22, 29, 31, tzinfo=UTC)
    expected_utc = datetime(2026, 8, 13, 14, 29, 31, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """INSERT INTO users
                    (id, username, password_hash, role, is_active, created_at, updated_at)
                    VALUES ('user-time', 'time-owner', 'hash', 'admin', 1, :now, :now)"""
                ),
                {"now": wrong_utc},
            )
            connection.execute(
                text(
                    """INSERT INTO bili_accounts
                    (id, user_id, bili_uid, display_name, status, encrypted_cookie,
                     collection_watermark_at, created_at, updated_at)
                    VALUES ('account-time', 'user-time', '123', 'UP', 'ACTIVE', 'cipher',
                            :wrong_utc, :wrong_utc, :wrong_utc)"""
                ),
                {"wrong_utc": wrong_utc},
            )
            connection.execute(
                text(
                    """INSERT INTO charge_records
                    (id, user_id, bili_account_id, event_id, supporter_uid, supporter_name,
                     avatar_url, amount, brokerage, remark, charged_at, raw_data, created_at)
                    VALUES ('charge-time', 'user-time', 'account-time', 'event-time', '456',
                            'supporter', '', 5, 3.36, '', :wrong_utc, :raw_data, :wrong_utc)"""
                ),
                {
                    "wrong_utc": wrong_utc,
                    "raw_data": json.dumps({"schema_version": 1, "ctime": "2026-08-13 22:29:31"}),
                },
            )

        upgrade_database(monkeypatch, path, "head")

        with Session(engine) as session:
            record = session.get(ChargeRecord, "charge-time")
            account = session.get(BiliAccount, "account-time")
            assert record is not None
            assert account is not None
            assert record.charged_at.replace(tzinfo=UTC) == expected_utc
            assert account.collection_watermark_at is not None
            assert account.collection_watermark_at.replace(tzinfo=UTC) == expected_utc
    finally:
        engine.dispose()
        clear_runtime_caches()


def test_0006_merges_legacy_name_variants_and_preserves_delivery_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "canonical-charge-keys.sqlite3"
    upgrade_database(monkeypatch, path, "0005_daily_task")
    engine = create_engine(database_url(path))
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def dedupe(event_id: str) -> str:
        return hashlib.sha256(f"user-1|charge:account-1:{event_id}".encode()).hexdigest()

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """INSERT INTO users
                    (id, username, password_hash, role, is_active, created_at, updated_at)
                    VALUES ('user-1', 'merge-owner', 'hash', 'admin', 1, :now, :now)"""
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    """INSERT INTO bili_accounts
                    (id, user_id, bili_uid, display_name, status, encrypted_cookie,
                     collection_watermark_at, created_at, updated_at)
                    VALUES ('account-1', 'user-1', '123', 'UP', 'active', 'cipher',
                            :now, :now, :now)"""
                ),
                {"now": now},
            )
            raw_named = json.dumps(
                {
                    "schema_version": 1,
                    "mid": "10001",
                    "name": "Alice",
                    "avatar": "https://example.invalid/alice.jpg",
                    "originalThirdCoin": "5",
                    "brokerage": "3.36",
                    "ctime": "2026-08-21 20:00:00",
                }
            )
            raw_uid = json.dumps(
                {
                    "schema_version": 1,
                    "mid": "10001",
                    "name": "10001",
                    "avatar": "",
                    "originalThirdCoin": "5",
                    "brokerage": "3.36",
                    "ctime": "2026-08-21 20:00:00",
                }
            )
            for charge_id, event_id, raw_data, created_at in (
                ("charge-1", "event-1", raw_named, now),
                ("charge-2", "event-2", raw_uid, now.replace(microsecond=1)),
            ):
                connection.execute(
                    text(
                        """INSERT INTO charge_records
                        (id, user_id, bili_account_id, event_id, supporter_uid, supporter_name,
                         avatar_url, amount, brokerage, remark, charged_at, raw_data, created_at)
                        VALUES (:id, 'user-1', 'account-1', :event_id, '10001', :name,
                                :avatar, 5, 3.36, '', :charged_at, :raw_data, :created_at)"""
                    ),
                    {
                        "id": charge_id,
                        "event_id": event_id,
                        "name": "Alice" if charge_id == "charge-1" else "10001",
                        "avatar": "https://example.invalid/alice.jpg"
                        if charge_id == "charge-1"
                        else "",
                        "charged_at": now,
                        "raw_data": raw_data,
                        "created_at": created_at,
                    },
                )
            connection.execute(
                text(
                    """INSERT INTO notification_channels
                    (id, user_id, name, provider, encrypted_config, enabled, created_at, updated_at)
                    VALUES (
                        'channel-1', 'user-1', 'merge-channel', 'webhook',
                        'cipher', 1, :now, :now
                    )"""
                ),
                {"now": now},
            )
            for outbox_id, event_id, created_at in (
                ("outbox-1", "event-1", now),
                ("outbox-2", "event-2", now.replace(microsecond=1)),
            ):
                connection.execute(
                    text(
                        """INSERT INTO notification_outbox
                        (id, user_id, event_type, dedupe_key, payload, status, attempts,
                         available_at, created_at)
                        VALUES (:id, 'user-1', 'new_charge', :dedupe_key, '{}', 'delivered', 1,
                                :now, :created_at)"""
                    ),
                    {
                        "id": outbox_id,
                        "dedupe_key": dedupe(event_id),
                        "now": now,
                        "created_at": created_at,
                    },
                )
                connection.execute(
                    text(
                        """INSERT INTO notification_deliveries
                        (id, user_id, outbox_id, channel_id, status, attempts, delivered_at)
                        VALUES (:id, 'user-1', :outbox_id, 'channel-1', 'succeeded', 1, :now)"""
                    ),
                    {"id": f"delivery-{outbox_id}", "outbox_id": outbox_id, "now": now},
                )

        upgrade_database(monkeypatch, path, "head")

        with engine.connect() as connection:
            charge_rows = (
                connection.execute(
                    text(
                        "SELECT id, supporter_name, avatar_url, record_key, "
                        "notification_eligible FROM charge_records ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
            assert len(charge_rows) == 1
            assert charge_rows[0]["supporter_name"] == "Alice"
            assert charge_rows[0]["avatar_url"] == "https://example.invalid/alice.jpg"
            assert len(charge_rows[0]["record_key"]) == 64
            assert not charge_rows[0]["notification_eligible"]

            merged = (
                connection.execute(
                    text(
                        "SELECT id, status, merged_into_outbox_id "
                        "FROM notification_outbox ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
            assert len(merged) == 2
            merged_row = next(row for row in merged if row["id"] == "outbox-2")
            assert merged_row["status"] == "merged"
            assert merged_row["merged_into_outbox_id"] == "outbox-1"
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM notification_outbox "
                        "WHERE id = 'outbox-2' AND status IN ('pending', 'retry')"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM notification_deliveries")
                ).scalar_one()
                == 2
            )
    finally:
        engine.dispose()
        clear_runtime_caches()
