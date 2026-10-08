"""Tests for S07-T05: Workflow lock release between wait iterations enabling ACK and authorized reply.

Covers:
- S07-T05: Lock release between iterations enables ACK and authorized reply;
  reacquisition reloads current durable state.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.errors import ErrorCode, StateStoreError
from octodot.jobs import execute_wait
from octodot.models import Event, OperationRecord, OperationState
from octodot.store import SQLiteStore
from octodot.transport import FakeClock


class TestJobsS07T05(unittest.TestCase):
    """S07-T05 test suite for workflow lock release and durable state reload."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s07_t05_lock_release_enables_ack_between_iterations(self) -> None:
        """S07-T05: Lock release between iterations enables ACK, reacquisition reloads current state."""
        # 1. Seed two events into store
        ev1 = Event.create(event_id="ev_ack_target", event_type="message", resource_id="r1")
        self.store.save_event(ev1)

        ack_attempted = False
        ack_succeeded = False

        # Hook called between wait iterations while workflow lock is released
        def external_action_hook(iteration: int) -> None:
            nonlocal ack_attempted, ack_succeeded
            if iteration == 0 and not ack_attempted:
                ack_attempted = True
                # A separate process or worker attempts to acquire the lock to ACK the event
                # If wait loop had NOT released the lock, acquire_lock would fail with timeout!
                external_store = SQLiteStore(self.test_dir)
                try:
                    locked = external_store.acquire_lock(timeout=1.0)
                    if locked:
                        external_store.ack_event("ev_ack_target")
                        ack_succeeded = True
                        external_store.release_lock()
                finally:
                    external_store.close()

        # Execute wait: Initially waiting for an event to remain unacked, or wait loop running
        # Seed another event that will trigger new_events after iteration 0
        ev2 = Event.create(event_id="ev_new_incoming", event_type="message", resource_id="r2")

        # In hook, also seed a new event that will be observed on reacquire
        def external_seed_and_ack_hook(iteration: int) -> None:
            nonlocal ack_attempted, ack_succeeded
            if iteration == 0 and not ack_attempted:
                ack_attempted = True
                ext_store = SQLiteStore(self.test_dir)
                try:
                    locked = ext_store.acquire_lock(timeout=1.0)
                    if locked:
                        ext_store.ack_event("ev_ack_target")
                        ext_store.save_event(ev2)
                        ack_succeeded = True
                        ext_store.release_lock()
                finally:
                    ext_store.close()

        result = execute_wait(
            predicate="new_events",
            timeout_seconds=10.0,
            store=self.store,
            clock=self.clock,
            poll_interval=1.0,
            max_iterations=5,
            on_iteration_hook=external_seed_and_ack_hook,
        )

        self.assertTrue(ack_attempted)
        self.assertTrue(ack_succeeded)
        # Wait reacquired the lock, reloaded durable state from SQLite, and observed ev2
        self.assertTrue(result.predicate_matched)
        self.assertTrue(self.store.is_event_acked("ev_ack_target"))

    def test_s07_t05_lock_release_enables_authorized_reply_and_operation_observed(self) -> None:
        """S07-T05: Lock release between iterations enables authorized reply, reacquire observes operation."""
        # 1. Seed operation in dispatching state
        self.store.save_operation(
            OperationRecord(
                operation_id="op_reply_01",
                state=OperationState.ACCEPTED,
                request_hash="hash_01",
            )
        )

        reply_updated = False

        # In hook between iterations: execute authorized reply outcome
        def reply_hook(iteration: int) -> None:
            nonlocal reply_updated
            if iteration == 0 and not reply_updated:
                ext_store = SQLiteStore(self.test_dir)
                try:
                    if ext_store.acquire_lock(timeout=1.0):
                        # Transition operation to EFFECT_OBSERVED
                        ext_store.transition_operation_state(
                            operation_id="op_reply_01",
                            to_state=OperationState.EFFECT_OBSERVED,
                            effect_observed=True,
                        )
                        reply_updated = True
                        ext_store.release_lock()
                finally:
                    ext_store.close()

        # Wait for operation_observed
        result = execute_wait(
            predicate="operation_observed",
            timeout_seconds=10.0,
            operation_id="op_reply_01",
            store=self.store,
            clock=self.clock,
            poll_interval=1.0,
            max_iterations=5,
            on_iteration_hook=reply_hook,
        )

        self.assertTrue(reply_updated)
        # Lock reacquire reloaded durable operation from store
        self.assertTrue(result.predicate_matched)
        op_loaded = self.store.get_operation("op_reply_01")
        assert op_loaded is not None
        self.assertTrue(op_loaded.effect_observed)

    def test_s07_lock_held_yields_waiting_without_mutation(self) -> None:
        """S07-T05: When workflow lock is held by another store instance, execute_wait yields waiting without mutation."""
        job_id = "job_locked_hold_test"
        self.store.create_job(
            job_id=job_id,
            profile="default",
            plan_id="p_locked",
            status="waiting",
            details={"predicate": "new_events"},
        )
        orig_job = self.store.load_job(job_id)
        assert orig_job is not None

        # Seed an event that WOULD trigger new_events predicate if evaluated
        ev = Event.create(event_id="ev_would_match", event_type="message", resource_id="r_match")
        self.store.save_event(ev)

        # Second store instance holds the workflow lock
        ext_store = SQLiteStore(self.test_dir)
        try:
            self.assertTrue(ext_store.acquire_lock(timeout=1.0))

            # Calling execute_wait with lock_timeout=0.1
            result = execute_wait(
                predicate="new_events",
                timeout_seconds=5.0,
                job_id=job_id,
                store=self.store,
                clock=self.clock,
                lock_timeout=0.1,
                poll_interval=1.0,
                max_iterations=5,
            )

            # Assert predicate was NOT matched (not evaluated)
            self.assertFalse(result.predicate_matched)
            self.assertEqual(result.job_id, job_id)
            self.assertTrue(result.resumed)
        finally:
            ext_store.release_lock()
            ext_store.close()

        # Check job in store: state was not mutated (still waiting, same updated_at and details)
        after_job = self.store.load_job(job_id)
        assert after_job is not None
        self.assertEqual(after_job["status"], "waiting")
        self.assertEqual(after_job["details"], orig_job["details"])
        self.assertEqual(after_job["updated_at"], orig_job["updated_at"])


if __name__ == "__main__":
    unittest.main()
