"""Migration v002: Add evidence, authorization records, observations, activities, manifests, and indexes."""

from __future__ import annotations

import sqlite3

VERSION = 2
DESCRIPTION = "Add operation evidence, authorization records, observations, manifests, and indexes"


def up(conn: sqlite3.Connection) -> None:
    """Apply migration v002."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(profiles)")
    cols = {row[1] for row in cursor.fetchall()}
    if "journal_seq" not in cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN journal_seq INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operation_evidence (
            evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (operation_id) REFERENCES operations (operation_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authorization_records (
            grant_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            profile_epoch INTEGER NOT NULL,
            source TEXT NOT NULL,
            repository TEXT NOT NULL,
            branch TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            context_hash TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            publication_scope TEXT NOT NULL,
            authorizing_source TEXT NOT NULL,
            session TEXT,
            expiry TEXT,
            revocation_ref TEXT,
            max_attempts INTEGER NOT NULL DEFAULT 1,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            observation_id TEXT PRIMARY KEY,
            scan_id TEXT,
            binding_json TEXT,
            sources_json TEXT,
            sessions_json TEXT,
            activities_json TEXT,
            coverage_json TEXT,
            candidate_bundle_json TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activities (
            activity_id TEXT PRIMARY KEY,
            session_id TEXT,
            activity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            originator TEXT,
            create_time TEXT,
            data_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manifests (
            artifact_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            byte_count INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_acked ON events (acked)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operations_state ON operations (state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_profile ON checkpoints (profile)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scans_profile ON scans (profile)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_evidence_op ON operation_evidence (operation_id)"
    )
