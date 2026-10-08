"""Migration runner for octodot SQLite database.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Callable

from octodot.errors import ErrorCode, StateStoreError
from octodot.migrations import (
    v001_initial,
    v002_evidence_and_manifests,
    v003_plan_scoped_action_results,
)

CURRENT_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class Migration:
    """Executable migration step."""

    version: int
    description: str
    up: Callable[[sqlite3.Connection], None]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=v001_initial.VERSION,
        description=v001_initial.DESCRIPTION,
        up=v001_initial.up,
    ),
    Migration(
        version=v002_evidence_and_manifests.VERSION,
        description=v002_evidence_and_manifests.DESCRIPTION,
        up=v002_evidence_and_manifests.up,
    ),
    Migration(
        version=v003_plan_scoped_action_results.VERSION,
        description=v003_plan_scoped_action_results.DESCRIPTION,
        up=v003_plan_scoped_action_results.up,
    ),
)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Retrieve the current recorded schema version from the database.

    Returns 0 if schema_migrations table does not exist.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    if cursor.fetchone() is None:
        return 0
    cursor.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def migrate_database(
    conn: sqlite3.Connection,
    target_version: int | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> int:
    """Migrate the database step-wise up to target_version (default: CURRENT_SCHEMA_VERSION).

    Enforces schema_version compatibility:
    - If database is newer than CURRENT_SCHEMA_VERSION, raises StateStoreError(SCHEMA_TOO_NEW).
    - Runs each migration step within a transaction.
    - Honors crash injection hooks before and after commit.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        )
        """
    )
    conn.commit()

    current_version = get_schema_version(conn)
    if current_version > CURRENT_SCHEMA_VERSION:
        raise StateStoreError(
            ErrorCode.SCHEMA_TOO_NEW,
            f"Database schema version {current_version} is newer than supported version {CURRENT_SCHEMA_VERSION}",
        )

    target = CURRENT_SCHEMA_VERSION if target_version is None else target_version
    if current_version >= target:
        return current_version

    for m in MIGRATIONS:
        if current_version < m.version <= target:
            conn.execute("BEGIN IMMEDIATE")
            try:
                m.up(conn)
                now_iso = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                    (m.version, now_iso, m.description),
                )
                if fault_hook:
                    fault_hook("before_migration_commit")
                conn.commit()
                if fault_hook:
                    fault_hook("after_migration_commit")
            except Exception:
                conn.rollback()
                raise

    return get_schema_version(conn)
