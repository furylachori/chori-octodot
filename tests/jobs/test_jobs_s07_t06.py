"""Tests for S07-T06: Read backoff for 429/Retry-After, transient GETs, and cancellation with finite fake-time traces.

Covers:
- S07-T06: Read backoff handles 429/Retry-After, transient GET failures and
  cancellation with finite fake-time traces.
- Max-iteration bound on wait polling loops.
- operation_observed predicate reading operation state through S03 store's get_operation.
- WaitActionHandler contract producing ActionResult with OK / WAITING status and exit codes.
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

from octodot.errors import (
    EXIT_OK,
    EXIT_WAITING,
    ErrorCode,
    OctodotError,
)
from octodot.jobs import (
    CancellationToken,
    WaitActionHandler,
    check_operation_observed_predicate,
    execute_wait,
)
from octodot.models import (
    ActionResultStatus,
    Binding,
    Coverage,
    OperationRecord,
    OperationState,
    SessionRecord,
)
from octodot.reads import ReadService, SessionInspection
from octodot.store import SQLiteStore
from octodot.transport import FakeClock


class FakeFailingReadService:
    """Mock ReadService raising configurable sequence of errors before succeeding."""

    def __init__(self, failure_sequence: list[Exception | None]) -> None:
        self.failures = list(failure_sequence)
        self.call_count = 0

    def inspect(self, binding: Binding, fresh: bool = True) -> SessionInspection:
        self.call_count += 1
        if self.failures:
            exc = self.failures.pop(0)
            if exc is not None:
                raise exc

        # Success: return session requiring attention
        session = SessionRecord(
            name=binding.session or "sessions/s1",
            id="s1",
            state="AWAITING_PLAN_APPROVAL",
        )
        return SessionInspection(
            session=session,
            binding=binding,
            state="AWAITING_PLAN_APPROVAL",
        )


class TestJobsS07T06(unittest.TestCase):
    """S07-T06 test suite for backoff, cancellation, and operation_observed predicate."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s07_t06_read_backoff_handles_429_retry_after(self) -> None:
        """S07-T06: 429/Rate-Limited respects Retry-After header with finite fake-time trace."""
        # Setup: first call fails with 429 + retry_after=7.0s, second call succeeds
        err_429 = OctodotError(ErrorCode.RATE_LIMITED, "HTTP 429 Too Many Requests")
        setattr(err_429, "retry_after", 7.0)

        failing_service = FakeFailingReadService([err_429, None])
        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="main",
            session="sessions/s1",
        )

        result = execute_wait(
            predicate="attention",
            timeout_seconds=30.0,
            selection=[binding],
            store=self.store,
            read_service=failing_service,  # type: ignore[arg-type]
            clock=self.clock,
            max_iterations=5,
        )

        # Predicate successfully matched after backoff
        self.assertTrue(result.predicate_matched)
        self.assertEqual(failing_service.call_count, 2)

        # Fake clock recorded the exact Retry-After sleep of 7.0 seconds
        self.assertIn(7.0, self.clock.sleep_calls)

    def test_s07_t06_read_backoff_transient_get_failures(self) -> None:
        """S07-T06: Transient GET failures perform bounded exponential backoff on fake clock."""
        # Setup: two consecutive transient failures before success
        err_500 = OctodotError(ErrorCode.TRANSPORT_ERROR, "HTTP 500 Internal Server Error")
        err_timeout = OctodotError(ErrorCode.TIMEOUT, "GET request timed out")

        failing_service = FakeFailingReadService([err_500, err_timeout, None])
        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="main",
            session="sessions/s1",
        )

        result = execute_wait(
            predicate="attention",
            timeout_seconds=30.0,
            selection=[binding],
            store=self.store,
            read_service=failing_service,  # type: ignore[arg-type]
            clock=self.clock,
            initial_backoff=1.5,
            max_iterations=5,
        )

        self.assertTrue(result.predicate_matched)
        self.assertEqual(failing_service.call_count, 3)

        # Verify finite fake-time backoff trace: 1.5, then 3.0
        self.assertIn(1.5, self.clock.sleep_calls)
        self.assertIn(3.0, self.clock.sleep_calls)

    def test_s07_t06_cancellation_during_wait_and_backoff(self) -> None:
        """S07-T06: Cooperative cancellation aborts wait loop with CANCELLED code."""
        token = CancellationToken()

        err_429 = OctodotError(ErrorCode.RATE_LIMITED, "Rate limited")
        setattr(err_429, "retry_after", 10.0)

        # In hook or after first sleep: trigger cancellation
        call_count = 0

        def inspect_cancelling(binding: Binding, fresh: bool = True) -> SessionInspection:
            nonlocal call_count
            call_count += 1
            token.cancel()  # Signal cancellation immediately
            raise err_429

        mock_service = type("MockService", (), {"inspect": staticmethod(inspect_cancelling)})()
        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="main",
            session="sessions/s1",
        )

        with self.assertRaises(OctodotError) as ctx:
            execute_wait(
                predicate="attention",
                timeout_seconds=30.0,
                selection=[binding],
                store=self.store,
                read_service=mock_service,  # type: ignore[arg-type]
                clock=self.clock,
                cancellation_token=token,
            )

        self.assertEqual(ctx.exception.code, ErrorCode.CANCELLED)

    def test_s07_t06_max_iteration_bound(self) -> None:
        """Every loop has a finite max_iterations bound preventing infinite looping."""
        # Always in progress, not matching
        session = SessionRecord(name="sessions/s_loop", id="s_loop", state="IN_PROGRESS")
        mock_insp = SessionInspection(session=session, binding=Binding("default", 0, "", "", session="s_loop"), state="IN_PROGRESS")
        mock_service = type("MockService", (), {"inspect": lambda *a, **k: mock_insp})()

        result = execute_wait(
            predicate="attention",
            timeout_seconds=1000.0,  # huge timeout
            selection=["sessions/s_loop"],
            store=self.store,
            read_service=mock_service,  # type: ignore[arg-type]
            clock=self.clock,
            max_iterations=3,  # strictly capped at 3 iterations
        )

        # Terminated via iteration cap, yielded waiting
        self.assertFalse(result.predicate_matched)
        assert result.job_id is not None
        job_record = self.store.load_job(result.job_id)
        assert job_record is not None
        self.assertEqual(job_record["status"], "waiting")

    def test_s07_operation_observed_predicate_reads_through_store(self) -> None:
        """operation_observed predicate reads operation state through S03 store's get_operation."""
        op_id = "op_test_observed_42"

        # 1. Operation not yet in store -> False
        self.assertFalse(check_operation_observed_predicate(self.store, op_id))

        # 2. Operation in store with effect_observed=False -> False
        self.store.save_operation(
            OperationRecord(
                operation_id=op_id,
                state=OperationState.ACCEPTED,
                request_hash="hash42",
            )
        )
        self.assertFalse(check_operation_observed_predicate(self.store, op_id))

        # 3. Update operation with effect_observed=True -> True!
        self.store.transition_operation_state(
            operation_id=op_id,
            to_state=OperationState.EFFECT_OBSERVED,
            effect_observed=True,
        )
        self.assertTrue(check_operation_observed_predicate(self.store, op_id))

    def test_s07_wait_action_handler_produces_ok_and_waiting_action_results(self) -> None:
        """WaitActionHandler conforms to ActionHandler and produces OK (exit 0) and WAITING (exit 2)."""
        handler = WaitActionHandler(store=self.store, clock=self.clock)

        # Case A: Operation already observed -> ActionResultStatus.OK, exit_code 0
        op_id = "op_action_test"
        self.store.save_operation(
            OperationRecord(operation_id=op_id, state=OperationState.EFFECT_OBSERVED, request_hash="h1")
        )
        self.store.update_operation_evidence_flags(operation_id=op_id, effect_observed=True)

        action_matched = {
            "action_id": "wait_01",
            "op": "wait",
            "args": {
                "predicate": "operation_observed",
                "operation_id": op_id,
                "timeout_seconds": 5.0,
            },
        }
        res_ok = handler.handle(action_matched, {"profile": "default", "plan_id": "p1"})
        self.assertEqual(res_ok.status, ActionResultStatus.OK)
        self.assertEqual(res_ok.exit_code, EXIT_OK)
        data_ok = dict(res_ok.data)
        self.assertTrue(data_ok["predicate_matched"])

        # Case B: Operation not observed, timeout yields -> ActionResultStatus.WAITING, exit_code 2
        action_timeout = {
            "action_id": "wait_02",
            "op": "wait",
            "args": {
                "predicate": "operation_observed",
                "operation_id": "op_nonexistent",
                "timeout_seconds": 1.0,
            },
        }
        res_waiting = handler.handle(action_timeout, {"profile": "default", "plan_id": "p1"})
        self.assertEqual(res_waiting.status, ActionResultStatus.WAITING)
        self.assertEqual(res_waiting.exit_code, EXIT_WAITING)
        data_waiting = dict(res_waiting.data)
        self.assertFalse(data_waiting["predicate_matched"])
        self.assertIsNotNone(data_waiting["job_id"])


if __name__ == "__main__":
    unittest.main()
