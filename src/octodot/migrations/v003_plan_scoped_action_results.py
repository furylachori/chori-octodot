"""Migration v003: Plan-scoped action results identity (plan_id, action_id) and durable plan bindings."""

from __future__ import annotations

import json
import sqlite3

VERSION = 3
DESCRIPTION = "Namespace action results by (plan_id, action_id) and add durable plan_bindings table"


def up(conn: sqlite3.Connection) -> None:
    """Apply migration v003.

    - Recreates action_results with composite primary key (plan_id, action_id).
    - Preserves all existing rows from v002.
    - Creates durable plan_bindings table (plan_id -> plan_hash, created_at).
    - Seeds plan_bindings from existing action_results.
    """
    cursor = conn.cursor()

    # 1. Check if action_results exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='action_results'")
    has_ar = cursor.fetchone() is not None

    if has_ar:
        # Create temporary table with new schema
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_results_v3 (
                action_id TEXT NOT NULL,
                plan_id TEXT NOT NULL DEFAULT '',
                op TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                error_code TEXT,
                coverage_json TEXT,
                data_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, action_id)
            )
            """
        )
        # Migrate existing rows, ensuring plan_id is not null
        conn.execute(
            """
            INSERT OR REPLACE INTO action_results_v3 (
                action_id, plan_id, op, status, exit_code, error_code, coverage_json, data_json, created_at
            )
            SELECT
                action_id, COALESCE(plan_id, ''), op, status, exit_code, error_code, coverage_json, data_json, created_at
            FROM action_results
            """
        )
        conn.execute("DROP TABLE action_results")
        conn.execute("ALTER TABLE action_results_v3 RENAME TO action_results")
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_results (
                action_id TEXT NOT NULL,
                plan_id TEXT NOT NULL DEFAULT '',
                op TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                error_code TEXT,
                coverage_json TEXT,
                data_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, action_id)
            )
            """
        )

    # 2. Create index on plan_id
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_results_plan ON action_results (plan_id)"
    )

    # 3. Create durable plan_bindings table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_bindings (
            plan_id TEXT PRIMARY KEY,
            plan_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # 4. Seed plan_bindings from existing action_results rows
    cursor.execute(
        "SELECT plan_id, data_json, created_at FROM action_results WHERE plan_id != ''"
    )
    for row in cursor.fetchall():
        pid = row[0]
        dj_str = row[1]
        cat = row[2]
        if not dj_str:
            continue
        try:
            dj = json.loads(dj_str)
            ph = dj.get("_plan_hash") or dj.get("plan_hash")
            if ph and isinstance(ph, str):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO plan_bindings (plan_id, plan_hash, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (pid, ph, cat),
                )
        except Exception:
            pass
