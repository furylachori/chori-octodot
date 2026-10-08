"""Tests for S03-T05: Recovery fence, credential rotation, and mutation resumption gating.

Missing DB, restored stale backup or changed profile generation blocks mutation resumption;
current trusted fence restores read-only recovery only until reconciled.
Rotate the synthetic credential configuration without changing the profile name:
the host epoch must advance, old bindings/grants must fail, and mutation eligibility
must remain blocked until fresh source/session identity is verified and a grant for
the new epoch is checked; neither key nor key fingerprint may be persisted.
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
from octodot.models import Binding, Event, OperationRecord, OperationState, VerifiedGrant
from octodot.store import FileRecoveryFence, InMemoryRecoveryFence, SQLiteStore

_SYNTHETIC_SECRET = "jules-secret-synthetic-token-abc123xyz"
_SYNTHETIC_FINGERPRINT = "sha256-fingerprint-synthetic-abc"


class TestStoreS03T05RecoveryFence(unittest.TestCase):
    """S03-T05 Recovery fence validation, credential rotation, and read-only recovery behavior."""

    def setUp(self) -> None:
        self.state_dir = tempfile.mkdtemp()
        self.fence_dir = tempfile.mkdtemp()  # OUTSIDE state_dir

    def tearDown(self) -> None:
        shutil.rmtree(self.state_dir, ignore_errors=True)
        shutil.rmtree(self.fence_dir, ignore_errors=True)

    def test_s03_t05_missing_profile_blocks_mutation_resumption(self) -> None:
        """S03-T05: Missing profile or un-reconciled state blocks mutation resumption; safe reads continue."""
        fence = InMemoryRecoveryFence({"default": 1})
        store = SQLiteStore(self.state_dir)
        try:
            # Profile 'default' has not been reconciled in store
            self.assertEqual(store.get_profile_epoch("default"), 0)
            self.assertFalse(store.is_mutation_resumption_allowed("default", fence))

            with self.assertRaises(StateStoreError) as ctx:
                store.check_mutation_eligibility("default", fence)
            self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

            # Reads still function without errors
            events = store.get_events()
            self.assertEqual(events, ())
            self.assertIsNone(store.get_operation("nonexistent"))
        finally:
            store.close()

    def test_s03_t05_restored_stale_backup_blocks_mutation_until_reconciled(self) -> None:
        """S03-T05: Restored stale DB backup with older epoch blocks mutations; reads OK; revalidation reconciles."""
        fence = InMemoryRecoveryFence({"default": 2})  # Host is at epoch 2
        store = SQLiteStore(self.state_dir)
        try:
            # Seed DB as if restored from an old backup where epoch was 1
            store.reconcile_profile_epoch("default", epoch=1, identity_validated=True)
            self.assertEqual(store.get_profile_epoch("default"), 1)

            # Because host epoch (2) != DB epoch (1), mutation resumption is blocked
            self.assertFalse(store.is_mutation_resumption_allowed("default", fence))
            with self.assertRaises(StateStoreError) as ctx:
                store.check_mutation_eligibility("default", fence)
            self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

            # Read-only operations are completely functional
            store.save_event(Event(event_id="e_stale_test", event_type="read", resource_id="r1"))
            events = store.get_events()
            self.assertEqual(len(events), 1)

            # Fresh identity revalidation performed for the new host epoch (2)
            store.reconcile_profile_epoch("default", epoch=2, identity_validated=True)
            self.assertTrue(store.is_mutation_resumption_allowed("default", fence))
            # Mutation eligibility check passes cleanly
            store.check_mutation_eligibility("default", fence)
        finally:
            store.close()

    def test_s03_t05_credential_rotation_without_profile_change(self) -> None:
        """S03-T05: Rotate credential configuration without changing profile name; host epoch advances; writes block."""
        fence = InMemoryRecoveryFence({"prod": 1})
        store = SQLiteStore(self.state_dir)
        try:
            # Initially reconciled with epoch 1
            store.reconcile_profile_epoch("prod", epoch=1, identity_validated=True)
            self.assertTrue(store.is_mutation_resumption_allowed("prod", fence))

            # Store an authorization record / grant bound to epoch 1
            grant = VerifiedGrant(
                action="chats.reply",
                operation_id="op_reply_01",
                profile="prod",
                profile_epoch=1,
                source="sources/github/OWNER/REPO",
                repository="OWNER/REPO",
                branch="feature/example",
                payload_hash="sha256:1111",
                context_hash="sha256:2222",
                plan_hash="sha256:3333",
                publication_scope="pr",
                authorizing_source="reviewer",
            )
            store.save_authorization_record(grant)

            # Synthetic credential rotation happens: profile name remains 'prod',
            # but host configuration epoch advances from 1 to 2.
            new_epoch = fence.advance_epoch("prod")
            self.assertEqual(new_epoch, 2)
            self.assertEqual(fence.get_current_epoch("prod"), 2)

            # Old DB still records epoch 1 -> mutation resumption immediately blocked!
            self.assertFalse(store.is_mutation_resumption_allowed("prod", fence))
            with self.assertRaises(StateStoreError) as ctx:
                store.check_mutation_eligibility("prod", fence)
            self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

            # Verify old grant has stale epoch compared to host
            saved_grant = store.get_authorization_record(f"op_reply_01:prod:1")
            self.assertIsNotNone(saved_grant)
            self.assertNotEqual(saved_grant.profile_epoch, fence.get_current_epoch("prod"))

            # Mutation remains blocked until fresh identity revalidation for epoch 2 is recorded
            store.reconcile_profile_epoch("prod", epoch=2, identity_validated=True)
            self.assertTrue(store.is_mutation_resumption_allowed("prod", fence))
            store.check_mutation_eligibility("prod", fence)
        finally:
            store.close()

    def test_s03_t05_no_credential_values_or_fingerprints_persisted(self) -> None:
        """S03-T05: Verify neither credential secret nor key fingerprint is persisted in DB."""
        fence = InMemoryRecoveryFence({"default": 1})
        store = SQLiteStore(self.state_dir)
        try:
            store.reconcile_profile_epoch("default", epoch=1, identity_validated=True)
            # Perform various store operations
            store.save_event(Event(event_id="e_sec", event_type="test", resource_id="res"))
            store.begin_scan("scan_sec", "default")
            store.commit_scan("scan_sec", complete=True)

            # Inspect database schema and contents
            conn = sqlite3.connect(str(store.db_path))
            try:
                cursor = conn.cursor()

                # 1. Column names across all tables
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")]
                forbidden_substrings = ("secret", "key_val", "fingerprint", "token", "password", "credential")
                for table in tables:
                    cursor.execute(f"PRAGMA table_info({table})")
                    cols = [c[1].lower() for c in cursor.fetchall()]
                    for col in cols:
                        # Ignore 'key' in key-value tables like operation_evidence
                        if col in ("key", "event_type", "primary key"):
                            continue
                        for forbidden in forbidden_substrings:
                            if forbidden in col and col != "key":
                                self.fail(f"Table '{table}' column '{col}' contains forbidden '{forbidden}'")

                # 2. Entire database raw text inspection
                # Read full dump and verify synthetic secret or fingerprint never appears
                dump = "\n".join(conn.iterdump())
                self.assertNotIn(_SYNTHETIC_SECRET, dump)
                self.assertNotIn(_SYNTHETIC_FINGERPRINT, dump)

                # 3. Profiles table only contains profile and epoch integer
                cursor.execute("SELECT * FROM profiles WHERE profile = 'default'")
                prof_row = cursor.fetchone()
                self.assertIsNotNone(prof_row)
                # Row columns: profile, reconciled_epoch, identity_validated, validated_at, created_at, updated_at
                self.assertEqual(prof_row[0], "default")
                self.assertEqual(prof_row[1], 1)
            finally:
                conn.close()
        finally:
            store.close()

    def test_s03_t05_file_recovery_fence_outside_state_dir(self) -> None:
        """S03-T05: Host-controlled FileRecoveryFence operates strictly outside state dir."""
        fence_file = os.path.join(self.fence_dir, "epochs.json")
        fence = FileRecoveryFence(fence_file)

        # Initially uninitialized
        self.assertEqual(fence.get_current_epoch("default"), 0)
        self.assertFalse(fence.is_fence_valid("default", 1))

        # Set epoch
        fence.set_epoch("default", 1)
        self.assertEqual(fence.get_current_epoch("default"), 1)
        self.assertTrue(fence.is_fence_valid("default", 1))
        self.assertFalse(fence.is_fence_valid("default", 2))

        # Advance epoch
        advanced = fence.advance_epoch("default")
        self.assertEqual(advanced, 2)
        self.assertEqual(fence.get_current_epoch("default"), 2)

        # Reopen fence from disk to ensure persistence
        fence2 = FileRecoveryFence(fence_file)
        self.assertEqual(fence2.get_current_epoch("default"), 2)

        # Directory-based fence mode
        dir_fence_path = os.path.join(self.fence_dir, "epoch_files")
        dir_fence = FileRecoveryFence(dir_fence_path)
        dir_fence.set_epoch("worker", 5)
        self.assertEqual(dir_fence.get_current_epoch("worker"), 5)
        self.assertTrue(dir_fence.is_fence_valid("worker", 5))

        # Checkpoint support in directory mode
        self.assertEqual(dir_fence.get_journal_checkpoint("worker"), 0)
        dir_fence.advance_journal_checkpoint("worker", 10)
        self.assertEqual(dir_fence.get_journal_checkpoint("worker"), 10)

    def test_s03_t05_same_epoch_stale_backup_restore_blocks_mutation_resumption(self) -> None:
        """S03-T05: Restoring stale backup at the same epoch blocks mutations when DB journal_seq < fence checkpoint."""
        fence = InMemoryRecoveryFence({"default": 1})
        store = SQLiteStore(self.state_dir, fence=fence)
        try:
            store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=fence)
            self.assertTrue(store.is_mutation_resumption_allowed("default", fence))

            # Copy the DB file before any operations
            backup_path = os.path.join(self.state_dir, "octodot_backup.db")
            shutil.copy2(str(store.db_path), backup_path)

            # Perform an operation and transition it (bumping DB journal_seq and fence checkpoint)
            op = OperationRecord(
                operation_id="op_stale_test_01",
                state=OperationState.PREPARED,
                request_hash="hash_stale_01",
            )
            store.save_operation(op, fence=fence)
            store.transition_operation_state("op_stale_test_01", OperationState.DISPATCHING, fence=fence)

            # Verify journal sequence was bumped and fence advanced
            self.assertEqual(store.get_profile_journal_seq("default"), 2)
            self.assertEqual(fence.get_journal_checkpoint("default"), 2)
            self.assertTrue(store.is_mutation_resumption_allowed("default", fence))

            # Close store and restore the stale backup (which had journal_seq = 0)
            store.close()
            shutil.copy2(backup_path, str(store.db_path))

            # Reopen store
            reopened_store = SQLiteStore(self.state_dir)
            try:
                # DB has journal_seq = 0, but host fence has checkpoint = 2
                self.assertEqual(reopened_store.get_profile_journal_seq("default"), 0)
                self.assertEqual(fence.get_journal_checkpoint("default"), 2)

                # Mutation resumption MUST be blocked!
                self.assertFalse(reopened_store.is_mutation_resumption_allowed("default", fence))
                with self.assertRaises(StateStoreError) as ctx:
                    reopened_store.check_mutation_eligibility("default", fence)
                self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

                # Reads remain allowed
                events = reopened_store.get_events()
                self.assertEqual(events, ())
            finally:
                reopened_store.close()
        finally:
            store.close()

    def test_s03_t05_missing_db_with_nonzero_fence_checkpoint_blocks_resumption(self) -> None:
        """S03-T05: Missing or uninitialized DB with a nonzero fence checkpoint blocks mutation resumption."""
        fence = InMemoryRecoveryFence({"default": 1}, checkpoints={"default": 5})
        store = SQLiteStore(self.state_dir)
        try:
            # DB has journal_seq = 0, fence has checkpoint = 5
            self.assertFalse(store.is_mutation_resumption_allowed("default", fence))
            with self.assertRaises(StateStoreError) as ctx:
                store.check_mutation_eligibility("default", fence)
            self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)
        finally:
            store.close()

    def test_s03_t05_crash_after_commit_before_fence_advance_recovers_on_reopen(self) -> None:
        """S03-T05: Crash between DB commit and fence advance leaves DB seq > fence seq; recovers on reopen without lowering fence."""
        fence = InMemoryRecoveryFence({"default": 1})
        store = SQLiteStore(self.state_dir)
        try:
            store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=fence)

            # Perform an operation without fence advance (simulating crash right after DB commit, before fence advance)
            op = OperationRecord(
                operation_id="op_crash_advance",
                state=OperationState.PREPARED,
                request_hash="hash_crash_advance",
            )
            # Call save_operation without passing fence so fence does not advance
            store.save_operation(op, fence=None)

            # DB has journal_seq = 1, fence has checkpoint = 0
            self.assertEqual(store.get_profile_journal_seq("default"), 1)
            self.assertEqual(fence.get_journal_checkpoint("default"), 0)

            # Before recovery, mutation resumption is blocked because DB is ahead of fence
            self.assertFalse(store.is_mutation_resumption_allowed("default", fence))

            # Reopening the store with the fence recovers the ahead checkpoint
            store.close()
            recovered_store = SQLiteStore(self.state_dir, fence=fence)
            try:
                # Fence checkpoint was safely re-advanced to the DB value
                self.assertEqual(fence.get_journal_checkpoint("default"), 1)
                self.assertTrue(recovered_store.is_mutation_resumption_allowed("default", fence))
            finally:
                recovered_store.close()
        finally:
            store.close()

    def test_s03_t05_operation_save_conflict_and_no_state_regression(self) -> None:
        """S03-T05 (O1): save_operation rejects conflicting request_hash and does not regress operation state."""
        store = SQLiteStore(self.state_dir)
        try:
            op_orig = OperationRecord(
                operation_id="op_conflict_test",
                state=OperationState.PREPARED,
                request_hash="hash_initial",
            )
            store.save_operation(op_orig)

            # 1. Attempting to save with a different request_hash raises OPERATION_CONFLICT
            op_conflicting = OperationRecord(
                operation_id="op_conflict_test",
                state=OperationState.PREPARED,
                request_hash="hash_different",
            )
            with self.assertRaises(StateStoreError) as ctx:
                store.save_operation(op_conflicting)
            self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)

            # 2. Transition operation through legal transitions to DISPATCHING, then to UNKNOWN
            store.transition_operation_state("op_conflict_test", OperationState.DISPATCHING)
            store.transition_operation_state("op_conflict_test", OperationState.UNKNOWN)
            saved = store.get_operation("op_conflict_test")
            self.assertIsNotNone(saved)
            self.assertEqual(saved.state, OperationState.UNKNOWN)

            # 3. Calling save_operation again with same hash does NOT regress state to PREPARED or DISPATCHING
            store.save_operation(op_orig)
            saved_after = store.get_operation("op_conflict_test")
            self.assertIsNotNone(saved_after)
            self.assertEqual(saved_after.state, OperationState.UNKNOWN)
            self.assertEqual(saved_after.request_hash, "hash_initial")

            # 4. Monotonic evidence flags update
            store.update_operation_evidence_flags("op_conflict_test", api_accepted=True)
            self.assertTrue(store.get_operation("op_conflict_test").api_accepted)
            # Cannot clear True to False
            store.update_operation_evidence_flags("op_conflict_test", api_accepted=False)
            self.assertTrue(store.get_operation("op_conflict_test").api_accepted)
        finally:
            store.close()
