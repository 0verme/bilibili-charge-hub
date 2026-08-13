"""Database migration readiness checks used by startup and ``/readyz``."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG_PATH = PROJECT_ROOT / "alembic.ini"


@dataclass(frozen=True)
class MigrationReadiness:
    """A comparison between revisions installed in the database and code heads."""

    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.reason is None
            and len(self.current_heads) == 1
            and self.current_heads == self.expected_heads
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "ok" if self.ready else "not_ready",
            "current_heads": list(self.current_heads),
            "expected_heads": list(self.expected_heads),
        }
        if self.reason:
            result["reason"] = self.reason
        return result


def get_code_heads(config_path: Path = ALEMBIC_CONFIG_PATH) -> tuple[str, ...]:
    """Return all Alembic heads shipped with the running application."""

    config_path = config_path.resolve()
    config = Config(str(config_path))
    script_location = Path(config.get_main_option("script_location"))
    if not script_location.is_absolute():
        config.set_main_option(
            "script_location", str((config_path.parent / script_location).resolve())
        )
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


def check_migration_readiness(
    bind: Connection | Engine,
    *,
    expected_heads: Iterable[str] | None = None,
) -> MigrationReadiness:
    """Require one database head that exactly matches the one code head.

    A missing version table, an unversioned database, or multiple heads is never
    considered ready. Callers handle connectivity and Alembic configuration
    errors separately so those failures can be reported without exposing details.
    """

    expected = tuple(sorted(expected_heads if expected_heads is not None else get_code_heads()))
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return check_migration_readiness(connection, expected_heads=expected)

    if not inspect(bind).has_table("alembic_version"):
        return MigrationReadiness((), expected, "missing_version_table")

    current = tuple(sorted(MigrationContext.configure(bind).get_current_heads()))
    if len(expected) != 1:
        return MigrationReadiness(current, expected, "code_has_multiple_heads")
    if not current:
        return MigrationReadiness(current, expected, "database_has_no_revision")
    if len(current) != 1:
        return MigrationReadiness(current, expected, "database_has_multiple_heads")
    if current != expected:
        return MigrationReadiness(current, expected, "revision_mismatch")
    return MigrationReadiness(current, expected)
