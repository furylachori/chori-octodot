"""Tests for S08 Mutation Journal: single-attempt dispatch, ticket authority, and crash matrix.

Standard library only. Compatible with Python 3.10+.
Covers:
- S08-T01: Crash matrix at all 7 boundaries proving <= 1 local POST attempt across restart.
- S08-T02: Same ID/same hash replay, different hash conflict, unresolved session/logical-task gating.
- S08-T03: Uncertain outcomes (timeout, disconnect, 5xx, malformed success, save failure) and clear 4xx rejections.
- S08-T06 (journal gating): Missing/stale DB and invalid grants cannot produce a dispatch ticket.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Mapping
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.api import JulesClient, compute_mutation_request_hash
from octodot.authorization import DisabledGrantVerifier, FakeGrantVerifier
from octodot.contracts import (
    canonical_hash,
    request_hash,
)
from octodot.errors import ErrorCode, OctodotError, StateStoreError
from octodot.journal import Journal, extract_logical_task_marker
from octodot.models import (
    Binding,
    DispatchTicket,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


def make_sample_action(
    op_id: str = "op-1",
    session: str | None = "sessions/sess-1",
    prompt: str = "Test prompt",
    marker: str | None = None,
    profile_epoch: int = 1,
) -> PreparedAction:
    """Helper to build a sample PreparedAction."""
    binding = Binding(
        profile="default",
        profile_epoch=profile_epoch,
        source="sources/github/OWNER/REPO",
        repository="OWNER/REPO",
        starting_branch="main",
        session=session,
    )
    payload: dict[str, Any] = {"prompt": prompt}
    if marker:
        payload["logical_task_marker"] = marker

    if session:
        target = f"/v1alpha/{session}:sendMessage"
        req_h = compute_mutation_request_hash(target, {"prompt": prompt})
        action_name = "chats.reply"
    else:
        target = "/v1alpha/sessions"
        req_h = compute_mutation_request_hash(target, payload)
        action_name = "tasks.create"

    return PreparedAction(
        action=action_name,
        operation_id=op_id,
        binding=binding,
        payload=payload,
        payload_hash=canonical_hash(payload),
        context_hash=canonical_hash({"ctx": 1}),
        request_hash=req_h,
        publication_scope="none",
        plan_hash=canonical_hash({"plan": 1}),
    )


def make_sample_grant(action: PreparedAction) -> VerifiedGrant:
    """Helper to build a matching VerifiedGrant."""
    return VerifiedGrant(
        action=action.action,
        operation_id=action.operation_id,
        profile=action.binding.profile,
        profile_epoch=action.binding.profile_epoch,
        source=action.binding.source,
        repository=action.binding.repository,
        branch=action.binding.starting_branch or "main",
        payload_hash=action.payload_hash,
        context_hash=action.context_hash,
        plan_hash=action.plan_hash,
        publication_scope=action.publication_scope,
        authorizing_source="grant-1",
        session=action.binding.session,
    )


class PersistentLoggingTransport(FixtureTransport):
    """Fixture transport that records POST calls in a shared/persistent list across restarts."""

    def __init__(
        self,
        call_log: list[tuple[str, str, bytes | None]],
        responses: Mapping[tuple[str, str], TransportOutcome] | None = None,
        fault_on_send: bool = False,
    ) -> None:
        super().__init__(responses=responses)
        self.call_log = call_log
        self.fault_on_send = fault_on_send

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> TransportOutcome:
        if method.upper() == "POST":
            self.call_log.append((method, path, body))
        if self.fault_on_send:
            raise ConnectionResetError("Deterministic network failure during send")
        return super().request(
            method=method,
            path=path,
            headers=headers,
            query=query,
            body=body,
            timeout=timeout,
        )


class TestS08T01CrashMatrix(unittest.TestCase):
    """S08-T01: Kill/restart at 7 boundaries proves <= 1 local POST attempt across restart."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.clock = FakeClock()
        self.verifier = FakeGrantVerifier(single_use=False)
        self.post_log: list[tuple[str, str, bytes | None]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_store(self, fault_hook: Any = None) -> SQLiteStore:
        store = SQLiteStore(state_dir=self.test_dir, fence=self.fence, fault_hook=fault_hook)
        store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        return store

    def _init_journal(self, store: SQLiteStore) -> Journal:
        journal = Journal(store=store, verifier=self.verifier, fence=self.fence, clock=self.clock)
        orig_prepare = journal.prepare
        def auto_prep(action, grant=None, **kwargs):
            if grant is not None and "authorization_ref" not in kwargs:
                ref = action.operation_id
                self.verifier.register_grant(ref, grant)
                kwargs["authorization_ref"] = ref
            return orig_prepare(action, grant, **kwargs)
        journal.prepare = auto_prep
        return journal

    def test_s08_t01_boundary_1_before_intent_commit(self) -> None:
        """S08-T01 Boundary 1: Crash before intent commit -> <= 1 local POST."""
        action = make_sample_action(op_id="op-b1", session="sessions/sess-b1")
        grant = make_sample_grant(action)

        # 1. Simulate crash before intent commit by injecting fault in store
        def fault_hook(point: str) -> None:
            if point == "before_save_intent":
                raise RuntimeError("Crash before intent commit")

        store1 = self._init_store()
        journal1 = self._init_journal(store1)

        # Before intent commit fails
        with self.assertRaises(Exception):
            with store1.transaction(fault_point_before="before_save_intent"):
                journal1.prepare(action, grant)
        store1.close()

        # 2. Restart and execute full mutation workflow
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")},
        )
        client = JulesClient(transport=transport, ticket_authority=journal2, clock=self.clock)

        rec = journal2.prepare(action, grant)
        self.assertEqual(rec.state, OperationState.PREPARED)
        ticket = journal2.begin_dispatch(action.operation_id, action.request_hash)
        resp = client.sessions_send_message(ticket, action.binding.session, action.payload)
        self.assertEqual(resp.outcome.status, 200)
        outcome_rec = journal2.record_outcome(ticket, resp)
        self.assertEqual(outcome_rec.state, OperationState.ACCEPTED)
        store2.close()

        # Proves <= 1 local POST attempt across restart
        self.assertEqual(len(self.post_log), 1)

    def test_s08_t01_boundary_2_after_intent_commit(self) -> None:
        """S08-T01 Boundary 2: Crash after intent commit (before dispatching) -> <= 1 local POST."""
        action = make_sample_action(op_id="op-b2", session="sessions/sess-b2")
        grant = make_sample_grant(action)

        # 1. Commit intent, then crash before begin_dispatch
        store1 = self._init_store()
        journal1 = self._init_journal(store1)
        rec = journal1.prepare(action, grant)
        self.assertEqual(rec.state, OperationState.PREPARED)
        store1.close()  # Simulated crash

        # 2. Restart: operation is still PREPARED, proceeds to dispatch
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        existing = journal2.get_record(action.operation_id)
        self.assertIsNotNone(existing)
        self.assertEqual(existing.state, OperationState.PREPARED)

        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")},
        )
        client = JulesClient(transport=transport, ticket_authority=journal2, clock=self.clock)

        ticket = journal2.begin_dispatch(action.operation_id, action.request_hash)
        resp = client.sessions_send_message(ticket, action.binding.session, action.payload)
        self.assertEqual(resp.outcome.status, 200)
        journal2.record_outcome(ticket, resp)
        store2.close()

        self.assertEqual(len(self.post_log), 1)

    def test_s08_t01_boundary_3_before_dispatching_commit(self) -> None:
        """S08-T01 Boundary 3: Crash during begin_dispatch before commit -> <= 1 local POST."""
        action = make_sample_action(op_id="op-b3", session="sessions/sess-b3")
        grant = make_sample_grant(action)

        store1 = self._init_store()
        journal1 = self._init_journal(store1)
        journal1.prepare(action, grant)

        # Simulate crash by raising error before dispatch commit
        def fault_hook(pt: str) -> None:
            if pt == "before_dispatching_commit":
                raise RuntimeError("Crash before dispatch commit")

        journal1.set_fault_hook(fault_hook)
        with self.assertRaises(RuntimeError):
            journal1.begin_dispatch(action.operation_id, action.request_hash)
        store1.close()

        # 2. Restart: operation is still PREPARED, no ticket was issued
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        existing = journal2.get_record(action.operation_id)
        self.assertEqual(existing.state, OperationState.PREPARED)

        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")},
        )
        client = JulesClient(transport=transport, ticket_authority=journal2, clock=self.clock)

        ticket = journal2.begin_dispatch(action.operation_id, action.request_hash)
        resp = client.sessions_send_message(ticket, action.binding.session, action.payload)
        self.assertEqual(resp.outcome.status, 200)
        journal2.record_outcome(ticket, resp)
        store2.close()

        self.assertEqual(len(self.post_log), 1)

    def test_s08_t01_boundary_4_after_dispatching_commit(self) -> None:
        """S08-T01 Boundary 4: Crash after dispatching commit (before send) -> <= 1 local POST (0 sent)."""
        action = make_sample_action(op_id="op-b4", session="sessions/sess-b4")
        grant = make_sample_grant(action)

        # 1. Dispatching committed, ticket issued, then crash BEFORE transport POST
        store1 = self._init_store()
        journal1 = self._init_journal(store1)
        journal1.prepare(action, grant)
        old_ticket = journal1.begin_dispatch(action.operation_id, action.request_hash)
        store1.close()  # Crash occurs before client sends request

        # 2. Restart: recovery transitions DISPATCHING -> UNKNOWN
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        recovered_rec = journal2.get_record(action.operation_id)
        self.assertIsNotNone(recovered_rec)
        self.assertEqual(recovered_rec.state, OperationState.UNKNOWN)

        # Proving: old ticket cannot be redeemed across restart for recovered UNKNOWN op
        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")},
        )
        client = JulesClient(transport=transport, ticket_authority=journal2, clock=self.clock)

        with self.assertRaises(OctodotError) as ctx:
            client.sessions_send_message(old_ticket, action.binding.session, action.payload)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_INVALID)

        # Proving: cannot begin_dispatch again for UNKNOWN operation (raises non-conflict UNRESOLVED_INTENT)
        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.UNRESOLVED_INTENT)

        store2.close()
        # Exactly 0 local POST attempts (<= 1)
        self.assertEqual(len(self.post_log), 0)

    def test_s08_t01_boundary_5_after_send(self) -> None:
        """S08-T01 Boundary 5: Crash after send (before response received) -> <= 1 local POST (1 sent)."""
        action = make_sample_action(op_id="op-b5", session="sessions/sess-b5")
        grant = make_sample_grant(action)

        store1 = self._init_store()
        journal1 = self._init_journal(store1)
        journal1.prepare(action, grant)
        ticket = journal1.begin_dispatch(action.operation_id, action.request_hash)

        # Simulate network disconnect / crash during POST execution
        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            fault_on_send=True,
        )
        client = JulesClient(transport=transport, ticket_authority=journal1, clock=self.clock)

        with self.assertRaises(ConnectionResetError):
            client.sessions_send_message(ticket, action.binding.session, action.payload)
        store1.close()  # Process terminates/crashes

        # 2. Restart
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        recovered_rec = journal2.get_record(action.operation_id)
        self.assertEqual(recovered_rec.state, OperationState.UNKNOWN)

        # Retry cannot dispatch
        with self.assertRaises(OctodotError):
            journal2.begin_dispatch(action.operation_id, action.request_hash)

        store2.close()
        # Exactly 1 local POST attempt was recorded in persistent transport log
        self.assertEqual(len(self.post_log), 1)

    def test_s08_t01_boundary_6_after_response(self) -> None:
        """S08-T01 Boundary 6: Crash after response received (before record_outcome) -> <= 1 local POST."""
        action = make_sample_action(op_id="op-b6", session="sessions/sess-b6")
        grant = make_sample_grant(action)

        store1 = self._init_store()
        journal1 = self._init_journal(store1)
        journal1.prepare(action, grant)
        ticket = journal1.begin_dispatch(action.operation_id, action.request_hash)

        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")},
        )
        client = JulesClient(transport=transport, ticket_authority=journal1, clock=self.clock)

        # Send succeeds and returns MutationResponse, but crash occurs before record_outcome
        resp = client.sessions_send_message(ticket, action.binding.session, action.payload)
        self.assertEqual(resp.outcome.status, 200)
        store1.close()  # Crash before journal1.record_outcome()

        # 2. Restart: operation was still DISPATCHING in store, recovers to UNKNOWN
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        recovered = journal2.get_record(action.operation_id)
        self.assertEqual(recovered.state, OperationState.UNKNOWN)

        store2.close()
        self.assertEqual(len(self.post_log), 1)

    def test_s08_t01_boundary_7_during_acceptance_persistence(self) -> None:
        """S08-T01 Boundary 7: Failure during acceptance persistence -> remains UNKNOWN on restart."""
        action = make_sample_action(op_id="op-b7", session="sessions/sess-b7")
        grant = make_sample_grant(action)

        store1 = self._init_store()
        journal1 = self._init_journal(store1)
        journal1.prepare(action, grant)
        ticket = journal1.begin_dispatch(action.operation_id, action.request_hash)

        target = f"/v1alpha/{action.binding.session}:sendMessage"
        transport = PersistentLoggingTransport(
            call_log=self.post_log,
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")},
        )
        client = JulesClient(transport=transport, ticket_authority=journal1, clock=self.clock)
        resp = client.sessions_send_message(ticket, action.binding.session, action.payload)

        # Inject failure during record_outcome persistence
        def fault_hook(pt: str) -> None:
            if pt == "during_acceptance_persistence":
                raise RuntimeError("DB write failure during acceptance")

        journal1.set_fault_hook(fault_hook)
        with self.assertRaises(RuntimeError):
            journal1.record_outcome(ticket, resp)
        store1.close()

        # 2. Restart: operation was not persisted as ACCEPTED; recovers to UNKNOWN
        store2 = self._init_store()
        journal2 = self._init_journal(store2)
        recovered = journal2.get_record(action.operation_id)
        self.assertEqual(recovered.state, OperationState.UNKNOWN)

        store2.close()
        self.assertEqual(len(self.post_log), 1)


class TestS08T02ReplayAndConflict(unittest.TestCase):
    """S08-T02: Same ID/same hash returns recorded state; different hash conflicts; new ID cannot bypass unresolved intent."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.verifier = FakeGrantVerifier(single_use=False)
        self.journal = Journal(store=self.store, verifier=self.verifier, fence=self.fence)
        orig_prepare = self.journal.prepare
        def auto_prep(action, grant=None, **kwargs):
            if grant is not None and "authorization_ref" not in kwargs:
                ref = action.operation_id
                self.verifier.register_grant(ref, grant)
                kwargs["authorization_ref"] = ref
            return orig_prepare(action, grant, **kwargs)
        self.journal.prepare = auto_prep

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s08_t02_same_id_same_hash_returns_recorded_state(self) -> None:
        """S08-T02 (F1b): Replay of same operation_id and same request_hash returns recorded state unchanged.
        
        Callers dispatch only when state is PREPARED. begin_dispatch on an op that is not PREPARED
        raises a non-conflict error: UNRESOLVED_INTENT for UNKNOWN/DISPATCHING, and an error naming
        the terminal state for others. OPERATION_CONFLICT is only for same ID with different hash.
        """
        # 1. State: PREPARED
        action = make_sample_action(op_id="op-replay-1", session="sessions/sess-1")
        grant = make_sample_grant(action)

        rec1 = self.journal.prepare(action, grant)
        self.assertEqual(rec1.state, OperationState.PREPARED)

        # Repeat prepare with identical ID and identical request hash -> returns recorded state unchanged
        rec2 = self.journal.prepare(action, grant)
        self.assertEqual(rec2.operation_id, rec1.operation_id)
        self.assertEqual(rec2.state, OperationState.PREPARED)
        self.assertEqual(rec2.request_hash, rec1.request_hash)

        # 2. Advance to UNKNOWN
        ticket1 = self.journal.begin_dispatch(action.operation_id, action.request_hash)
        outcome_timeout = TransportOutcome(status=0, uncertain_effect=True)
        rec_unknown = self.journal.record_outcome(ticket1, outcome_timeout)
        self.assertEqual(rec_unknown.state, OperationState.UNKNOWN)

        # Replay prepare on UNKNOWN -> returns recorded UNKNOWN record unchanged without OPERATION_CONFLICT
        replay_unknown = self.journal.prepare(action, grant)
        self.assertEqual(replay_unknown.state, OperationState.UNKNOWN)
        self.assertEqual(replay_unknown.operation_id, action.operation_id)

        # begin_dispatch on UNKNOWN raises non-conflict UNRESOLVED_INTENT (zero ticket, zero POST)
        with self.assertRaises(OctodotError) as ctx:
            self.journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.UNRESOLVED_INTENT)

        # 3. Terminal state: ACCEPTED
        action_acc = make_sample_action(op_id="op-replay-acc", session="sessions/sess-acc")
        grant_acc = make_sample_grant(action_acc)
        self.journal.prepare(action_acc, grant_acc)
        ticket_acc = self.journal.begin_dispatch(action_acc.operation_id, action_acc.request_hash)
        outcome_200 = TransportOutcome(status=200, body=b"{}")
        rec_acc = self.journal.record_outcome(ticket_acc, outcome_200)
        self.assertEqual(rec_acc.state, OperationState.ACCEPTED)

        # Replay prepare on ACCEPTED -> returns recorded ACCEPTED record unchanged without OPERATION_CONFLICT
        replay_acc = self.journal.prepare(action_acc, grant_acc)
        self.assertEqual(replay_acc.state, OperationState.ACCEPTED)

        # begin_dispatch on ACCEPTED raises error naming terminal state (zero ticket, zero POST)
        with self.assertRaises(OctodotError) as ctx:
            self.journal.begin_dispatch(action_acc.operation_id, action_acc.request_hash)
        self.assertEqual(ctx.exception.code, "accepted")
        self.assertIn("accepted", ctx.exception.message)

    def test_s08_t02_same_id_different_hash_conflicts(self) -> None:
        """S08-T02: Same operation_id with different request_hash raises OPERATION_CONFLICT."""
        action1 = make_sample_action(op_id="op-conf-1", prompt="Initial prompt")
        grant1 = make_sample_grant(action1)
        self.journal.prepare(action1, grant1)

        # Same op_id, different prompt -> different request_hash
        action2 = make_sample_action(op_id="op-conf-1", prompt="Modified prompt")
        grant2 = make_sample_grant(action2)

        with self.assertRaises(OctodotError) as ctx:
            self.journal.prepare(action2, grant2)
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)

    def test_s08_t02_new_id_cannot_bypass_unresolved_same_session(self) -> None:
        """S08-T02: A new operation ID cannot bypass an unresolved operation for the same session."""
        action1 = make_sample_action(op_id="op-sess-1", session="sessions/sess-target")
        grant1 = make_sample_grant(action1)
        self.journal.prepare(action1, grant1)
        ticket = self.journal.begin_dispatch(action1.operation_id, action1.request_hash)

        # Simulate timeout -> state becomes UNKNOWN (unresolved!)
        outcome = TransportOutcome(status=0, uncertain_effect=True)
        rec1 = self.journal.record_outcome(ticket, outcome)
        self.assertEqual(rec1.state, OperationState.UNKNOWN)

        # Attempt to create NEW operation for the same session
        action2 = make_sample_action(op_id="op-sess-2", session="sessions/sess-target", prompt="New attempt")
        grant2 = make_sample_grant(action2)

        with self.assertRaises(OctodotError) as ctx:
            self.journal.prepare(action2, grant2)
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        self.assertIn("Unresolved same-session operation", ctx.exception.message)

    def test_s08_t02_new_id_cannot_bypass_unresolved_logical_task(self) -> None:
        """S08-T02: A new operation ID cannot bypass an unresolved creation logical-task marker."""
        marker = "task-marker-alpha-42"
        action1 = make_sample_action(op_id="op-create-1", session=None, marker=marker)
        grant1 = make_sample_grant(action1)
        self.journal.prepare(action1, grant1)
        ticket = self.journal.begin_dispatch(action1.operation_id, action1.request_hash)

        # State becomes UNKNOWN (unresolved creation!)
        outcome = TransportOutcome(status=500, uncertain_effect=True)
        rec1 = self.journal.record_outcome(ticket, outcome)
        self.assertEqual(rec1.state, OperationState.UNKNOWN)

        # Attempt to create a new creation operation with the SAME logical task marker
        action2 = make_sample_action(op_id="op-create-2", session=None, marker=marker)
        grant2 = make_sample_grant(action2)

        with self.assertRaises(OctodotError) as ctx:
            self.journal.prepare(action2, grant2)
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        self.assertIn("Unresolved creation logical-task marker", ctx.exception.message)


class TestS08T03OutcomeMappingAndRejection(unittest.TestCase):
    """S08-T03: Outcome mapping for timeouts, 5xx, malformed responses, response save failures, and 4xx rejections."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.verifier = FakeGrantVerifier(single_use=False)
        self.journal = Journal(store=self.store, verifier=self.verifier, fence=self.fence)
        orig_prepare = self.journal.prepare
        def auto_prep(action, grant=None, **kwargs):
            if grant is not None and "authorization_ref" not in kwargs:
                ref = action.operation_id
                self.verifier.register_grant(ref, grant)
                kwargs["authorization_ref"] = ref
            return orig_prepare(action, grant, **kwargs)
        self.journal.prepare = auto_prep

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s08_t03_timeout_disconnect_5xx_remain_unknown(self) -> None:
        """S08-T03: Timeout, disconnect, and 5xx map to UNKNOWN and never auto-retry."""
        test_cases = [
            ("op-timeout", TransportOutcome(status=0, uncertain_effect=True)),
            ("op-500", TransportOutcome(status=500, uncertain_effect=True)),
            ("op-503", TransportOutcome(status=503, uncertain_effect=True, retry_after=10.0)),
        ]
        for op_id, outcome in test_cases:
            action = make_sample_action(op_id=op_id, session=f"sessions/{op_id}")
            grant = make_sample_grant(action)
            self.journal.prepare(action, grant)
            ticket = self.journal.begin_dispatch(op_id, action.request_hash)

            rec = self.journal.record_outcome(ticket, outcome)
            self.assertEqual(rec.state, OperationState.UNKNOWN)
            self.assertFalse(rec.api_accepted)

            # Ticket cannot be redeemed again
            self.assertFalse(self.journal.redeem(ticket, action.request_hash))
            # Operation cannot begin_dispatch again
            with self.assertRaises(OctodotError):
                self.journal.begin_dispatch(op_id, action.request_hash)

    def test_s08_t03_malformed_success_remains_unknown(self) -> None:
        """S08-T03: 200 OK with malformed JSON or missing fields maps to UNKNOWN."""
        action = make_sample_action(op_id="op-malformed", session=None)
        grant = make_sample_grant(action)
        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)

        # Malformed success produced by JulesClient when session payload is corrupted
        malformed_outcome = TransportOutcome(
            status=200,
            uncertain_effect=True,
            sanitized_error_code=ErrorCode.MALFORMED_RESPONSE,
        )
        rec = self.journal.record_outcome(ticket, malformed_outcome)
        self.assertEqual(rec.state, OperationState.UNKNOWN)
        self.assertEqual(rec.error_code, ErrorCode.MALFORMED_RESPONSE)
        self.assertFalse(rec.api_accepted)

    def test_s08_t03_response_save_failure_remains_unknown(self) -> None:
        """S08-T03: Response save failure leaves operation in UNKNOWN on restart."""
        action = make_sample_action(op_id="op-save-fail", session="sessions/sess-sf")
        grant = make_sample_grant(action)
        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)

        # Inject failure during state transition
        def fault_hook(pt: str) -> None:
            if pt == "during_acceptance_persistence":
                raise RuntimeError("Disk failure during outcome save")

        self.journal.set_fault_hook(fault_hook)
        with self.assertRaises(RuntimeError):
            self.journal.record_outcome(ticket, TransportOutcome(status=200, body=b"{}"))

        self.journal.set_fault_hook(None)
        # Recover: recovers to UNKNOWN
        self.journal.recover()
        rec = self.journal.get_record(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.UNKNOWN)

    def test_s08_t03_clear_rejection_recorded_without_retry(self) -> None:
        """S08-T03: Clear 4xx rejection is recorded as REJECTED without automatic retry."""
        rejection_cases = [
            ("op-rej-400", TransportOutcome(status=400, uncertain_effect=False)),
            ("op-rej-403", TransportOutcome(status=403, uncertain_effect=False)),
            ("op-rej-404", TransportOutcome(status=404, uncertain_effect=False)),
            ("op-rej-422", TransportOutcome(status=422, uncertain_effect=False)),
        ]
        for op_id, outcome in rejection_cases:
            action = make_sample_action(op_id=op_id, session=f"sessions/{op_id}")
            grant = make_sample_grant(action)
            self.journal.prepare(action, grant)
            ticket = self.journal.begin_dispatch(op_id, action.request_hash)

            rec = self.journal.record_outcome(ticket, outcome)
            self.assertEqual(rec.state, OperationState.REJECTED)
            self.assertFalse(rec.api_accepted)

            # Rejection is terminal: no tickets or retries allowed
            self.assertFalse(self.journal.redeem(ticket, action.request_hash))
            with self.assertRaises(OctodotError):
                self.journal.begin_dispatch(op_id, action.request_hash)


class TestS08T06JournalGating(unittest.TestCase):
    """S08-T06: Stale DB/fence and invalid grants cannot produce a dispatch ticket."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s08_t06_missing_stale_db_and_fence_blocks_ticket_issuance(self) -> None:
        """S08-T06: Stale recovery fence blocks ticket issuance with RECOVERY_FENCE_STALE."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)
        action = make_sample_action(op_id="op-fence-stale")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-ref-stale", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-stale")

        # Host advances epoch outside the store -> DB is stale!
        self.fence.advance_epoch("default")  # epoch is now 2, DB is 1

        with self.assertRaises(OctodotError) as ctx:
            journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

    def test_s08_t06_disabled_verifier_blocks_ticket_issuance(self) -> None:
        """S08-T06: DisabledGrantVerifier records BLOCKED_BEFORE_DISPATCH; ticket cannot be issued."""
        disabled_verifier = DisabledGrantVerifier()
        journal = Journal(store=self.store, verifier=disabled_verifier, fence=self.fence)

        action = make_sample_action(op_id="op-disabled-grant")
        grant = make_sample_grant(action)

        rec = journal.prepare(action, grant, authorization_ref="auth-ref-disabled")
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.VERIFIER_UNAVAILABLE)

        with self.assertRaises(OctodotError) as ctx:
            journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.VERIFIER_UNAVAILABLE)

    def test_s08_t06_advance_fence_and_reconcile_blocks_dispatch(self) -> None:
        """S08-T06 / F1: Operation prepared at E blocks if host advances fence and reconciles to E+1."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)
        action = make_sample_action(op_id="op-fence-advance", profile_epoch=1)
        grant = make_sample_grant(action)
        verifier.register_grant("auth-ref-advance", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-advance")

        # Host advances epoch and store is reconciled to epoch 2
        self.fence.advance_epoch("default")
        self.store.reconcile_profile_epoch("default", epoch=2, identity_validated=True, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

        rec = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.RECOVERY_FENCE_STALE)

    def test_s08_t06_expired_grant_blocks_dispatch(self) -> None:
        """S08-T06 / F1: Grant expired before dispatch re-verification blocks ticket issuance."""
        clock = FakeClock(datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc))
        verifier = FakeGrantVerifier(clock=clock, single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence, clock=clock)

        action = make_sample_action(op_id="op-grant-exp")
        grant = VerifiedGrant(
            action=action.action,
            operation_id=action.operation_id,
            profile=action.binding.profile,
            profile_epoch=action.binding.profile_epoch,
            source=action.binding.source,
            repository=action.binding.repository,
            branch=action.binding.starting_branch or "main",
            payload_hash=action.payload_hash,
            context_hash=action.context_hash,
            plan_hash=action.plan_hash,
            publication_scope=action.publication_scope,
            authorizing_source="coord",
            session=action.binding.session,
            expiry="2026-10-07T12:05:00Z",
        )
        verifier.register_grant("auth-ref-exp", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-exp")

        # Advance clock past expiry
        clock.advance(600)

        with self.assertRaises(OctodotError) as ctx:
            journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_EXPIRED)

        rec = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_EXPIRED)

    def test_s08_t06_revoked_grant_blocks_dispatch(self) -> None:
        """S08-T06 / F1: Revoked grant reference blocks dispatch re-verification."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)

        action = make_sample_action(op_id="op-grant-rev")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-ref-rev", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-rev")

        # Authority revokes grant before dispatch
        verifier.revoke_grant("auth-ref-rev")

        with self.assertRaises(OctodotError) as ctx:
            journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_REVOKED)

        rec = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_REVOKED)

    def test_s08_t06_reopen_store_verifies_grant_and_issues_ticket(self) -> None:
        """S08-T06 / F1: Reopening file-backed store reconstructs evidence and issues ticket if grant valid."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)

        action = make_sample_action(op_id="op-reopen-valid")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-ref-valid", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-valid")

        # Close store and simulate process restart
        self.store.close()

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence)

        ticket = journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.operation_id, action.operation_id)
        self.assertEqual(ticket.request_hash, action.request_hash)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.DISPATCHING)

    def test_s08_t06_reopen_store_with_expired_or_revoked_grant_blocks_dispatch(self) -> None:
        """S08-T06 / F1: Reopening store fails closed if grant expired or revoked during downtime."""
        clock = FakeClock(datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc))
        verifier = FakeGrantVerifier(clock=clock, single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence, clock=clock)

        action = make_sample_action(op_id="op-reopen-exp")
        grant = VerifiedGrant(
            action=action.action,
            operation_id=action.operation_id,
            profile=action.binding.profile,
            profile_epoch=action.binding.profile_epoch,
            source=action.binding.source,
            repository=action.binding.repository,
            branch=action.binding.starting_branch or "main",
            payload_hash=action.payload_hash,
            context_hash=action.context_hash,
            plan_hash=action.plan_hash,
            publication_scope=action.publication_scope,
            authorizing_source="coord",
            session=action.binding.session,
            expiry="2026-10-07T12:05:00Z",
        )
        verifier.register_grant("auth-ref-reopen-exp", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-reopen-exp")

        # Close store
        self.store.close()

        # Advance clock while offline
        clock.advance(600)

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence, clock=clock)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_EXPIRED)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_EXPIRED)

    def test_s08_t06_reopen_missing_or_corrupted_evidence_fails_closed(self) -> None:
        """S08-T06 / F1: Reopened store with corrupted/missing evidence fails closed with GRANT_MISSING."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)

        action = make_sample_action(op_id="op-corrupt-evidence")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-ref-corrupt", grant)
        journal.prepare(action, grant, authorization_ref="auth-ref-corrupt")

        db_path = str(self.store.db_path)
        self.store.close()

        # Directly delete authorization_ref from evidence
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM operation_evidence WHERE key = 'authorization_ref' AND operation_id = 'op-corrupt-evidence'")
        conn.commit()
        conn.close()

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_MISSING)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_MISSING)

        # Also test malformed JSON for prepared_action
        action2 = make_sample_action(op_id="op-bad-json")
        grant2 = make_sample_grant(action2)
        verifier.register_grant("auth-ref-bad-json", grant2)
        journal2.prepare(action2, grant2, authorization_ref="auth-ref-bad-json")
        store2.close()

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE operation_evidence SET value_json = '\"not-valid-json-escaped\"' WHERE key = 'prepared_action' AND operation_id = 'op-bad-json'")
        conn.commit()
        conn.close()

        store3 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store3.close)
        journal3 = Journal(store=store3, verifier=verifier, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal3.begin_dispatch(action2.operation_id, action2.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_MISSING)

        rec2 = store3.get_operation(action2.operation_id)
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec2.error_code, ErrorCode.GRANT_MISSING)

    def test_s08_t06_distinct_authorization_ref_and_authorizing_source(self) -> None:
        """S08-T06 / F2: Disentangles authorization_ref (lookup reference) from authorizing_source."""
        auth_ref = "approval-token-xyz-123"
        auth_source = "coordinator-agent-primary"

        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)

        action = make_sample_action(op_id="op-distinct-auth")
        grant = VerifiedGrant(
            action=action.action,
            operation_id=action.operation_id,
            profile=action.binding.profile,
            profile_epoch=action.binding.profile_epoch,
            source=action.binding.source,
            repository=action.binding.repository,
            branch=action.binding.starting_branch or "main",
            payload_hash=action.payload_hash,
            context_hash=action.context_hash,
            plan_hash=action.plan_hash,
            publication_scope=action.publication_scope,
            authorizing_source=auth_source,
            session=action.binding.session,
        )
        # Register grant strictly by its lookup token (authorization_ref)
        verifier.register_grant(auth_ref, grant)

        # Prepare and dispatch using auth_ref
        journal.prepare(action, grant, authorization_ref=auth_ref)
        ticket = journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.operation_id, action.operation_id)
        self.assertEqual(ticket.request_hash, action.request_hash)

    def test_s08_t06_verifier_none_blocks_prepare_and_dispatch(self) -> None:
        """S08-T06 / F1: verifier=None fails closed in prepare and begin_dispatch with VERIFIER_UNAVAILABLE, 0 tickets."""
        journal = Journal(store=self.store, verifier=None, fence=self.fence)
        action = make_sample_action(op_id="op-verifier-none")
        grant = make_sample_grant(action)

        rec = journal.prepare(action, grant, authorization_ref="auth-ref-none")
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.VERIFIER_UNAVAILABLE)

        with self.assertRaises(OctodotError) as ctx:
            journal.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.VERIFIER_UNAVAILABLE)

        # 0 tickets issued
        rec_store = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(rec_store)
        self.assertIsNone(rec_store.ticket_id)

    def test_s08_t06_verifier_none_blocks_begin_dispatch_after_reopen(self) -> None:
        """S08-T06 / F1: verifier=None fails closed in begin_dispatch after disk reopen of prepared operation."""
        v1 = FakeGrantVerifier(single_use=False)
        journal1 = Journal(store=self.store, verifier=v1, fence=self.fence)
        action = make_sample_action(op_id="op-reopen-none")
        grant = make_sample_grant(action)
        v1.register_grant("auth-reopen-none", grant)

        rec = journal1.prepare(action, grant, authorization_ref="auth-reopen-none")
        self.assertEqual(rec.state, OperationState.PREPARED)

        # Reopen store with verifier=None
        self.store.close()
        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=None, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.VERIFIER_UNAVAILABLE)

        rec2 = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec2.error_code, ErrorCode.VERIFIER_UNAVAILABLE)
        self.assertIsNone(rec2.ticket_id)

    def test_s08_t06_missing_or_empty_authorization_ref_blocks_prepare_with_grant_missing(self) -> None:
        """S08-T06 / F2: Missing or empty authorization_ref with active verifier blocks with GRANT_MISSING, 0 POSTs."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)

        # Case 1: authorization_ref is None
        action1 = make_sample_action(op_id="op-no-auth-ref")
        grant1 = make_sample_grant(action1)
        rec1 = journal.prepare(action1, grant1, authorization_ref=None)
        self.assertEqual(rec1.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec1.error_code, ErrorCode.GRANT_MISSING)

        with self.assertRaises(OctodotError) as ctx1:
            journal.begin_dispatch(action1.operation_id, action1.request_hash)
        self.assertEqual(ctx1.exception.code, ErrorCode.GRANT_MISSING)

        # Case 2: authorization_ref is whitespace / empty string
        action2 = make_sample_action(op_id="op-empty-auth-ref")
        grant2 = make_sample_grant(action2)
        rec2 = journal.prepare(action2, grant2, authorization_ref="   ")
        self.assertEqual(rec2.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec2.error_code, ErrorCode.GRANT_MISSING)

        with self.assertRaises(OctodotError) as ctx2:
            journal.begin_dispatch(action2.operation_id, action2.request_hash)
        self.assertEqual(ctx2.exception.code, ErrorCode.GRANT_MISSING)

    def test_s08_t06_dispatch_boundary_tampered_request_hash_after_reopen(self) -> None:
        """S08-T06 / F1: Tampered request_hash in durable evidence blocks at dispatch boundary after reopen."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)
        action = make_sample_action(op_id="op-tampered-reqhash")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-tampered-reqhash", grant)
        journal.prepare(action, grant, authorization_ref="auth-tampered-reqhash")

        # Tamper stored prepared_action request_hash in SQLite
        db_path = str(self.store.db_path)
        self.store.close()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT value_json FROM operation_evidence WHERE operation_id = 'op-tampered-reqhash' AND key = 'prepared_action'")
        act_data = json.loads(cur.fetchone()[0])
        act_data["request_hash"] = "tampered_bad_request_hash"
        cur.execute("UPDATE operation_evidence SET value_json = ? WHERE operation_id = 'op-tampered-reqhash' AND key = 'prepared_action'", (json.dumps(act_data),))
        conn.commit()
        conn.close()

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_INVALID)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_INVALID)
        self.assertIsNone(rec.ticket_id)

    def test_s08_t06_dispatch_boundary_binding_mismatch_after_reopen(self) -> None:
        """S08-T06 / F1: Binding mismatch in durable evidence blocks at dispatch boundary after reopen."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)
        action = make_sample_action(op_id="op-tampered-binding")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-tampered-binding", grant)
        journal.prepare(action, grant, authorization_ref="auth-tampered-binding")

        db_path = str(self.store.db_path)
        self.store.close()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT value_json FROM operation_evidence WHERE operation_id = 'op-tampered-binding' AND key = 'prepared_action'")
        act_data = json.loads(cur.fetchone()[0])
        act_data["binding"]["repository"] = "FORGED/REPO"
        cur.execute("UPDATE operation_evidence SET value_json = ? WHERE operation_id = 'op-tampered-binding' AND key = 'prepared_action'", (json.dumps(act_data),))
        conn.commit()
        conn.close()

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.BINDING_MISMATCH)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.BINDING_MISMATCH)
        self.assertIsNone(rec.ticket_id)

    def test_s08_t06_dispatch_boundary_payload_mismatch_after_reopen(self) -> None:
        """S08-T06 / F1: Tampered payload content (recomputed payload hash differs) blocks after reopen."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)
        action = make_sample_action(op_id="op-tampered-payload")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-tampered-payload", grant)
        journal.prepare(action, grant, authorization_ref="auth-tampered-payload")

        db_path = str(self.store.db_path)
        self.store.close()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT value_json FROM operation_evidence WHERE operation_id = 'op-tampered-payload' AND key = 'prepared_action'")
        act_data = json.loads(cur.fetchone()[0])
        act_data["payload"] = {"prompt": "altered unauthorized prompt text"}
        cur.execute("UPDATE operation_evidence SET value_json = ? WHERE operation_id = 'op-tampered-payload' AND key = 'prepared_action'", (json.dumps(act_data),))
        conn.commit()
        conn.close()

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_INVALID)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_INVALID)
        self.assertIsNone(rec.ticket_id)

    def test_s08_t06_dispatch_boundary_missing_required_field_after_reopen(self) -> None:
        """S08-T06 / F1: Missing required field in evidence blocks with GRANT_MISSING after reopen."""
        verifier = FakeGrantVerifier(single_use=False)
        journal = Journal(store=self.store, verifier=verifier, fence=self.fence)
        action = make_sample_action(op_id="op-missing-required-field")
        grant = make_sample_grant(action)
        verifier.register_grant("auth-missing-field", grant)
        journal.prepare(action, grant, authorization_ref="auth-missing-field")

        db_path = str(self.store.db_path)
        self.store.close()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT value_json FROM operation_evidence WHERE operation_id = 'op-missing-required-field' AND key = 'prepared_action'")
        act_data = json.loads(cur.fetchone()[0])
        del act_data["publication_scope"]
        cur.execute("UPDATE operation_evidence SET value_json = ? WHERE operation_id = 'op-missing-required-field' AND key = 'prepared_action'", (json.dumps(act_data),))
        conn.commit()
        conn.close()

        store2 = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.addCleanup(store2.close)
        journal2 = Journal(store=store2, verifier=verifier, fence=self.fence)

        with self.assertRaises(OctodotError) as ctx:
            journal2.begin_dispatch(action.operation_id, action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_MISSING)

        rec = store2.get_operation(action.operation_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.BLOCKED_BEFORE_DISPATCH)
        self.assertEqual(rec.error_code, ErrorCode.GRANT_MISSING)
        self.assertIsNone(rec.ticket_id)

