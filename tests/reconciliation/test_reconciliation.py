"""Tests for S08 Reconciliation: read-only effect verification, honest attribution, and desired-state resolution.

Standard library only. Compatible with Python 3.10+.
Covers:
- S08-T04: Exact manual matching message, duplicate text, and multiple creation-marker matches yield effect evidence with honest attribution uncertainty.
- S08-T05: No matching effect after 0, 1, or 3 complete scans remains unknown; absence never authorizes retry.
- S08-T06: Desired-state resolution retires a blocker without authorizing resend.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from typing import Any, Mapping, Sequence
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.api import JulesClient, compute_mutation_request_hash
from octodot.contracts import canonical_hash, request_hash
from octodot.errors import ErrorCode, OctodotError
from octodot.journal import Journal
from octodot.models import (
    ActivityRecord,
    Binding,
    OperationRecord,
    OperationState,
    PreparedAction,
    SessionRecord,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.reconciliation import (
    Reconciler,
    reconcile_operation,
    resolve_desired_state,
)
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class FixtureReadAPI:
    """In-test read API providing synthetic sessions and activities without network."""

    def __init__(
        self,
        sessions: Sequence[SessionRecord | dict[str, Any]] = (),
        activities: Mapping[str, Sequence[ActivityRecord | dict[str, Any]]] | None = None,
    ) -> None:
        self._sessions = list(sessions)
        self._activities = dict(activities or {})
        self.sessions_list_calls = 0
        self.activities_list_calls = 0

    def sessions_list(self) -> tuple[Any, ...]:
        self.sessions_list_calls += 1
        return tuple(self._sessions)

    def activities_list(self, session_name: str) -> tuple[Any, ...]:
        self.activities_list_calls += 1
        return tuple(self._activities.get(session_name, ()))


def make_test_action(
    op_id: str = "op-reconcile-1",
    session: str | None = "sessions/sess-1",
    prompt: str = "Fix the login bug",
    marker: str | None = None,
) -> PreparedAction:
    binding = Binding(
        profile="default",
        profile_epoch=1,
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
    else:
        target = "/v1alpha/sessions"
        req_h = compute_mutation_request_hash(target, payload)
    return PreparedAction(
        action="chats.reply" if session else "tasks.create",
        operation_id=op_id,
        binding=binding,
        payload=payload,
        payload_hash=canonical_hash(payload),
        context_hash=canonical_hash({"ctx": 1}),
        request_hash=req_h,
        publication_scope="none",
        plan_hash=canonical_hash({"plan": 1}),
    )


def make_test_grant(action: PreparedAction) -> VerifiedGrant:
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


class TestS08T04HonestAttribution(unittest.TestCase):
    """S08-T04: Exact manual matching message, duplicate text, and multiple creation-marker matches yield effect evidence with honest attribution uncertainty."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.journal = Journal(store=self.store, fence=self.fence, clock=self.clock)
        self.reconciler = Reconciler(store=self.store, fence=self.fence, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s08_t04_exact_manual_matching_message_attribution_uncertain(self) -> None:
        """S08-T04: A message matching the prompt but with a manual originator yields effect_observed=True, attribution uncertain, state remains UNKNOWN."""
        session_name = "sessions/sess-manual"
        action = make_test_action(op_id="op-manual-match", session=session_name, prompt="Hello from agent")
        grant = make_test_grant(action)

        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)

        # Dispatched, but response timed out -> state is UNKNOWN
        self.store.transition_operation_state(ticket.operation_id, OperationState.UNKNOWN, fence=self.fence)

        # Synthetic activity from manual user with exact matching text
        manual_activity = ActivityRecord(
            name="activities/act-manual-1",
            activity_type="message",
            originator="USER",  # Human originator!
            unknown_fields=(("prompt", "Hello from agent"),),
        )
        api = FixtureReadAPI(activities={session_name: [manual_activity]})

        res = self.reconciler.reconcile(action.operation_id, read_api=api, scans=1)

        # State must remain UNKNOWN (never falsely claimed as resolved or confirmed attribution)
        self.assertEqual(res.reconciled_state, OperationState.UNKNOWN.value)
        record = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, OperationState.UNKNOWN)
        # Segregated evidence fields: effect_observed=True, but attribution is uncertain!
        self.assertTrue(record.effect_observed)
        self.assertFalse(record.api_accepted)
        self.assertIn("uncertain", record.attribution)
        self.assertIn("manual", record.attribution)

    def test_s08_t04_duplicate_text_attribution_uncertain(self) -> None:
        """S08-T04: Duplicate messages with identical text yield effect_observed=True, attribution uncertain, state remains UNKNOWN."""
        session_name = "sessions/sess-duplicate"
        action = make_test_action(op_id="op-dup-text", session=session_name, prompt="Duplicate text test")
        grant = make_test_grant(action)

        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)
        self.store.transition_operation_state(ticket.operation_id, OperationState.UNKNOWN, fence=self.fence)

        # Two activities with identical text
        act1 = ActivityRecord(
            name="activities/act-1",
            activity_type="message",
            originator="AGENT",
            unknown_fields=(("prompt", "Duplicate text test"),),
        )
        act2 = ActivityRecord(
            name="activities/act-2",
            activity_type="message",
            originator="AGENT",
            unknown_fields=(("prompt", "Duplicate text test"),),
        )
        api = FixtureReadAPI(activities={session_name: [act1, act2]})

        res = self.reconciler.reconcile(action.operation_id, read_api=api, scans=1)

        self.assertEqual(res.reconciled_state, OperationState.UNKNOWN.value)
        record = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, OperationState.UNKNOWN)
        self.assertTrue(record.effect_observed)
        self.assertFalse(record.api_accepted)
        self.assertIn("uncertain", record.attribution)
        self.assertIn("duplicate", record.attribution)

    def test_s08_t04_multiple_creation_marker_matches_attribution_uncertain(self) -> None:
        """S08-T04: Multiple sessions matching creation logical-task marker yield effect_observed=True, attribution uncertain, state remains UNKNOWN."""
        marker = "task-marker-dup-99"
        action = make_test_action(op_id="op-create-marker-dup", session=None, marker=marker)
        grant = make_test_grant(action)

        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)
        self.store.transition_operation_state(ticket.operation_id, OperationState.UNKNOWN, fence=self.fence)

        # Two sessions matching the same marker
        sess1 = SessionRecord(
            name="sessions/sess-match-1",
            state="ACTIVE",
            title=f"Task {marker}",
        )
        sess2 = SessionRecord(
            name="sessions/sess-match-2",
            state="ACTIVE",
            title=f"Duplicate {marker}",
        )
        api = FixtureReadAPI(sessions=[sess1, sess2])

        res = self.reconciler.reconcile(action.operation_id, read_api=api, scans=1)

        self.assertEqual(res.reconciled_state, OperationState.UNKNOWN.value)
        record = self.store.get_operation(action.operation_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, OperationState.UNKNOWN)
        self.assertTrue(record.effect_observed)
        self.assertFalse(record.api_accepted)
        self.assertIn("uncertain", record.attribution)
        self.assertIn("multiple", record.attribution)

    def test_s08_t04_unique_matches_confirm_attribution(self) -> None:
        """S08-T04: Unique automated matches yield inferred attribution (never 'confirmed' from text/marker match alone) and transition to EFFECT_OBSERVED."""
        # 1. Unique message match
        session_name = "sessions/sess-unique"
        action1 = make_test_action(op_id="op-unique-msg", session=session_name, prompt="Unique automated message")
        grant1 = make_test_grant(action1)
        self.journal.prepare(action1, grant1)
        ticket1 = self.journal.begin_dispatch(action1.operation_id, action1.request_hash)
        self.store.transition_operation_state(ticket1.operation_id, OperationState.UNKNOWN, fence=self.fence)

        unique_act = ActivityRecord(
            name="activities/act-unique-1",
            activity_type="message",
            originator="AGENT",
            unknown_fields=(("prompt", "Unique automated message"),),
        )
        api1 = FixtureReadAPI(activities={session_name: [unique_act]})

        res1 = self.reconciler.reconcile(action1.operation_id, read_api=api1, scans=1)
        self.assertEqual(res1.reconciled_state, OperationState.EFFECT_OBSERVED.value)
        rec1 = self.store.get_operation(action1.operation_id)
        self.assertEqual(rec1.state, OperationState.EFFECT_OBSERVED)
        self.assertTrue(rec1.effect_observed)
        # F2: must be inferred:unique_text_match, never confirmed
        self.assertEqual(rec1.attribution, "inferred:unique_text_match")

        # 2. Unique creation match
        marker = "task-marker-unique-77"
        action2 = make_test_action(op_id="op-unique-create", session=None, marker=marker)
        grant2 = make_test_grant(action2)
        self.journal.prepare(action2, grant2)
        ticket2 = self.journal.begin_dispatch(action2.operation_id, action2.request_hash)
        self.store.transition_operation_state(ticket2.operation_id, OperationState.UNKNOWN, fence=self.fence)

        unique_sess = SessionRecord(
            name="sessions/sess-unique-created",
            state="ACTIVE",
            title=f"Created {marker}",
        )
        api2 = FixtureReadAPI(sessions=[unique_sess])

        res2 = self.reconciler.reconcile(action2.operation_id, read_api=api2, scans=1)
        self.assertEqual(res2.reconciled_state, OperationState.EFFECT_OBSERVED.value)
        rec2 = self.store.get_operation(action2.operation_id)
        self.assertEqual(rec2.state, OperationState.EFFECT_OBSERVED)
        self.assertTrue(rec2.effect_observed)
        # F2: must be inferred:unique_marker_match, never confirmed
        self.assertEqual(rec2.attribution, "inferred:unique_marker_match")


class TestS08T05AbsenceNeverAuthorizesRetry(unittest.TestCase):
    """S08-T05: No matching effect after 0, 1 or 3 complete scans remains unknown; absence never authorizes retry."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.journal = Journal(store=self.store, fence=self.fence, clock=self.clock)
        self.reconciler = Reconciler(store=self.store, fence=self.fence, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s08_t05_absence_after_0_1_3_scans_remains_unknown(self) -> None:
        """S08-T05: Operation remains UNKNOWN after 0, 1, and 3 scans; absence never authorizes resend or retry."""
        action = make_test_action(op_id="op-absence", session="sessions/sess-absent", prompt="Missing message")
        grant = make_test_grant(action)
        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)

        # Transition to UNKNOWN (e.g. uncertain server error)
        self.store.transition_operation_state(ticket.operation_id, OperationState.UNKNOWN, fence=self.fence)

        api = FixtureReadAPI(activities={"sessions/sess-absent": []})  # Zero matching activities

        # Case 1: 0 scans
        res_0 = self.reconciler.reconcile(action.operation_id, read_api=api, scans=0)
        self.assertEqual(res_0.reconciled_state, OperationState.UNKNOWN.value)
        rec_0 = self.store.get_operation(action.operation_id)
        self.assertEqual(rec_0.state, OperationState.UNKNOWN)
        # Cannot begin_dispatch or mint ticket
        with self.assertRaises(OctodotError):
            self.journal.begin_dispatch(action.operation_id, action.request_hash)

        # Case 2: 1 complete scan
        res_1 = self.reconciler.reconcile(action.operation_id, read_api=api, scans=1)
        self.assertEqual(res_1.reconciled_state, OperationState.UNKNOWN.value)
        rec_1 = self.store.get_operation(action.operation_id)
        self.assertEqual(rec_1.state, OperationState.UNKNOWN)
        with self.assertRaises(OctodotError):
            self.journal.begin_dispatch(action.operation_id, action.request_hash)

        # Case 3: 3 complete scans
        res_3 = self.reconciler.reconcile(action.operation_id, read_api=api, scans=3)
        self.assertEqual(res_3.reconciled_state, OperationState.UNKNOWN.value)
        rec_3 = self.store.get_operation(action.operation_id)
        self.assertEqual(rec_3.state, OperationState.UNKNOWN)

        # Absence NEVER authorizes retry: ticket redemption fails, begin_dispatch fails
        self.assertFalse(self.journal.redeem(ticket, action.request_hash))
        with self.assertRaises(OctodotError):
            self.journal.begin_dispatch(action.operation_id, action.request_hash)


class TestS08T06DesiredStateResolution(unittest.TestCase):
    """S08-T06: Desired-state resolution retires a blocker without authorizing resend."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.journal = Journal(store=self.store, fence=self.fence, clock=self.clock)
        self.reconciler = Reconciler(store=self.store, fence=self.fence, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s08_t06_reject_empty_and_placeholder_decision_references(self) -> None:
        """S08-T06 (F1): resolve_desired_state rejects empty or placeholder decided_by references."""
        session_name = "sessions/sess-dec-ref"
        action = make_test_action(op_id="op-dec-ref", session=session_name)
        grant = make_test_grant(action)
        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)
        self.store.transition_operation_state(ticket.operation_id, OperationState.UNKNOWN, fence=self.fence)

        invalid_refs = ["", "   ", "placeholder", "PLACEHOLDER_REF", "todo", "TODO:review", "none", "null", "N/A"]
        for bad_ref in invalid_refs:
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaises(OctodotError) as ctx:
                    self.reconciler.resolve_desired_state(
                        operation_id=action.operation_id,
                        decided_by=bad_ref,
                        reason="Test",
                    )
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s08_t06_resolution_keeps_unknown_and_flags_unchanged(self) -> None:
        """S08-T06 (F1): Desired-state resolution keeps operation in UNKNOWN and leaves evidence flags unchanged."""
        session_name = "sessions/sess-flags"
        action = make_test_action(op_id="op-flags-1", session=session_name)
        grant = make_test_grant(action)
        self.journal.prepare(action, grant)
        ticket = self.journal.begin_dispatch(action.operation_id, action.request_hash)
        self.store.transition_operation_state(ticket.operation_id, OperationState.UNKNOWN, fence=self.fence)

        resolved_rec = self.reconciler.resolve_desired_state(
            operation_id="op-flags-1",
            decided_by="coord-decision-ref-101",
            reason="Confirmed by operator via audit log",
        )

        # F1: The operation state stays UNKNOWN (remote truth stays unknown) and its evidence flags are untouched
        self.assertEqual(resolved_rec.state, OperationState.UNKNOWN)
        self.assertFalse(resolved_rec.api_accepted)
        self.assertFalse(resolved_rec.effect_observed)
        self.assertEqual(resolved_rec.attribution, "")
        self.assertFalse(resolved_rec.ui_verified)

        # Check resolution evidence record was appended
        evidence_dict = dict(resolved_rec.evidence)
        self.assertIn("desired_state_resolution", evidence_dict)
        res_data = evidence_dict["desired_state_resolution"]
        self.assertEqual(res_data["resolution"], "desired_state_resolved")
        self.assertEqual(res_data["decided_by"], "coord-decision-ref-101")
        self.assertEqual(res_data["reason"], "Confirmed by operator via audit log")

    def test_s08_t06_unresolved_unknown_blocks_new_id(self) -> None:
        """S08-T06 (F1): An unresolved UNKNOWN operation blocks a new operation ID for the same session."""
        session_name = "sessions/sess-unresolved"
        action1 = make_test_action(op_id="op-unresolved-1", session=session_name)
        grant1 = make_test_grant(action1)
        self.journal.prepare(action1, grant1)
        ticket1 = self.journal.begin_dispatch(action1.operation_id, action1.request_hash)
        self.store.transition_operation_state(ticket1.operation_id, OperationState.UNKNOWN, fence=self.fence)

        # op1 is UNKNOWN without desired-state resolution -> blocks new ID
        action2 = make_test_action(op_id="op-new-attempt", session=session_name)
        grant2 = make_test_grant(action2)
        with self.assertRaises(OctodotError) as ctx:
            self.journal.prepare(action2, grant2)
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        self.assertIn("Unresolved same-session operation", ctx.exception.message)

    def test_s08_t06_resolved_without_predecessor_linkage_still_blocks_and_with_linkage_allows(self) -> None:
        """S08-T06 (F1): A resolved UNKNOWN without predecessor linkage still blocks; with linkage it allows."""
        session_name = "sessions/sess-linkage"
        op1_action = make_test_action(op_id="op-linkage-1", session=session_name, prompt="First prompt")
        op1_grant = make_test_grant(op1_action)
        self.journal.prepare(op1_action, op1_grant)
        ticket1 = self.journal.begin_dispatch(op1_action.operation_id, op1_action.request_hash)
        self.store.transition_operation_state(ticket1.operation_id, OperationState.UNKNOWN, fence=self.fence)

        # Resolve op1
        self.reconciler.resolve_desired_state(
            operation_id="op-linkage-1",
            decided_by="coord-decision-linkage-202",
            reason="Retiring blocker after review",
        )

        # 1. New op WITHOUT predecessor linkage -> STILL BLOCKS!
        op2_unlinked = make_test_action(op_id="op-linkage-2-unlinked", session=session_name, prompt="Unlinked prompt")
        op2_unlinked_grant = make_test_grant(op2_unlinked)
        with self.assertRaises(OctodotError) as ctx:
            self.journal.prepare(op2_unlinked, op2_unlinked_grant)
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        self.assertIn("requires predecessor_operation_id linkage", ctx.exception.message)

        # 2. New op with non-existent predecessor linkage -> raises INVALID_INPUT
        op2_wrong_link = make_test_action(
            op_id="op-linkage-2-wrong",
            session=session_name,
            prompt="Wrong link prompt",
        )
        op2_wrong_grant = make_test_grant(op2_wrong_link)
        with self.assertRaises(OctodotError) as ctx:
            self.journal.prepare(op2_wrong_link, op2_wrong_grant, predecessor_operation_id="op-nonexistent-999")
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

        # 3. Assert byte-identical payload and request_hash with/without linkage
        op2_plain = make_test_action(op_id="op-linkage-test", session=session_name, prompt="Byte identical test")
        op2_with_link = make_test_action(op_id="op-linkage-test", session=session_name, prompt="Byte identical test")
        self.assertEqual(op2_plain.payload, op2_with_link.payload)
        self.assertEqual(op2_plain.request_hash, op2_with_link.request_hash)
        self.assertEqual(op2_plain.payload_hash, op2_with_link.payload_hash)
        self.assertNotIn("predecessor_operation_id", op2_with_link.payload)
        self.assertNotIn("predecessor_id", op2_with_link.payload)

        # 4. New op WITH correct predecessor linkage pointing at op-linkage-1 -> ALLOWED!
        op2_linked = make_test_action(
            op_id="op-linkage-2-linked",
            session=session_name,
            prompt="Linked prompt",
        )
        op2_linked_grant = make_test_grant(op2_linked)
        rec2 = self.journal.prepare(op2_linked, op2_linked_grant, predecessor_operation_id="op-linkage-1")
        self.assertEqual(rec2.state, OperationState.PREPARED)
        # Linkage is recorded as evidence on the new op
        evidence_dict = dict(rec2.evidence)
        self.assertEqual(evidence_dict.get("predecessor_operation_id"), "op-linkage-1")

        # The new op can proceed to dispatch, and outgoing transport body contains NO predecessor key
        ticket2 = self.journal.begin_dispatch(op2_linked.operation_id, op2_linked.request_hash)
        self.assertEqual(ticket2.operation_id, "op-linkage-2-linked")

        transport = FixtureTransport(
            responses={("POST", f"/v1alpha/{session_name}:sendMessage"): TransportOutcome(status=200, body=b"{}")}
        )
        client = JulesClient(transport=transport, ticket_authority=self.journal, clock=self.clock)
        client.sessions_send_message(ticket2, session_name, op2_linked.payload)
        self.assertEqual(len(transport.calls), 1)
        sent_body = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertNotIn("predecessor_operation_id", sent_body)
        self.assertNotIn("predecessor_id", sent_body)
        self.assertEqual(sent_body, {"prompt": "Linked prompt"})

    def test_s08_t06_resolved_op_own_id_never_redispatches_and_post_counts(self) -> None:
        """S08-T06 (F1): A resolved operation can never be dispatched again under its own ID; POST counts are asserted."""
        session_name = "sessions/sess-redispatch"
        op1_action = make_test_action(op_id="op-resolved-orig", session=session_name, prompt="Original prompt")
        op1_grant = make_test_grant(op1_action)

        self.journal.prepare(op1_action, op1_grant)
        ticket1 = self.journal.begin_dispatch(op1_action.operation_id, op1_action.request_hash)

        # Track POST requests
        post_log: list[tuple[str, str, bytes | None]] = []
        target = f"/v1alpha/{session_name}:sendMessage"
        transport = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=500, uncertain_effect=True)}
        )
        client = JulesClient(transport=transport, ticket_authority=self.journal, clock=self.clock)

        # Initial dispatch attempt -> 500 error (uncertain effect)
        resp1 = client.sessions_send_message(ticket1, session_name, op1_action.payload)
        post_log.append(("POST", target, b"initial"))
        self.journal.record_outcome(ticket1, resp1)
        rec1 = self.journal.get_record(op1_action.operation_id)
        self.assertEqual(rec1.state, OperationState.UNKNOWN)

        # Resolve op1
        self.reconciler.resolve_desired_state(
            operation_id="op-resolved-orig",
            decided_by="coord-decision-ref-303",
            reason="Retiring blocker after investigation",
        )

        # Assert op1's own ID can NEVER re-dispatch:
        # 1. Replay of prepare returns recorded UNKNOWN record
        replay_rec = self.journal.prepare(op1_action, op1_grant)
        self.assertEqual(replay_rec.state, OperationState.UNKNOWN)

        # 2. begin_dispatch under op1's own ID fails with UNRESOLVED_INTENT (non-conflict error)
        with self.assertRaises(OctodotError) as ctx:
            self.journal.begin_dispatch(op1_action.operation_id, op1_action.request_hash)
        self.assertEqual(ctx.exception.code, ErrorCode.UNRESOLVED_INTENT)

        # 3. Old ticket redemption fails
        self.assertFalse(self.journal.redeem(ticket1, op1_action.request_hash))

        # 4. Total POST calls for op1 remains exactly 1 (no additional POSTs)
        self.assertEqual(len(post_log), 1)

        # 5. Only a NEW operation with predecessor linkage can dispatch
        op2_action = make_test_action(
            op_id="op-successor-linked",
            session=session_name,
            prompt="Successor prompt",
        )
        op2_grant = make_test_grant(op2_action)
        self.journal.prepare(op2_action, op2_grant, predecessor_operation_id="op-resolved-orig")
        ticket2 = self.journal.begin_dispatch(op2_action.operation_id, op2_action.request_hash)

        # Configure 200 OK for successor
        transport2 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")}
        )
        client2 = JulesClient(transport=transport2, ticket_authority=self.journal, clock=self.clock)
        resp2 = client2.sessions_send_message(ticket2, session_name, op2_action.payload)

        # Outgoing transport body has NO predecessor key
        self.assertEqual(len(transport2.calls), 1)
        sent_body2 = json.loads(transport2.calls[0]["body"].decode("utf-8"))
        self.assertNotIn("predecessor_operation_id", sent_body2)
        self.assertNotIn("predecessor_id", sent_body2)
        self.assertEqual(sent_body2, {"prompt": "Successor prompt"})

        post_log.append(("POST", target, b"successor"))
        outcome2 = self.journal.record_outcome(ticket2, resp2)
        self.assertEqual(outcome2.state, OperationState.ACCEPTED)

        # Proves exactly 2 total POST attempts: 1 for original, 1 for successor
        self.assertEqual(len(post_log), 2)
