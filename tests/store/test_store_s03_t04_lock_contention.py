"""Tests for S03-T04: Two-process lock contention and short transaction isolation.

Two processes contend on the global lock; wait release permits receipt handling
and a separately authorized action; no transaction spans network/sleep.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.models import OperationRecord, OperationState, Receipt
from octodot.store import SQLiteStore

_WORKER_SCRIPT = """
import sys
import time

sys.path.insert(0, sys.argv[1])
from octodot.store import SQLiteStore

state_dir = sys.argv[2]
store = SQLiteStore(state_dir)

if not store.acquire_lock(timeout=1.0):
    print("FAILED_TO_LOCK", flush=True)
    sys.exit(1)

print("LOCKED", flush=True)

# Hold the exclusive lock for a bounded duration
time.sleep(0.3)

store.release_lock()
print("RELEASED", flush=True)
store.close()
sys.exit(0)
"""


class TestStoreS03T04LockContention(unittest.TestCase):
    """S03-T04 Two-process lock contention via subprocesses and transaction duration bounds."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s03_t04_two_process_lock_contention(self) -> None:
        """S03-T04: Two processes contend on the global lock; wait release permits receipt handling and authorized action."""
        parent_store = SQLiteStore(self.test_dir)
        try:
            # Start child process that acquires and holds lock for 0.3s
            proc = subprocess.Popen(
                [sys.executable, "-c", _WORKER_SCRIPT, _SRC_DIR, self.test_dir],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Wait for child to acquire lock
            assert proc.stdout is not None
            first_line = proc.stdout.readline().strip()
            self.assertEqual(first_line, "LOCKED", f"Child output: {first_line}")

            # While child holds the lock, parent immediate acquire (timeout=0.0) must fail
            acquired_early = parent_store.acquire_lock(timeout=0.0)
            self.assertFalse(acquired_early, "Parent should not acquire lock while child holds it")

            # Parent waits for child release with bounded timeout
            acquired_after = parent_store.acquire_lock(timeout=3.0)
            self.assertTrue(acquired_after, "Parent should acquire lock after child releases")

            # Verify child released and exited cleanly
            exit_code = proc.wait(timeout=2.0)
            self.assertEqual(exit_code, 0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

            # Parent performs receipt handling under the exclusive lock
            receipt = Receipt(
                receipt_id="rcpt_contention_01",
                event_id="evt_contention_01",
                receiver_accepted=True,
                channel="webhook",
                timestamp="2026-10-01T12:00:00Z",
                metadata=(("status", "accepted"),),
            )
            parent_store.save_receipt(receipt)

            # Parent performs a separately authorized action (operation transition)
            op = OperationRecord(
                operation_id="op_contention_01",
                state=OperationState.PREPARED,
                request_hash="hash_contention_01",
            )
            parent_store.save_operation(op)

            transitioned = parent_store.transition_operation_state(
                "op_contention_01",
                OperationState.DISPATCHING,
                ticket_id="ticket_auth_01",
            )
            self.assertEqual(transitioned.state, OperationState.DISPATCHING)
            self.assertEqual(transitioned.ticket_id, "ticket_auth_01")

            parent_store.release_lock()

            # Verify persisted state
            saved_rcpt = parent_store.get_receipt("rcpt_contention_01")
            self.assertIsNotNone(saved_rcpt)
            self.assertTrue(saved_rcpt.receiver_accepted)

            saved_op = parent_store.get_operation("op_contention_01")
            self.assertIsNotNone(saved_op)
            self.assertEqual(saved_op.state, OperationState.DISPATCHING)
        finally:
            parent_store.close()

    def test_s03_t04_no_transaction_spans_network_or_sleep(self) -> None:
        """S03-T04: Transactions are short and closed immediately; no transaction is held across sleep."""
        store = SQLiteStore(self.test_dir)
        try:
            # Inside a short transaction
            with store.transaction():
                store._conn.execute(
                    "INSERT INTO events (event_id, event_type, resource_id, payload_json) VALUES ('e1', 't', 'r', '{}')"
                )
            # Outside transaction: connection must not be in transaction
            self.assertFalse(
                store._conn.in_transaction,
                "Connection should not be in an uncommitted transaction",
            )

            # Sleep outside transaction
            time.sleep(0.05)
            self.assertFalse(store._conn.in_transaction)

            # Next transaction works cleanly
            with store.transaction():
                store._conn.execute(
                    "INSERT INTO events (event_id, event_type, resource_id, payload_json) VALUES ('e2', 't', 'r', '{}')"
                )
            self.assertFalse(store._conn.in_transaction)
        finally:
            store.close()
