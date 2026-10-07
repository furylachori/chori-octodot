"""Tests for S03-T02: Crash injection and recovery at commit boundaries.

Crash before and after each migration commit and scan-commit boundary;
reopen without partial checkpoint or event loss.
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

from octodot.migrations import CURRENT_SCHEMA_VERSION, get_schema_version, migrate_database
from octodot.models import Coverage, Event, Observation
from octodot.store import SQLiteStore


class SimulatedCrashError(Exception):
    """Simulated deterministic crash error."""


class TestStoreS03T02CrashRecovery(unittest.TestCase):
    """S03-T02 Crash recovery tests at migration, scan, and checkpoint commit boundaries."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s03_t02_crash_before_migration_commit(self) -> None:
        """S03-T02: Crash before migration commit aborts cleanly without partial schema."""
        db_path = os.path.join(self.test_dir, "octodot.db")
        conn = sqlite3.connect(db_path)

        def crash_hook(point: str) -> None:
            if point == "before_migration_commit":
                raise SimulatedCrashError("Crash before migration commit")

        with self.assertRaises(SimulatedCrashError):
            migrate_database(conn, fault_hook=crash_hook)

        # Database should have rolled back
        self.assertEqual(get_schema_version(conn), 0)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='profiles'")
        self.assertIsNone(cursor.fetchone())
        conn.close()

        # Reopen with normal store and complete migration
        store = SQLiteStore(self.test_dir, auto_migrate=True)
        try:
            conn2 = sqlite3.connect(str(store.db_path))
            self.assertEqual(get_schema_version(conn2), CURRENT_SCHEMA_VERSION)
            conn2.close()
        finally:
            store.close()

    def test_s03_t02_crash_after_migration_commit(self) -> None:
        """S03-T02: Crash after migration commit retains committed migration."""
        db_path = os.path.join(self.test_dir, "octodot.db")
        conn = sqlite3.connect(db_path)

        def crash_hook(point: str) -> None:
            if point == "after_migration_commit":
                raise SimulatedCrashError("Crash after migration commit")

        with self.assertRaises(SimulatedCrashError):
            migrate_database(conn, target_version=1, fault_hook=crash_hook)

        # Migration was committed before the crash
        self.assertEqual(get_schema_version(conn), 1)
        conn.close()

        # Reopen cleanly
        store = SQLiteStore(self.test_dir, auto_migrate=True)
        try:
            conn2 = sqlite3.connect(str(store.db_path))
            self.assertEqual(get_schema_version(conn2), CURRENT_SCHEMA_VERSION)
            conn2.close()
        finally:
            store.close()

    def test_s03_t02_crash_before_and_after_scan_commit(self) -> None:
        """S03-T02: Crash before scan commit rolls back; crash after preserves committed scan."""
        store = SQLiteStore(self.test_dir)
        try:
            # Seed an event
            event = Event(
                event_id="evt_preserve_1",
                event_type="test",
                resource_id="res_1",
                payload=(("k", "v"),),
            )
            store.save_event(event)

            # --- Crash before scan commit ---
            store.begin_scan("scan_crash_before", "default")
            store.save_observation(
                Observation(metadata=(("test", "data"),)),
                scan_id="scan_crash_before",
            )

            store.set_fault_hook(
                lambda pt: (_ for _ in ()).throw(SimulatedCrashError("crash"))
                if pt == "before_scan_commit"
                else None
            )

            with self.assertRaises(SimulatedCrashError):
                store.commit_scan("scan_crash_before", complete=True)

            store.set_fault_hook(None)

            # Reopen store and check state
            store.close()
            store = SQLiteStore(self.test_dir)

            # Scan should not be complete
            scan = store.get_scan("scan_crash_before")
            self.assertIsNotNone(scan)
            self.assertFalse(scan["complete"])
            # No checkpoint should exist
            self.assertIsNone(store.get_checkpoint("default"))
            # Seeded event must not be lost
            events = store.get_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_id, "evt_preserve_1")

            # --- Crash after scan commit ---
            store.begin_scan("scan_crash_after", "default")
            store.set_fault_hook(
                lambda pt: (_ for _ in ()).throw(SimulatedCrashError("crash"))
                if pt == "after_scan_commit"
                else None
            )

            with self.assertRaises(SimulatedCrashError):
                store.commit_scan("scan_crash_after", complete=True)

            store.set_fault_hook(None)
            store.close()

            # Reopen and check: scan was committed before crash
            store = SQLiteStore(self.test_dir)
            scan_after = store.get_scan("scan_crash_after")
            self.assertIsNotNone(scan_after)
            self.assertTrue(scan_after["complete"])
            # No partial checkpoint was created
            self.assertIsNone(store.get_checkpoint("default"))
            # Seeded event intact
            events = store.get_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_id, "evt_preserve_1")
        finally:
            store.close()

    def test_s03_t02_crash_before_and_after_checkpoint_commit(self) -> None:
        """S03-T02: Crash at checkpoint commit boundary; reopen without partial checkpoint or event loss."""
        store = SQLiteStore(self.test_dir)
        try:
            # Prepare a complete scan
            store.begin_scan("scan_cp", "default")
            store.commit_scan("scan_cp", complete=True)

            # Seed an event
            event = Event(
                event_id="evt_checkpoint_test",
                event_type="test",
                resource_id="res_cp",
                payload=(("a", "b"),),
            )
            store.save_event(event)

            # Crash before checkpoint commit
            store.set_fault_hook(
                lambda pt: (_ for _ in ()).throw(SimulatedCrashError("crash"))
                if pt == "before_checkpoint_commit"
                else None
            )

            with self.assertRaises(SimulatedCrashError):
                store.advance_checkpoint("cp_crash_before", "default", "scan_cp")

            store.set_fault_hook(None)
            store.close()

            # Reopen: no partial checkpoint
            store = SQLiteStore(self.test_dir)
            self.assertIsNone(store.get_checkpoint("default"))
            # Event is intact
            self.assertEqual(len(store.get_events()), 1)

            # Crash after checkpoint commit
            store.set_fault_hook(
                lambda pt: (_ for _ in ()).throw(SimulatedCrashError("crash"))
                if pt == "after_checkpoint_commit"
                else None
            )

            with self.assertRaises(SimulatedCrashError):
                store.advance_checkpoint("cp_crash_after", "default", "scan_cp", position="pos_1")

            store.set_fault_hook(None)
            store.close()

            # Reopen: checkpoint was committed, event intact
            store = SQLiteStore(self.test_dir)
            cp = store.get_checkpoint("default")
            self.assertIsNotNone(cp)
            self.assertEqual(cp["checkpoint_id"], "cp_crash_after")
            self.assertEqual(cp["position"], "pos_1")
            self.assertEqual(len(store.get_events()), 1)
        finally:
            store.close()
