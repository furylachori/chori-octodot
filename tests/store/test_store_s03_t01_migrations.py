"""Tests for S03-T01: SQLite schema migrations and versioning.

Greenfield: Migrate empty DB to current version and step-wise v1->vN migrations
preserving stable IDs/acks of seeded rows; record schema_version; refuse newer schema (schema_too_new).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.errors import ErrorCode, StateStoreError
from octodot.migrations import (
    CURRENT_SCHEMA_VERSION,
    get_schema_version,
    migrate_database,
)
from octodot.models import OperationState
from octodot.store import SQLiteStore


class TestStoreS03T01Migrations(unittest.TestCase):
    """S03-T01 schema migrations, stable identities, and schema version validation."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s03_t01_migrate_empty_database(self) -> None:
        """S03-T01: Migrate empty database to current schema version and record version."""
        store = SQLiteStore(self.test_dir, auto_migrate=True)
        try:
            conn = sqlite3.connect(str(store.db_path))
            version = get_schema_version(conn)
            self.assertEqual(version, CURRENT_SCHEMA_VERSION)

            # Check that all required tables exist
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            expected_tables = {
                "schema_migrations",
                "profiles",
                "scans",
                "checkpoints",
                "jobs",
                "operations",
                "events",
                "receiver_receipts",
                "action_results",
                "operation_evidence",
                "authorization_records",
                "observations",
                "activities",
                "manifests",
            }
            self.assertTrue(
                expected_tables.issubset(tables),
                f"Missing tables: {expected_tables - tables}",
            )
            conn.close()
        finally:
            store.close()

    def test_s03_t01_stepwise_migration_preserves_seeded_rows_and_acks(self) -> None:
        """S03-T01: Step-wise v1 -> v2 migration preserving stable IDs and acks of seeded rows."""
        db_path = os.path.join(self.test_dir, "octodot.db")
        conn = sqlite3.connect(db_path)

        # Apply only migration v1
        version_applied = migrate_database(conn, target_version=1)
        self.assertEqual(version_applied, 1)
        self.assertEqual(get_schema_version(conn), 1)

        # Seed v1 database with events, sends, receipts
        # Acknowledged event
        conn.execute(
            """
            INSERT INTO events (event_id, event_type, resource_id, payload_json, created_at, acked, acked_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            ("evt_ack_1", "message_received", "res_001", '{"text": "hello"}', "2026-10-01T00:00:00Z", "2026-10-01T00:01:00Z"),
        )
        # Unacknowledged event
        conn.execute(
            """
            INSERT INTO events (event_id, event_type, resource_id, payload_json, created_at, acked)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("evt_unack_2", "message_received", "res_002", '{"text": "world"}', "2026-10-01T00:02:00Z"),
        )
        # In-flight send (DISPATCHING)
        conn.execute(
            """
            INSERT INTO operations (operation_id, state, request_hash, profile, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("op_inflight", "dispatching", "hash_inflight", "default", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
        )
        # Accepted send (ACCEPTED)
        conn.execute(
            """
            INSERT INTO operations (operation_id, state, request_hash, profile, api_accepted, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            ("op_accepted", "accepted", "hash_accepted", "default", "2026-10-01T00:01:00Z", "2026-10-01T00:01:00Z"),
        )
        # Unknown send (UNKNOWN)
        conn.execute(
            """
            INSERT INTO operations (operation_id, state, request_hash, profile, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("op_unknown", "unknown", "hash_unknown", "default", "2026-10-01T00:02:00Z", "2026-10-01T00:02:00Z"),
        )
        # Receipt
        conn.execute(
            """
            INSERT INTO receiver_receipts (receipt_id, event_id, receiver_accepted, timestamp)
            VALUES (?, ?, 1, ?)
            """,
            ("rcpt_001", "evt_ack_1", "2026-10-01T00:00:30Z"),
        )
        conn.commit()
        conn.close()

        # Reopen with SQLiteStore which auto-migrates to CURRENT_SCHEMA_VERSION (v2)
        store = SQLiteStore(self.test_dir, auto_migrate=True)
        try:
            # Check schema version updated
            conn2 = sqlite3.connect(str(store.db_path))
            self.assertEqual(get_schema_version(conn2), CURRENT_SCHEMA_VERSION)
            conn2.close()

            # Verify stable IDs and acknowledgements preserved exactly
            self.assertTrue(store.is_event_acked("evt_ack_1"))
            self.assertFalse(store.is_event_acked("evt_unack_2"))

            events = store.get_events(limit=10)
            self.assertEqual(len(events), 2)
            event_ids = {e.event_id for e in events}
            self.assertEqual(event_ids, {"evt_ack_1", "evt_unack_2"})

            # Verify operations and their states preserved
            op_inf = store.get_operation("op_inflight")
            self.assertIsNotNone(op_inf)
            self.assertEqual(op_inf.state, OperationState.DISPATCHING)

            op_acc = store.get_operation("op_accepted")
            self.assertIsNotNone(op_acc)
            self.assertEqual(op_acc.state, OperationState.ACCEPTED)
            self.assertTrue(op_acc.api_accepted)

            op_unk = store.get_operation("op_unknown")
            self.assertIsNotNone(op_unk)
            self.assertEqual(op_unk.state, OperationState.UNKNOWN)

            # Verify receipt preserved
            rcpt = store.get_receipt("rcpt_001")
            self.assertIsNotNone(rcpt)
            self.assertEqual(rcpt.event_id, "evt_ack_1")
            self.assertTrue(rcpt.receiver_accepted)
        finally:
            store.close()

    def test_s03_t01_refuse_newer_schema(self) -> None:
        """S03-T01: Refuse database with newer incompatible schema_version (schema_too_new)."""
        db_path = os.path.join(self.test_dir, "octodot.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
            """
        )
        future_version = CURRENT_SCHEMA_VERSION + 1
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
            (future_version, "2026-10-01T00:00:00Z", "Future schema"),
        )
        conn.commit()
        conn.close()

        # Opening SQLiteStore should raise StateStoreError with SCHEMA_TOO_NEW
        with self.assertRaises(StateStoreError) as ctx:
            SQLiteStore(self.test_dir)
        self.assertEqual(ctx.exception.code, ErrorCode.SCHEMA_TOO_NEW)
