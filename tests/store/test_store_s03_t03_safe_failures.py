"""Tests for S03-T03: Safe failures on disk-full, locked/corrupt DB, newer schema, unsafe dir, and unavailable lock.

Fail safely on disk-full, locked/corrupt DB, incompatible newer schema,
unsafe state directory and unavailable lock support.
"""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.errors import ErrorCode, StateStoreError
from octodot.migrations import CURRENT_SCHEMA_VERSION
from octodot.store import SQLiteStore


class TestStoreS03T03SafeFailures(unittest.TestCase):
    """S03-T03 Failure modes: disk full, locked/corrupt DB, newer schema, unsafe dir, lock unsupported."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s03_t03_fail_safely_on_disk_full(self) -> None:
        """S03-T03: Disk-full condition fails safely with StateStoreError and rolls back."""
        store = SQLiteStore(self.test_dir)
        try:
            with self.assertRaises(StateStoreError) as ctx:
                with store.transaction():
                    raise sqlite3.OperationalError("database or disk is full")
            self.assertIn("disk is full", ctx.exception.message.lower())
        finally:
            store.close()

    def test_s03_t03_fail_safely_on_locked_db(self) -> None:
        """S03-T03: Locked SQLite database fails safely with STATE_LOCKED."""
        store = SQLiteStore(self.test_dir)
        try:
            with self.assertRaises(StateStoreError) as ctx:
                with store.transaction():
                    raise sqlite3.OperationalError("database is locked")
            self.assertEqual(ctx.exception.code, ErrorCode.STATE_LOCKED)
        finally:
            store.close()

    def test_s03_t03_fail_safely_on_corrupt_db(self) -> None:
        """S03-T03: Corrupted SQLite database fails safely with STATE_CORRUPT."""
        db_path = os.path.join(self.test_dir, "octodot.db")
        # Overwrite with garbage data
        with open(db_path, "wb") as f:
            f.write(b"NOT A SQLITE DATABASE HEADER GIBBERISH" * 50)
        os.chmod(db_path, 0o600)

        with self.assertRaises(StateStoreError) as ctx:
            SQLiteStore(self.test_dir)
        self.assertEqual(ctx.exception.code, ErrorCode.STATE_CORRUPT)

    def test_s03_t03_fail_safely_on_incompatible_newer_schema(self) -> None:
        """S03-T03: Incompatible newer schema fails safely with SCHEMA_TOO_NEW."""
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
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
            (CURRENT_SCHEMA_VERSION + 10, "2026-10-01T00:00:00Z", "Future version"),
        )
        conn.commit()
        conn.close()

        with self.assertRaises(StateStoreError) as ctx:
            SQLiteStore(self.test_dir)
        self.assertEqual(ctx.exception.code, ErrorCode.SCHEMA_TOO_NEW)

    def test_s03_t03_fail_safely_on_unsafe_state_directory_symlink(self) -> None:
        """S03-T03: Symlink state directory is refused with UNSAFE_STATE_DIR."""
        real_dir = tempfile.mkdtemp()
        symlink_dir = os.path.join(self.test_dir, "symlink_state")
        os.symlink(real_dir, symlink_dir)
        try:
            with self.assertRaises(StateStoreError) as ctx:
                SQLiteStore(symlink_dir)
            self.assertEqual(ctx.exception.code, ErrorCode.UNSAFE_STATE_DIR)
            self.assertIn("symbolic link", ctx.exception.message)
        finally:
            shutil.rmtree(real_dir, ignore_errors=True)

    def test_s03_t03_fail_safely_on_unsafe_state_directory_permissions(self) -> None:
        """S03-T03: World-writable or group-writable state directory is refused with UNSAFE_STATE_DIR."""
        unsafe_dir = os.path.join(self.test_dir, "world_writable")
        os.makedirs(unsafe_dir, mode=0o777)
        os.chmod(unsafe_dir, 0o777)

        with self.assertRaises(StateStoreError) as ctx:
            SQLiteStore(unsafe_dir)
        self.assertEqual(ctx.exception.code, ErrorCode.UNSAFE_STATE_DIR)

        # Group-writable
        group_dir = os.path.join(self.test_dir, "group_writable")
        os.makedirs(group_dir, mode=0o770)
        os.chmod(group_dir, 0o770)

        with self.assertRaises(StateStoreError) as ctx2:
            SQLiteStore(group_dir)
        self.assertEqual(ctx2.exception.code, ErrorCode.UNSAFE_STATE_DIR)

    def test_s03_t03_fail_safely_on_unsafe_state_directory_not_owned(self) -> None:
        """S03-T03: State directory not owned by current user is refused with UNSAFE_STATE_DIR."""
        owned_dir = os.path.join(self.test_dir, "not_owned")
        os.makedirs(owned_dir, mode=0o700)

        real_stat = os.stat(owned_dir)
        mock_stat = os.stat_result((
            real_stat.st_mode,
            real_stat.st_ino,
            real_stat.st_dev,
            real_stat.st_nlink,
            real_stat.st_uid + 9999,  # Fake different owner
            real_stat.st_gid,
            real_stat.st_size,
            real_stat.st_atime,
            real_stat.st_mtime,
            real_stat.st_ctime,
        ))

        with patch("os.stat", return_value=mock_stat):
            with self.assertRaises(StateStoreError) as ctx:
                SQLiteStore(owned_dir)
            self.assertEqual(ctx.exception.code, ErrorCode.UNSAFE_STATE_DIR)
            self.assertIn("not owned", ctx.exception.message)

    def test_s03_t03_fail_safely_on_unavailable_lock_support(self) -> None:
        """S03-T03: Unsupported file locking fails safely with LOCK_UNSUPPORTED."""
        store = SQLiteStore(self.test_dir)
        try:
            with patch("fcntl.flock", side_effect=OSError(errno.ENOTSUP, "Operation not supported")):
                with self.assertRaises(StateStoreError) as ctx:
                    store.acquire_lock()
                self.assertEqual(ctx.exception.code, ErrorCode.LOCK_UNSUPPORTED)
        finally:
            store.close()
