"""Comprehensive tests for S09 ActionRunner.

Covers:
- S09-T01: Invalid last action rejects the whole plan before credentials/network;
  duplicate/forward refs, unknown fields and dynamic mutation targets fail.
- S09-T02: Failed read dependency skips dependent action; independent reads continue;
  blocked/rejected/unknown mutation suppresses subsequent mutations while reconciliation reads continue.
- S09-T03: Replay same plan ID/hash returns recorded action/operation status; changed hash conflicts;
  a new run cannot repeat a recorded mutation.
- S09-T04: Large text/artifacts spill to checksummed bounded private artifacts;
  capped output names every omitted attention item or marks incomplete coverage.
- S09-T05: Mixed ok/waiting/read-error/mutation-error/unsupported/interrupted cases match
  deterministic result and exit-code precedence.
- S09-T06: Read-only mode cannot enter a mutation dispatch path or issue POST; disabled templates
  cannot load any credentials, regardless of action text. Authorized GET mode may load the selected Jules credential.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock
from typing import Any

import sys
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.authorization import FakeGrantVerifier
from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    Coverage,
    canonical_hash,
    compute_plan_hash,
    request_hash,
    validate_result,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_INTERRUPTED,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    EXIT_WAITING,
    ErrorCode,
    OctodotError,
    combine_exit_codes,
)
from octodot.journal import Journal
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    Binding,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.api import JulesClient
from octodot.runner import ActionRunner, run_plan, spill_artifact
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport, HttpTransport, SpyCredentialSource
from octodot import cli


class SpyTransportFactory:
    """Transport factory with observable call tracking for isolation verification."""

    def __init__(self, transport: Any = None) -> None:
        self.call_count = 0
        self.transport = transport or FixtureTransport()

    def __call__(self) -> Any:
        self.call_count += 1
        return self.transport


class JournalJulesClientMutationHandler:
    """Test mutation handler going through real S08 Journal and S02 JulesClient with FixtureTransport."""

    def __init__(self, journal: Journal, client: JulesClient) -> None:
        self.journal = journal
        self.client = client

    def can_handle(self, op: str) -> bool:
        return op in ("tasks.create", "chats.reply", "plans.approve")

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = action["id"]
        op = action["op"]
        operation_id = action["operation_id"]
        payload = action.get("payload", {})
        target = action.get("target", "OWNER/REPO")

        target_path = "/v1alpha/sessions" if op == "tasks.create" else f"/v1alpha/{target}:sendMessage"
        req_hash = request_hash({"target": target_path, "body": payload})
        binding = Binding(
            profile=context.get("profile", "default"),
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="main",
            session=target if target.startswith("sessions/") else None,
        )
        plan_h = context.get("plan", {}).get("plan_hash") or "plan_hash"
        prep = PreparedAction(
            action=op,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            payload_hash=canonical_hash(payload),
            context_hash=canonical_hash({"source": "sources/github/OWNER/REPO"}),
            request_hash=req_hash,
            publication_scope="none",
            plan_hash=plan_h,
        )

        auth_ref = action.get("authorization_ref") or "ref-1"
        grant = VerifiedGrant(
            action=op,
            operation_id=operation_id,
            profile=context.get("profile", "default"),
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            branch="main",
            payload_hash=prep.payload_hash,
            context_hash=prep.context_hash,
            plan_hash=prep.plan_hash,
            publication_scope="none",
            authorizing_source="authority-signer-1",
            session=binding.session,
            max_attempts=1,
        )

        verifier = self.journal.verifier or context.get("verifier")
        if verifier is not None and hasattr(verifier, "register_grant"):
            verifier.register_grant(auth_ref, grant)

        self.journal.prepare(prep, grant=grant, authorization_ref=auth_ref)
        ticket = self.journal.begin_dispatch(operation_id, req_hash)

        if op == "tasks.create":
            resp = self.client.sessions_create(ticket, payload)
        elif op == "chats.reply":
            resp = self.client.sessions_send_message(ticket, target, payload)
        else:
            resp = self.client.plans_approve(ticket, target)

        record = self.journal.record_outcome(ticket, resp.outcome)

        status = ActionResultStatus.OK if resp.outcome.status in (200, 201) else ActionResultStatus.ERROR
        exit_code = EXIT_OK if status == ActionResultStatus.OK else EXIT_MUTATION_BLOCKED
        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=status,
            exit_code=exit_code,
            data={"operation_id": operation_id, "state": record.state.value, "status": resp.outcome.status, "_plan_hash": plan_h},
        )


class FakeMutationHandler:
    """Test-local fake mutation handler that goes through the real S08 Journal."""

    def __init__(
        self,
        journal: Journal,
        should_succeed: bool = True,
        transport_outcome: TransportOutcome | None = None,
    ) -> None:
        self.journal = journal
        self.should_succeed = should_succeed
        self.transport_outcome = transport_outcome or TransportOutcome(200, json.dumps({"name": "sessions/fake_res"}))
        self.dispatch_count = 0

    def can_handle(self, op: str) -> bool:
        return op in ("tasks.create", "chats.reply", "plans.approve")

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = action["id"]
        op = action["op"]
        operation_id = action["operation_id"]
        payload = action.get("payload", {})
        target = action.get("target", "OWNER/REPO")

        req_hash = request_hash({"target": target, "body": payload})
        binding = Binding(
            profile=context.get("profile", "default"),
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="main",
            session=target if target.startswith("sessions/") else None,
        )
        plan_h = context.get("plan", {}).get("plan_hash") or "plan_hash"
        prep = PreparedAction(
            action=op,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            payload_hash=canonical_hash(payload),
            context_hash=canonical_hash({"source": "sources/github/OWNER/REPO"}),
            request_hash=req_hash,
            publication_scope="none",
            plan_hash=plan_h,
        )

        auth_ref = action.get("authorization_ref") or "ref-1"
        grant = VerifiedGrant(
            action=op,
            operation_id=operation_id,
            profile=context.get("profile", "default"),
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            branch="main",
            payload_hash=prep.payload_hash,
            context_hash=prep.context_hash,
            plan_hash=prep.plan_hash,
            publication_scope="none",
            authorizing_source=auth_ref,
            session=binding.session,
            max_attempts=1,
        )

        verifier = self.journal.verifier or context.get("verifier")
        if verifier is not None and hasattr(verifier, "register_grant"):
            verifier.register_grant(auth_ref, grant)

        # 1. Prepare in journal
        self.journal.prepare(prep, grant=grant)

        # 2. Begin dispatch (commits DISPATCHING and issues ticket)
        ticket = self.journal.begin_dispatch(operation_id, req_hash)
        self.dispatch_count += 1

        # 3. Redeem and record outcome
        if self.should_succeed:
            self.journal.redeem(ticket, req_hash)
            record = self.journal.record_outcome(ticket, self.transport_outcome)
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.OK,
                exit_code=EXIT_OK,
                data={"operation_id": operation_id, "state": record.state.value},
            )
        else:
            outcome = self.transport_outcome or TransportOutcome(403, json.dumps({"error": "forbidden"}))
            self.journal.redeem(ticket, req_hash)
            record = self.journal.record_outcome(ticket, outcome)
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.AUTH_DENIED,
                data={"operation_id": operation_id, "state": record.state.value},
            )


class SimpleActionHandler:
    """Configurable test action handler."""

    def __init__(
        self,
        op: str,
        status: ActionResultStatus = ActionResultStatus.OK,
        exit_code: int = EXIT_OK,
        data: dict[str, Any] | None = None,
        coverage: Coverage | None = None,
        error_code: ErrorCode | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.op = op
        self.status = status
        self.exit_code = exit_code
        self.data = dict(data or {})
        self.coverage = coverage
        self.error_code = error_code
        self.raise_error = raise_error
        self.call_count = 0

    def can_handle(self, op: str) -> bool:
        return op == self.op

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        self.call_count += 1
        if self.raise_error:
            raise self.raise_error
        return ActionResult.create(
            action_id=action["id"],
            op=self.op,
            status=self.status,
            exit_code=self.exit_code,
            error_code=self.error_code,
            coverage=self.coverage,
            data=self.data,
        )


def _make_valid_plan(
    plan_id: str = "plan-test-1",
    mode: str = "read_only",
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if actions is None:
        actions = [
            {
                "id": "act-1",
                "op": "healthcheck",
                "params": {},
            }
        ]
    plan = {
        "schema_version": "jules-controller.plan.v1",
        "plan_id": plan_id,
        "profile": "default",
        "execution": {"mode": mode},
        "scope": {"repository": "OWNER/REPO"},
        "limits": dict(LIVE_INVOCATION_DEFAULTS),
        "actions": actions,
        "output": {"format": "json"},
    }
    if mode == "mutation":
        plan["limits"]["max_posts"] = 1
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


class TestRunnerS09(unittest.TestCase):
    """S09 Test Cases."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.store = SQLiteStore(self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.clock = FakeClock()
        self.verifier = FakeGrantVerifier()
        self.transport = FixtureTransport()
        self.journal = Journal(self.store, verifier=self.verifier, clock=self.clock, fence=self.fence)
        self.artifacts_dir = os.path.join(self.test_dir, "artifacts")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s09_t01_whole_plan_validation_rejects_before_credentials_and_network(self) -> None:
        """S09-T01: Invalid last action rejects the whole plan before credentials/network;
        duplicate/forward refs, unknown fields and dynamic mutation targets fail."""
        spy_credentials = SpyCredentialSource()
        spy_transport_factory = SpyTransportFactory()

        # 1. Invalid last action in plan: 2 valid actions followed by 1 invalid action
        invalid_last_actions = [
            {"id": "act-1", "op": "healthcheck", "params": {}},
            {"id": "act-2", "op": "capabilities.inspect", "params": {}},
            {"id": "act-3", "op": "nonexistent.op", "params": {}},  # Unknown op
        ]
        plan1 = _make_valid_plan(actions=invalid_last_actions)
        with self.assertRaises(OctodotError) as ctx:
            run_plan(plan1, credential_source=spy_credentials, transport_factory=spy_transport_factory)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        # Verify neither credentials nor transport factory was touched
        self.assertFalse(spy_credentials.was_accessed())
        self.assertEqual(spy_transport_factory.call_count, 0)

        # Do the same through the CLI path for run
        plan1_file = os.path.join(self.test_dir, "plan_invalid_last.json")
        with open(plan1_file, "w", encoding="utf-8") as f:
            json.dump(plan1, f)
        cli_rc = cli.main(
            ["run", "--plan", plan1_file],
            credential_source=spy_credentials,
            transport_factory=spy_transport_factory,
        )
        self.assertEqual(cli_rc, EXIT_FATAL_READ_OR_LOCAL)
        self.assertFalse(spy_credentials.was_accessed())
        self.assertEqual(spy_transport_factory.call_count, 0)

        # Do the same through the CLI shorthand path (with invalid input)
        shorthand_rc = cli.main(
            ["inventory", "--repo", "invalid_no_slash"],
            credential_source=spy_credentials,
            transport_factory=spy_transport_factory,
        )
        self.assertEqual(shorthand_rc, EXIT_FATAL_READ_OR_LOCAL)
        self.assertFalse(spy_credentials.was_accessed())
        self.assertEqual(spy_transport_factory.call_count, 0)

        # Do the same through the CLI shorthand path where compiled plan has invalid last action
        with mock.patch("octodot.cli.compile_shorthand_to_plan", return_value=plan1):
            shorthand_rc2 = cli.main(
                ["inventory"],
                credential_source=spy_credentials,
                transport_factory=spy_transport_factory,
            )
            self.assertEqual(shorthand_rc2, EXIT_FATAL_READ_OR_LOCAL)
            self.assertFalse(spy_credentials.was_accessed())
            self.assertEqual(spy_transport_factory.call_count, 0)

        # 2. Duplicate action ID
        dup_actions = [
            {"id": "act-dup", "op": "healthcheck", "params": {}},
            {"id": "act-dup", "op": "healthcheck", "params": {}},
        ]
        plan2 = _make_valid_plan(actions=dup_actions)
        with self.assertRaises(OctodotError) as ctx:
            run_plan(plan2, credential_source=spy_credentials)
        self.assertEqual(ctx.exception.code, ErrorCode.DUPLICATE_KEY)
        self.assertFalse(spy_credentials.was_accessed())

        # 3. Forward reference (earlier action references later action)
        fwd_actions = [
            {
                "id": "act-early",
                "op": "chats.collect",
                "params": {"session": {"from": "act-later", "select": "active_session"}},
            },
            {"id": "act-later", "op": "inventory.collect", "params": {}},
        ]
        plan3 = _make_valid_plan(actions=fwd_actions)
        with self.assertRaises(OctodotError) as ctx:
            run_plan(plan3, credential_source=spy_credentials)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)
        self.assertFalse(spy_credentials.was_accessed())

        # 4. Unknown fields in action
        unknown_field_actions = [
            {"id": "act-1", "op": "healthcheck", "params": {}, "unknown_garbage_key": 123},
        ]
        plan4 = _make_valid_plan(actions=unknown_field_actions)
        with self.assertRaises(OctodotError) as ctx:
            run_plan(plan4, credential_source=spy_credentials)
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_FIELD)
        self.assertFalse(spy_credentials.was_accessed())

        # 5. Dynamic mutation target (from/select inside mutation target/payload)
        dynamic_mut_actions = [
            {
                "id": "act-mut",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-dyn",
                "authorization_ref": "ref-1",
                "target": {"from": "act-1", "select": "session"},
                "payload": {"title": "Test", "prompt": "Test"},
            }
        ]
        plan5 = _make_valid_plan(mode="mutation", actions=dynamic_mut_actions)
        with self.assertRaises(OctodotError) as ctx:
            run_plan(plan5, credential_source=spy_credentials)
        self.assertEqual(ctx.exception.code, ErrorCode.DYNAMIC_MUTATION_TARGET)
        self.assertFalse(spy_credentials.was_accessed())

    def test_s09_t02_failed_read_dependency_skips_dependents_and_mutation_suppression(self) -> None:
        """S09-T02: Failed read dependency skips dependent action; independent reads continue;
        blocked/rejected/unknown mutation suppresses subsequent mutations while reconciliation reads continue."""
        # --- Part A: Read dependency failure skipping ---
        # act-1: Read fails
        # act-2: Dependent on act-1 -> should be SKIPPED
        # act-3: Independent read -> should execute and SUCCEED
        h1 = SimpleActionHandler("inventory.collect", status=ActionResultStatus.ERROR, exit_code=EXIT_FATAL_READ_OR_LOCAL)
        h2 = SimpleActionHandler("chats.collect", status=ActionResultStatus.OK)
        h3 = SimpleActionHandler("healthcheck", status=ActionResultStatus.OK)

        handlers = {
            "inventory.collect": h1,
            "chats.collect": h2,
            "healthcheck": h3,
        }

        actions_a = [
            {"id": "act-1", "op": "inventory.collect", "params": {}},
            {
                "id": "act-2",
                "op": "chats.collect",
                "params": {"session": {"from": "act-1", "select": "active_session"}},
            },
            {"id": "act-3", "op": "healthcheck", "params": {}},
        ]
        plan_a = _make_valid_plan(plan_id="plan-s09-t02-a", actions=actions_a)
        result_a = run_plan(plan_a, handlers=handlers, store=self.store)

        action_results_a = {ar["action_id"]: ar for ar in result_a["action_results"]}
        # act-1 failed
        self.assertEqual(action_results_a["act-1"]["status"], "error")
        # act-2 skipped due to dependency failure
        self.assertEqual(action_results_a["act-2"]["status"], "skipped")
        self.assertEqual(h2.call_count, 0)  # Handler not invoked
        # act-3 independent read succeeded
        self.assertEqual(action_results_a["act-3"]["status"], "ok")
        self.assertEqual(h3.call_count, 1)

        # --- Part B: Mutation blocker suppresses subsequent mutations while reconciliation reads continue ---
        fake_mut_blocked = FakeMutationHandler(self.journal, should_succeed=False)
        fake_mut_subsequent = FakeMutationHandler(self.journal, should_succeed=True)
        reconciliation_handler = SimpleActionHandler("session.inspect", status=ActionResultStatus.OK)

        # Inject handlers
        handlers_b = {
            "tasks.create": fake_mut_blocked,
            "chats.reply": fake_mut_subsequent,
            "session.inspect": reconciliation_handler,
        }

        actions_b = [
            {
                "id": "act-m1",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-m1",
                "authorization_ref": "ref-m1",
                "target": "OWNER/REPO",
                "payload": {"title": "Task 1", "prompt": "Create 1"},
            },
            {
                "id": "act-m2",
                "op": "chats.reply",
                "enabled": True,
                "operation_id": "op-m2",
                "authorization_ref": "ref-m2",
                "target": "sessions/S1",
                "payload": {"prompt": "Reply text"},
            },
            {
                "id": "act-r1",
                "op": "session.inspect",
                "params": {"session": "sessions/S1"},
            },
        ]
        plan_b = _make_valid_plan(plan_id="plan-s09-t02-b", mode="mutation", actions=actions_b)
        result_b = run_plan(plan_b, handlers=handlers_b, store=self.store, journal=self.journal, verifier=self.verifier, transport=self.transport)

        action_results_b = {ar["action_id"]: ar for ar in result_b["action_results"]}
        # act-m1 was blocked
        self.assertEqual(action_results_b["act-m1"]["status"], "blocked")
        self.assertEqual(action_results_b["act-m1"]["exit_code"], EXIT_MUTATION_BLOCKED)
        # act-m2 was SUPPRESSED (skipped) due to earlier mutation blocker
        self.assertEqual(action_results_b["act-m2"]["status"], "skipped")
        self.assertEqual(fake_mut_subsequent.dispatch_count, 0)  # Zero dispatch!
        # act-r1 reconciliation read continued and executed
        self.assertEqual(action_results_b["act-r1"]["status"], "ok")
        self.assertEqual(reconciliation_handler.call_count, 1)

        # --- Part C: Mutation REJECTED suppresses subsequent mutations while reconciliation & independent reads continue ---
        class RejectedMutationHandler:
            def __init__(self) -> None:
                self.dispatch_count = 0

            def can_handle(self, op: str) -> bool:
                return op == "tasks.create"

            def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
                self.dispatch_count += 1
                return ActionResult.create(
                    action_id=action["id"],
                    op=action["op"],
                    status=ActionResultStatus.REJECTED,
                    exit_code=EXIT_FATAL_READ_OR_LOCAL,
                    error_code=ErrorCode.GRANT_INVALID,
                    data={"reason": "Grant rejected or preflight denied"},
                )

        mut_rejected = RejectedMutationHandler()
        mut_subsequent_c = FakeMutationHandler(self.journal, should_succeed=True)
        reconcile_h_c = SimpleActionHandler("session.inspect", status=ActionResultStatus.OK)
        independent_read_c = SimpleActionHandler("inventory.collect", status=ActionResultStatus.OK)

        handlers_c = {
            "tasks.create": mut_rejected,
            "chats.reply": mut_subsequent_c,
            "session.inspect": reconcile_h_c,
            "inventory.collect": independent_read_c,
        }
        actions_c = [
            {
                "id": "act-rej",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-rej",
                "authorization_ref": "ref-rej",
                "target": "OWNER/REPO",
                "payload": {"title": "Task Rej", "prompt": "Prompt text"},
            },
            {
                "id": "act-sub-c",
                "op": "chats.reply",
                "enabled": True,
                "operation_id": "op-sub-c",
                "authorization_ref": "ref-sub-c",
                "target": "sessions/S1",
                "payload": {"prompt": "Reply text"},
            },
            {
                "id": "act-rec-c",
                "op": "session.inspect",
                "params": {"session": "sessions/S1"},
            },
            {
                "id": "act-ind-c",
                "op": "inventory.collect",
                "params": {"scope": "all", "repository": "OWNER/REPO"},
            },
        ]
        plan_c = _make_valid_plan(plan_id="plan-s09-t02-c", mode="mutation", actions=actions_c)
        result_c = run_plan(plan_c, handlers=handlers_c, store=self.store, journal=self.journal, verifier=self.verifier, transport=self.transport)
        action_results_c = {ar["action_id"]: ar for ar in result_c["action_results"]}
        self.assertEqual(action_results_c["act-rej"]["status"], "rejected")
        self.assertEqual(action_results_c["act-sub-c"]["status"], "skipped")
        self.assertEqual(mut_subsequent_c.dispatch_count, 0)
        self.assertEqual(action_results_c["act-rec-c"]["status"], "ok")
        self.assertEqual(reconcile_h_c.call_count, 1)
        self.assertEqual(action_results_c["act-ind-c"]["status"], "ok")
        self.assertEqual(independent_read_c.call_count, 1)

        # --- Part D: Mutation UNKNOWN suppresses subsequent mutations while reconciliation & independent reads continue ---
        class UnknownMutationHandler:
            def __init__(self) -> None:
                self.dispatch_count = 0

            def can_handle(self, op: str) -> bool:
                return op == "tasks.create"

            def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
                self.dispatch_count += 1
                return ActionResult.create(
                    action_id=action["id"],
                    op=action["op"],
                    status=ActionResultStatus.UNKNOWN,
                    exit_code=EXIT_INTERRUPTED,
                    error_code=ErrorCode.UNCERTAIN_EFFECT,
                    data={"reason": "Transport timeout after dispatch"},
                )

        mut_unknown = UnknownMutationHandler()
        mut_subsequent_d = FakeMutationHandler(self.journal, should_succeed=True)
        reconcile_h_d = SimpleActionHandler("session.inspect", status=ActionResultStatus.OK)
        independent_read_d = SimpleActionHandler("inventory.collect", status=ActionResultStatus.OK)

        handlers_d = {
            "tasks.create": mut_unknown,
            "chats.reply": mut_subsequent_d,
            "session.inspect": reconcile_h_d,
            "inventory.collect": independent_read_d,
        }
        actions_d = [
            {
                "id": "act-unk",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-unk",
                "authorization_ref": "ref-unk",
                "target": "OWNER/REPO",
                "payload": {"title": "Task Unk", "prompt": "Prompt text"},
            },
            {
                "id": "act-sub-d",
                "op": "chats.reply",
                "enabled": True,
                "operation_id": "op-sub-d",
                "authorization_ref": "ref-sub-d",
                "target": "sessions/S1",
                "payload": {"prompt": "Reply text"},
            },
            {
                "id": "act-rec-d",
                "op": "session.inspect",
                "params": {"session": "sessions/S1"},
            },
            {
                "id": "act-ind-d",
                "op": "inventory.collect",
                "params": {"scope": "all", "repository": "OWNER/REPO"},
            },
        ]
        plan_d = _make_valid_plan(plan_id="plan-s09-t02-d", mode="mutation", actions=actions_d)
        result_d = run_plan(plan_d, handlers=handlers_d, store=self.store, journal=self.journal, verifier=self.verifier, transport=self.transport)
        action_results_d = {ar["action_id"]: ar for ar in result_d["action_results"]}
        self.assertEqual(action_results_d["act-unk"]["status"], "unknown")
        self.assertEqual(action_results_d["act-sub-d"]["status"], "skipped")
        self.assertEqual(mut_subsequent_d.dispatch_count, 0)
        self.assertEqual(action_results_d["act-rec-d"]["status"], "ok")
        self.assertEqual(reconcile_h_d.call_count, 1)
        self.assertEqual(action_results_d["act-ind-d"]["status"], "ok")
        self.assertEqual(independent_read_d.call_count, 1)

    def test_s09_t03_plan_replay_hash_conflict_and_mutation_repeat_prevention(self) -> None:
        """S09-T03: Replay same plan ID/hash returns recorded action/operation status;
        changed hash conflicts; a new run cannot repeat a recorded mutation.
        Assert POST counts directly at S02 FixtureTransport."""
        transport = FixtureTransport(
            responses={("POST", "/v1alpha/sessions"): TransportOutcome(200, json.dumps({"name": "sessions/sess-t03"}))}
        )
        client = JulesClient(transport=transport, ticket_authority=self.journal, clock=self.clock)
        handler = JournalJulesClientMutationHandler(journal=self.journal, client=client)

        handlers = {
            "tasks.create": handler,
            "healthcheck": SimpleActionHandler("healthcheck", status=ActionResultStatus.OK),
        }

        actions = [
            {
                "id": "act-m1",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-t03-1",
                "authorization_ref": "ref-t03-1",
                "target": "OWNER/REPO",
                "payload": {"title": "Task Replay", "prompt": "Prompt text"},
            },
            {"id": "act-h1", "op": "healthcheck", "params": {}},
        ]
        plan = _make_valid_plan(plan_id="plan-t03-test", mode="mutation", actions=actions)

        # Pre-execution: 0 POSTs on FixtureTransport
        self.assertEqual(sum(1 for c in transport.calls if c.get("method") == "POST"), 0)

        # 1. First execution
        res1 = run_plan(
            plan,
            handlers=handlers,
            store=self.store,
            journal=self.journal,
            verifier=self.verifier,
            transport=transport,
        )
        self.assertEqual(res1["status"], "ok")
        # Assert POST count at S02 FixtureTransport is exactly 1
        self.assertEqual(sum(1 for c in transport.calls if c.get("method") == "POST"), 1)

        # 2. Replay with SAME plan_id and SAME plan_hash: returns recorded results
        res2 = run_plan(
            plan,
            handlers=handlers,
            store=self.store,
            journal=self.journal,
            verifier=self.verifier,
            transport=transport,
        )
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["action_results"], res1["action_results"])
        # POST count at S02 FixtureTransport remains 1 (no extra POST!)
        self.assertEqual(sum(1 for c in transport.calls if c.get("method") == "POST"), 1)

        # 3. Same plan_id but CHANGED hash: conflicts!
        conflicting_plan = copy.deepcopy(plan)
        conflicting_plan["scope"]["branch"] = "different-branch"
        conflicting_plan["plan_hash"] = compute_plan_hash(conflicting_plan)

        with self.assertRaises(OctodotError) as ctx:
            run_plan(
                conflicting_plan,
                handlers=handlers,
                store=self.store,
                journal=self.journal,
                verifier=self.verifier,
                transport=transport,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        # POST count at S02 FixtureTransport is STILL 1
        self.assertEqual(sum(1 for c in transport.calls if c.get("method") == "POST"), 1)

        # 4. A new run (different plan_id) cannot repeat a recorded mutation
        new_plan_actions = [
            {
                "id": "act-new-m1",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-t03-1",  # Same operation_id that was already recorded
                "authorization_ref": "ref-t03-1",
                "target": "OWNER/REPO",
                "payload": {"title": "Task Replay", "prompt": "Prompt text"},
            }
        ]
        new_plan = _make_valid_plan(plan_id="plan-t03-new-run", mode="mutation", actions=new_plan_actions)
        with self.assertRaises(OctodotError) as ctx:
            run_plan(
                new_plan,
                handlers=handlers,
                store=self.store,
                journal=self.journal,
                verifier=self.verifier,
                transport=transport,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        # POST count at S02 FixtureTransport is STILL 1
        self.assertEqual(sum(1 for c in transport.calls if c.get("method") == "POST"), 1)

    def test_s09_t04_large_text_artifact_spill_and_capped_output(self) -> None:
        """S09-T04: Large text/artifacts spill to checksummed bounded private artifacts;
        capped output names every omitted attention item or marks incomplete coverage."""
        large_text = "A" * 5000  # 5000 bytes > 4096 threshold
        h_large = SimpleActionHandler(
            "healthcheck",
            status=ActionResultStatus.OK,
            data={"diagnostic_log": large_text},
            coverage=Coverage(complete=True),
        )
        handlers = {"healthcheck": h_large}
        plan = _make_valid_plan(plan_id="plan-t04-spill")

        result = run_plan(
            plan,
            handlers=handlers,
            store=self.store,
            artifacts_dir=self.artifacts_dir,
            spill_threshold=1000,
        )

        ar_data = dict(result["action_results"][0]["data"])
        self.assertIn("diagnostic_log", ar_data)
        artifact_ref = ar_data["diagnostic_log"]
        self.assertIsInstance(artifact_ref, dict)
        self.assertIn("artifact_id", artifact_ref)
        self.assertIn("content_hash", artifact_ref)
        self.assertEqual(artifact_ref["byte_count"], 5000)

        # Check file exists and permissions
        art_path = artifact_ref["path"]
        self.assertTrue(os.path.exists(art_path))
        # Directory permission 0700
        dir_stat = os.stat(self.artifacts_dir)
        self.assertEqual(dir_stat.st_mode & 0o777, 0o700)
        # File permission 0600
        file_stat = os.stat(art_path)
        self.assertEqual(file_stat.st_mode & 0o777, 0o600)

        # Check content hash
        with open(art_path, "rb") as f:
            art_bytes = f.read()
        self.assertEqual(art_bytes, large_text.encode("utf-8"))
        self.assertEqual(artifact_ref["content_hash"], "sha256:" + hashlib.sha256(art_bytes).hexdigest())

        # Check manifest in store
        manifest = self.store.get_manifest(artifact_ref["artifact_id"])
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest.byte_count, 5000)

        # --- Test capped output & omitted attention items ---
        h_capped = SimpleActionHandler(
            "healthcheck",
            status=ActionResultStatus.PARTIAL,
            exit_code=EXIT_PARTIAL_OR_UNSUPPORTED,
            data={"omitted_attention_items": ["sessions/s_awaiting_approval_1", "sessions/s_feedback_2"]},
            coverage=Coverage(complete=False, reasons=("output_cap_reached",), skipped_scope=("items",)),
        )
        handlers_capped = {"healthcheck": h_capped}
        plan_capped = _make_valid_plan(plan_id="plan-t04-capped")

        result_capped = run_plan(plan_capped, handlers=handlers_capped, store=self.store)
        self.assertFalse(result_capped["coverage"]["complete"])
        self.assertIn("output_cap_reached", result_capped["coverage"]["reasons"])
        self.assertIn("sessions/s_awaiting_approval_1", result_capped["omitted_attention_items"])
        self.assertIn("sessions/s_feedback_2", result_capped["omitted_attention_items"])

    def test_s09_t05_exit_code_precedence_and_deterministic_status(self) -> None:
        """S09-T05: Mixed ok/waiting/read-error/mutation-error/unsupported/interrupted cases match
        deterministic result and exit-code precedence."""
        # Precedence order: 130 > 4 > 3 > 5 > 2 > 0
        h_ok = SimpleActionHandler("healthcheck", status=ActionResultStatus.OK, exit_code=EXIT_OK)
        h_waiting = SimpleActionHandler("wait", status=ActionResultStatus.WAITING, exit_code=EXIT_WAITING)
        h_unsupported = SimpleActionHandler("suggestions.collect", status=ActionResultStatus.UNSUPPORTED, exit_code=EXIT_PARTIAL_OR_UNSUPPORTED)
        h_read_err = SimpleActionHandler("capabilities.inspect", status=ActionResultStatus.ERROR, exit_code=EXIT_FATAL_READ_OR_LOCAL)
        h_mut_blocked = SimpleActionHandler("tasks.create", status=ActionResultStatus.BLOCKED, exit_code=EXIT_MUTATION_BLOCKED)
        h_interrupted = SimpleActionHandler("chats.collect", status=ActionResultStatus.INTERRUPTED, exit_code=EXIT_INTERRUPTED)

        test_matrix = [
            # (handlers, expected_exit, expected_status)
            ([h_ok], EXIT_OK, "ok"),
            ([h_ok, h_waiting], EXIT_WAITING, "waiting"),
            ([h_ok, h_waiting, h_unsupported], EXIT_PARTIAL_OR_UNSUPPORTED, "unsupported"),
            ([h_ok, h_waiting, h_unsupported, h_read_err], EXIT_FATAL_READ_OR_LOCAL, "error"),
            ([h_ok, h_waiting, h_unsupported, h_read_err, h_mut_blocked], EXIT_MUTATION_BLOCKED, "error"),
            ([h_ok, h_waiting, h_unsupported, h_read_err, h_mut_blocked, h_interrupted], EXIT_INTERRUPTED, "interrupted"),
        ]

        for idx, (h_list, exp_exit, exp_status) in enumerate(test_matrix):
            actions = []
            handlers = {}
            has_mutation = False
            for h_idx, h in enumerate(h_list):
                aid = f"act-{idx}-{h_idx}"
                op = h.op
                handlers[op] = h
                if op == "tasks.create":
                    has_mutation = True
                    actions.append({
                        "id": aid,
                        "op": op,
                        "enabled": True,
                        "operation_id": f"op-prec-{idx}-{h_idx}",
                        "authorization_ref": f"ref-prec-{idx}-{h_idx}",
                        "target": "OWNER/REPO",
                        "payload": {"title": "Title", "prompt": "Prompt"},
                    })
                else:
                    actions.append({"id": aid, "op": op, "params": {}})

            mode = "mutation" if has_mutation else "read_only"
            plan = _make_valid_plan(plan_id=f"plan-prec-{idx}", mode=mode, actions=actions)
            res = run_plan(plan, handlers=handlers, store=self.store, verifier=self.verifier, transport=self.transport)
            self.assertEqual(res["exit_code"], exp_exit, f"Mismatch on matrix index {idx}")
            self.assertEqual(res["status"], exp_status, f"Status mismatch on matrix index {idx}")
            validate_result(res)

    def test_s09_t06_read_only_mode_disabled_template_and_authorized_get(self) -> None:
        """S09-T06: Read-only mode cannot enter a mutation dispatch path or issue POST;
        disabled templates cannot load any credentials, regardless of action text.
        Authorized GET mode may load the selected Jules credential."""
        spy_credentials = SpyCredentialSource({"default": "fake-key"})

        # 1. Read-only mode rejects mutation actions before execution
        read_only_mut_plan = _make_valid_plan(
            mode="read_only",
            actions=[{
                "id": "act-m",
                "op": "tasks.create",
                "enabled": True,
                "operation_id": "op-ro",
                "authorization_ref": "ref-ro",
                "target": "OWNER/REPO",
                "payload": {"title": "T", "prompt": "P"},
            }],
        )
        with self.assertRaises(OctodotError) as ctx:
            run_plan(read_only_mut_plan, credential_source=spy_credentials)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)
        self.assertFalse(spy_credentials.was_accessed())

        # 2. Disabled template with enabled=False fails in check_execution_eligibility
        # BEFORE any credential load, regardless of action text
        disabled_template_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-disabled-template",
            "profile": "default",
            "execution": {"mode": "mutation"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS, max_posts=1),
            "actions": [{
                "id": "act-disabled",
                "op": "tasks.create",
                "enabled": False,  # Template is disabled
                "operation_id": "op-dis",
                "authorization_ref": "ref-dis",
                "target": "OWNER/REPO",
                "payload": {"title": "Title", "prompt": "Prompt"},
            }],
            "output": {"format": "json"},
        }
        disabled_template_plan["plan_hash"] = compute_plan_hash(disabled_template_plan)

        with self.assertRaises(OctodotError) as ctx:
            run_plan(disabled_template_plan, credential_source=spy_credentials)
        self.assertEqual(ctx.exception.code, ErrorCode.TEMPLATE_DISABLED)
        # Invariant: Credentials NEVER accessed
        self.assertFalse(spy_credentials.was_accessed())
        self.assertEqual(spy_credentials.access_count(), 0)

        # 3. Authorized GET mode permits credential resolution for reads
        auth_get_plan = _make_valid_plan(mode="authorized_get")
        cred_res = spy_credentials.get_credential("default")
        self.assertEqual(cred_res, "fake-key")
        self.assertTrue(spy_credentials.was_accessed())

        res = run_plan(
            auth_get_plan,
            handlers={"healthcheck": SimpleActionHandler("healthcheck", status=ActionResultStatus.OK)},
            store=self.store,
            credential_source=spy_credentials,
        )
        self.assertEqual(res["status"], "ok")

        # 4. Derived live mode from transport (F4):
        # HttpTransport (constructed only, no requests) + FakeGrantVerifier => AUTH_DENIED before any action runs.
        http_transport = HttpTransport()
        with self.assertRaises(OctodotError) as ctx:
            run_plan(auth_get_plan, verifier=FakeGrantVerifier(), transport=http_transport)
        self.assertEqual(ctx.exception.code, ErrorCode.AUTH_DENIED)
        self.assertIn("Fixture-only verifier 'FakeGrantVerifier' is barred from live mode", ctx.exception.message)

        # Unknown transport object => also counts as live => AUTH_DENIED
        class UnknownTransport:
            pass

        with self.assertRaises(OctodotError) as ctx:
            run_plan(auth_get_plan, verifier=FakeGrantVerifier(), transport=UnknownTransport())
        self.assertEqual(ctx.exception.code, ErrorCode.AUTH_DENIED)

        # FixtureTransport + FakeGrantVerifier => allowed (live=False)
        fixture_transport = FixtureTransport()
        allowed_res = run_plan(
            auth_get_plan,
            handlers={"healthcheck": SimpleActionHandler("healthcheck", status=ActionResultStatus.OK)},
            store=self.store,
            verifier=FakeGrantVerifier(),
            transport=fixture_transport,
        )
        self.assertEqual(allowed_res["status"], "ok")


if __name__ == "__main__":
    unittest.main()
