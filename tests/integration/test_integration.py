"""Integration tests for S14 offline core release (S14-T03).

Standard library only. Compatible with Python 3.10+.
Covers S14-T03:
- End-to-end offline read plan execution through the CLI entrypoint
- Prepare command with --validate-only offline verification
- Disabled template actions blocked before credentials or network access
- Fake-grant mutation pipeline (chats.reply, tasks.create, plans.approve)
  through the production registry with FixtureTransport (POST count <= 1)
- Crash / restart recovery with read-only reconciliation of unknown operations
- Resumed bounded wait workflow across runner executions
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.actions.approve import _inspection_to_material_context
from octodot.api import JulesClient
from octodot.authorization import (
    DisabledGrantVerifier,
    FakeGrantVerifier,
)
from octodot.cli import main as cli_main
from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    canonical_hash,
    compute_plan_hash,
    validate_result,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_WAITING,
    ErrorCode,
    OctodotError,
)
from octodot.journal import Journal
from octodot.models import (
    Binding,
    OperationRecord,
    OperationState,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.preparation import prepare_action
from octodot.reads import ReadService
from octodot.registry import build_handler_registry
from octodot.runner import run_plan
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class TestIntegrationS14T03(unittest.TestCase):
    """S14-T03 End-to-end integration test suite using real runner and registry."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.state_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s14_t03_cli_e2e_read_plan(self) -> None:
        """S14-T03: Execute an end-to-end offline read plan via the CLI entrypoint."""
        plan_doc = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-integ-read-01",
            "profile": "default",
            "execution": {"mode": "read_only"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [
                {
                    "id": "act-health",
                    "op": "healthcheck",
                    "params": {},
                },
                {
                    "id": "act-cap",
                    "op": "capabilities.inspect",
                    "params": {},
                },
            ],
            "output": {"format": "json"},
        }
        plan_doc["plan_hash"] = compute_plan_hash(plan_doc)

        plan_file = os.path.join(self.test_dir, "read_plan.json")
        result_file = os.path.join(self.test_dir, "read_result.json")
        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump(plan_doc, f)

        exit_code = cli_main(
            ["run", "--plan", plan_file, "--result", result_file, "--state-dir", self.state_dir]
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(os.path.isfile(result_file))

        with open(result_file, "rb") as f:
            result_doc = json.load(f)

        validate_result(result_doc)
        self.assertEqual(result_doc["status"], "ok")
        self.assertEqual(result_doc["exit_code"], 0)
        self.assertEqual(len(result_doc["action_results"]), 2)
        for ar in result_doc["action_results"]:
            self.assertEqual(ar["status"], "ok")
            self.assertEqual(ar["exit_code"], 0)

    def test_s14_t03_cli_prepare_validate_only(self) -> None:
        """S14-T03: Prepare --validate-only offline verification through the CLI."""
        plan_doc = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-integ-prepare-01",
            "profile": "default",
            "execution": {"mode": "read_only"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [
                {
                    "id": "act-1",
                    "op": "healthcheck",
                    "params": {},
                }
            ],
            "output": {"format": "json"},
        }
        plan_doc["plan_hash"] = compute_plan_hash(plan_doc)

        plan_file = os.path.join(self.test_dir, "prepare_plan.json")
        report_file = os.path.join(self.test_dir, "prepare_report.json")
        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump(plan_doc, f)

        exit_code = cli_main(
            ["prepare", "--validate-only", "--plan", plan_file, "--result", report_file]
        )
        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(os.path.isfile(report_file))

        with open(report_file, "r", encoding="utf-8") as f:
            report_doc = json.load(f)

        self.assertEqual(report_doc["schema_version"], "jules-controller.report.v1")
        self.assertEqual(report_doc["mode"], "validate_only")
        self.assertTrue(report_doc["is_valid"])
        self.assertEqual(report_doc["blockers"], [])

    def test_s14_t03_disabled_template_blocked_before_credentials(self) -> None:
        """S14-T03: Disabled template actions block execution before loading credentials."""
        credential_accessed = False

        def cred_spy() -> str:
            nonlocal credential_accessed
            credential_accessed = True
            return "fake-key"

        plan_doc = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-disabled-template-01",
            "profile": "default",
            "execution": {"mode": "mutation"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [
                {
                    "id": "act-disabled",
                    "op": "chats.reply",
                    "enabled": False,  # Template disabled
                    "target": "sessions/s-integ-1",
                    "payload": {"text": "Hello world"},
                    "operation_id": "op-reply-disabled",
                    "authorization_ref": "ref-disabled",
                }
            ],
            "output": {"format": "json"},
        }
        plan_doc["plan_hash"] = compute_plan_hash(plan_doc)

        # 1. run_plan raises TEMPLATE_DISABLED before accessing credentials
        with self.assertRaises(OctodotError) as ctx:
            run_plan(
                plan=plan_doc,
                credential_source=cred_spy,
                verifier=DisabledGrantVerifier(),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.TEMPLATE_DISABLED)
        self.assertFalse(credential_accessed, "Credential source must not be accessed")

        # 2. CLI invocation also blocks before credentials
        plan_file = os.path.join(self.test_dir, "disabled_plan.json")
        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump(plan_doc, f)

        exit_code = cli_main(
            ["run", "--plan", plan_file],
            credential_source=cred_spy,
        )
        self.assertEqual(exit_code, EXIT_FATAL_READ_OR_LOCAL)
        self.assertFalse(credential_accessed, "Credential source must not be accessed via CLI")

    def test_s14_t03_fake_grant_reply_create_approve_through_registry(self) -> None:
        """S14-T03: Real runner and registry execute reply, create, and approve (each POST <= 1)."""
        sources_data = {
            "sources": [
                {
                    "name": "sources/github/OWNER/REPO",
                    "id": "src-1",
                    "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                }
            ]
        }

        # 1. chats.reply setup
        session_reply = {
            "name": "sessions/s-reply-1",
            "id": "s-reply-1",
            "title": "Reply Session",
            "state": "RUNNING",
            "createTime": "2026-10-07T12:00:00Z",
            "updateTime": "2026-10-07T12:01:00Z",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/integ"},
            },
        }
        activities_reply = {
            "activities": [
                {
                    "name": "sessions/s-reply-1/activities/act-1",
                    "id": "act-1",
                    "type": "userMessage",
                    "originator": "USER",
                    "createTime": "2026-10-07T12:00:00Z",
                    "text": "Hello",
                },
                {
                    "name": "sessions/s-reply-1/activities/act-2",
                    "id": "act-2",
                    "type": "agentMessage",
                    "originator": "AGENT",
                    "createTime": "2026-10-07T12:01:00Z",
                    "text": "How can I help you?",
                },
            ]
        }

        reply_action = {
            "id": "act-reply-1",
            "op": "chats.reply",
            "enabled": True,
            "target": "sessions/s-reply-1",
            "payload": {"text": "Yes proceed please"},
            "operation_id": "op-reply-1",
            "authorization_ref": "grant-reply-1",
        }
        reply_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-reply-01",
            "profile": "default",
            "execution": {"mode": "mutation"},
            "scope": {"repository": "OWNER/REPO", "sessions": ["sessions/s-reply-1"]},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [reply_action],
            "output": {"format": "json"},
        }
        reply_plan["plan_hash"] = compute_plan_hash(reply_plan)

        transport_reply = FixtureTransport(responses={
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=json.dumps(sources_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-reply-1"): TransportOutcome(status=200, body=json.dumps(session_reply).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-reply-1/activities"): TransportOutcome(status=200, body=json.dumps(activities_reply).encode("utf-8")),
            ("POST", "/v1alpha/sessions/s-reply-1:sendMessage"): TransportOutcome(status=200, body=b"{}"),
        })
        client_reply = JulesClient(transport=transport_reply, clock=self.clock)
        read_service_reply = ReadService(api=client_reply)

        binding_reply = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/integ",
            session="sessions/s-reply-1",
        )
        prep_reply = prepare_action(
            reply_action,
            reply_plan,
            current_profile_epoch=1,
            read_service=read_service_reply,
            binding=binding_reply,
        )
        grant_reply = VerifiedGrant(
            action=prep_reply.action,
            operation_id=prep_reply.operation_id,
            profile=prep_reply.binding.profile,
            profile_epoch=prep_reply.binding.profile_epoch,
            source=prep_reply.binding.source,
            repository=prep_reply.binding.repository,
            branch="feature/integ",
            session=prep_reply.binding.session,
            payload_hash=prep_reply.payload_hash,
            context_hash=prep_reply.context_hash,
            plan_hash=prep_reply.plan_hash,
            publication_scope=prep_reply.publication_scope,
            authorizing_source="test_integration",
        )
        verifier_reply = FakeGrantVerifier(grants={"grant-reply-1": grant_reply})
        journal_reply = Journal(store=self.store, verifier=verifier_reply, fence=self.fence, clock=self.clock)
        client_reply.ticket_authority = journal_reply

        handlers_reply = build_handler_registry(
            mode="mutation",
            store=self.store,
            read_service=read_service_reply,
            clock=self.clock,
            journal=journal_reply,
            verifier=verifier_reply,
            transport=transport_reply,
            fence=self.fence,
            api=client_reply,
        )

        res_reply = run_plan(
            reply_plan,
            handlers=handlers_reply,
            store=self.store,
            read_service=read_service_reply,
            journal=journal_reply,
            verifier=verifier_reply,
            clock=self.clock,
            transport=transport_reply,
        )
        self.assertEqual(res_reply["exit_code"], EXIT_OK)
        reply_posts = sum(1 for c in transport_reply.calls if c["method"] == "POST")
        self.assertLessEqual(reply_posts, 1)

        # 2. tasks.create setup
        created_session_data = {
            "name": "sessions/s-created-1",
            "id": "s-created-1",
            "title": "Created Task",
            "state": "RUNNING",
            "createTime": "2026-10-07T12:03:00Z",
            "updateTime": "2026-10-07T12:03:00Z",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/integ"},
            },
            "prompt": "Create new task marker-01",
        }
        create_action = {
            "id": "act-create-1",
            "op": "tasks.create",
            "enabled": True,
            "target": "OWNER/REPO",
            "payload": {
                "prompt": "Create new task marker-01",
                "title": "Created Task",
                "starting_branch": "feature/integ",
            },
            "preconditions": {
                "branch": "feature/integ",
            },
            "operation_id": "op-create-1",
            "authorization_ref": "grant-create-1",
        }
        create_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-create-01",
            "profile": "default",
            "execution": {"mode": "mutation"},
            "scope": {"repository": "OWNER/REPO", "branch": "feature/integ"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [create_action],
            "output": {"format": "json"},
        }
        create_plan["plan_hash"] = compute_plan_hash(create_plan)

        transport_create = FixtureTransport(responses={
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=json.dumps(sources_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=json.dumps({"sessions": [created_session_data]}).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-created-1"): TransportOutcome(status=200, body=json.dumps(created_session_data).encode("utf-8")),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=json.dumps(created_session_data).encode("utf-8")),
        })
        client_create = JulesClient(transport=transport_create, clock=self.clock)
        read_service_create = ReadService(api=client_create)

        prep_create = prepare_action(create_action, create_plan, current_profile_epoch=1, read_service=read_service_create)
        grant_create = VerifiedGrant(
            action=prep_create.action,
            operation_id=prep_create.operation_id,
            profile=prep_create.binding.profile,
            profile_epoch=prep_create.binding.profile_epoch,
            source=prep_create.binding.source,
            repository=prep_create.binding.repository,
            branch=prep_create.binding.starting_branch,
            session=prep_create.binding.session,
            payload_hash=prep_create.payload_hash,
            context_hash=prep_create.context_hash,
            plan_hash=prep_create.plan_hash,
            publication_scope=prep_create.publication_scope,
            authorizing_source="test_integration",
        )
        verifier_create = FakeGrantVerifier(grants={"grant-create-1": grant_create})
        journal_create = Journal(store=self.store, verifier=verifier_create, fence=self.fence, clock=self.clock)
        client_create.ticket_authority = journal_create

        handlers_create = build_handler_registry(
            mode="mutation",
            store=self.store,
            read_service=read_service_create,
            clock=self.clock,
            journal=journal_create,
            verifier=verifier_create,
            transport=transport_create,
            fence=self.fence,
            api=client_create,
        )

        res_create = run_plan(
            create_plan,
            handlers=handlers_create,
            store=self.store,
            read_service=read_service_create,
            journal=journal_create,
            verifier=verifier_create,
            clock=self.clock,
            transport=transport_create,
        )
        self.assertEqual(res_create["exit_code"], EXIT_OK)
        create_posts = sum(1 for c in transport_create.calls if c["method"] == "POST")
        self.assertLessEqual(create_posts, 1)

        # 3. plans.approve setup
        session_approve = {
            "name": "sessions/s-approve-1",
            "id": "s-approve-1",
            "title": "Approve Session",
            "state": "PLANNING",
            "requirePlanApproval": True,
            "createTime": "2026-10-07T12:00:00Z",
            "updateTime": "2026-10-07T12:01:00Z",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/integ"},
            },
        }
        activities_approve_1 = {
            "activities": [
                {
                    "name": "sessions/s-approve-1/activities/act-1",
                    "id": "act-1",
                    "type": "userMessage",
                    "originator": "USER",
                    "createTime": "2026-10-07T12:00:00Z",
                    "text": "Hello",
                },
                {
                    "name": "sessions/s-approve-1/activities/act-2",
                    "id": "act-2",
                    "type": "planGenerated",
                    "planId": "plan-app-1",
                    "createTime": "2026-10-07T12:01:00Z",
                    "plan": {"id": "plan-app-1", "steps": [{"title": "Step 1"}]},
                },
            ]
        }
        activities_approve_2 = {
            "activities": [
                {
                    "name": "sessions/s-approve-1/activities/act-1",
                    "id": "act-1",
                    "type": "userMessage",
                    "originator": "USER",
                    "createTime": "2026-10-07T12:00:00Z",
                    "text": "Hello",
                },
                {
                    "name": "sessions/s-approve-1/activities/act-2",
                    "id": "act-2",
                    "type": "planGenerated",
                    "planId": "plan-app-1",
                    "createTime": "2026-10-07T12:01:00Z",
                    "plan": {"id": "plan-app-1", "steps": [{"title": "Step 1"}]},
                },
                {
                    "name": "sessions/s-approve-1/activities/act-3",
                    "id": "act-3",
                    "type": "planApproved",
                    "planId": "plan-app-1",
                    "createTime": "2026-10-07T12:02:00Z",
                },
            ]
        }
        plan_content = {"id": "plan-app-1", "steps": [{"title": "Step 1"}]}
        plan_hash = canonical_hash(plan_content)
        approve_action = {
            "id": "act-approve-1",
            "op": "plans.approve",
            "enabled": True,
            "target": "sessions/s-approve-1",
            "payload": {
                "plan_id": "plan-app-1",
            },
            "operation_id": "op-approve-1",
            "authorization_ref": "grant-approve-1",
            "preconditions": {
                "latest_plan_id": "plan-app-1",
                "latest_plan_hash": plan_hash,
                "state": "PLANNING",
                "branch": "feature/integ",
            },
        }
        approve_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-approve-01",
            "profile": "default",
            "execution": {"mode": "mutation"},
            "scope": {"repository": "OWNER/REPO", "branch": "feature/integ", "sessions": ["sessions/s-approve-1"]},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [approve_action],
            "output": {"format": "json"},
        }
        approve_plan["plan_hash"] = compute_plan_hash(approve_plan)

        transport_approve = FixtureTransport(responses={
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=json.dumps(sources_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-approve-1"): TransportOutcome(status=200, body=json.dumps(session_approve).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-approve-1/activities"): [
                TransportOutcome(status=200, body=json.dumps(activities_approve_1).encode("utf-8")),
                TransportOutcome(status=200, body=json.dumps(activities_approve_1).encode("utf-8")),
                TransportOutcome(status=200, body=json.dumps(activities_approve_2).encode("utf-8")),
            ],
            ("POST", "/v1alpha/sessions/s-approve-1:approvePlan"): TransportOutcome(status=200, body=b"{}"),
        })
        client_approve = JulesClient(transport=transport_approve, clock=self.clock)
        read_service_approve = ReadService(api=client_approve)

        binding_approve = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/integ",
            session="sessions/s-approve-1",
        )
        insp_approve = read_service_approve.inspect(binding_approve)
        insp_ctx = _inspection_to_material_context(insp_approve)

        prep_approve = prepare_action(
            approve_action,
            approve_plan,
            current_profile_epoch=1,
            read_service=read_service_approve,
            context=insp_ctx,
            binding=binding_approve,
        )
        grant_approve = VerifiedGrant(
            action=prep_approve.action,
            operation_id=prep_approve.operation_id,
            profile=prep_approve.binding.profile,
            profile_epoch=prep_approve.binding.profile_epoch,
            source=prep_approve.binding.source,
            repository=prep_approve.binding.repository,
            branch=prep_approve.binding.starting_branch,
            session=prep_approve.binding.session,
            payload_hash=prep_approve.payload_hash,
            context_hash=prep_approve.context_hash,
            plan_hash=prep_approve.plan_hash,
            publication_scope=prep_approve.publication_scope,
            authorizing_source="test_integration",
        )
        verifier_approve = FakeGrantVerifier(grants={"grant-approve-1": grant_approve})
        journal_approve = Journal(store=self.store, verifier=verifier_approve, fence=self.fence, clock=self.clock)
        client_approve.ticket_authority = journal_approve

        handlers_approve = build_handler_registry(
            mode="mutation",
            store=self.store,
            read_service=read_service_approve,
            clock=self.clock,
            journal=journal_approve,
            verifier=verifier_approve,
            transport=transport_approve,
            fence=self.fence,
            api=client_approve,
        )

        res_approve = run_plan(
            approve_plan,
            handlers=handlers_approve,
            store=self.store,
            read_service=read_service_approve,
            journal=journal_approve,
            verifier=verifier_approve,
            clock=self.clock,
            transport=transport_approve,
        )
        self.assertEqual(res_approve["exit_code"], EXIT_OK)
        approve_posts = sum(1 for c in transport_approve.calls if c["method"] == "POST")
        self.assertLessEqual(approve_posts, 1)

    def test_s14_t03_crash_restart_unknown_reconciliation(self) -> None:
        """S14-T03: Simulated crash leaves operation UNKNOWN; restart reconciles without resending."""
        binding = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/integ",
            session="sessions/s-crash-1",
        )
        op_rec = OperationRecord(
            operation_id="op-crash-01",
            state=OperationState.UNKNOWN,
            request_hash="sha256:dummyhash",
            binding=binding,
            evidence=(("prompt", "Hello from crashed dispatch"),),
        )
        self.store.save_operation(op_rec)

        activities_data = {
            "activities": [
                {
                    "name": "sessions/s-crash-1/activities/act-reconciled",
                    "id": "act-reconciled",
                    "type": "agentMessage",
                    "originator": "AGENT",
                    "createTime": "2026-10-07T12:05:00Z",
                    "text": "Hello from crashed dispatch",
                }
            ]
        }
        session_data = {
            "name": "sessions/s-crash-1",
            "id": "s-crash-1",
            "title": "Crash Session",
            "state": "RUNNING",
            "createTime": "2026-10-07T12:00:00Z",
            "updateTime": "2026-10-07T12:05:00Z",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/integ"},
            },
        }
        responses = {
            ("GET", "/v1alpha/sessions/s-crash-1"): TransportOutcome(
                status=200, body=json.dumps(session_data).encode("utf-8")
            ),
            ("GET", "/v1alpha/sessions/s-crash-1/activities"): TransportOutcome(
                status=200, body=json.dumps(activities_data).encode("utf-8")
            ),
        }
        transport = FixtureTransport(responses=responses)
        client = JulesClient(transport=transport, clock=self.clock)
        read_service = ReadService(api=client)

        reconcile_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-reconcile-01",
            "profile": "default",
            "execution": {"mode": "read_only"},
            "scope": {"repository": "OWNER/REPO", "sessions": ["sessions/s-crash-1"]},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [
                {
                    "id": "act-rec-1",
                    "op": "operations.reconcile",
                    "params": {"operation_id": "op-crash-01", "scans": 1},
                }
            ],
            "output": {"format": "json"},
        }
        reconcile_plan["plan_hash"] = compute_plan_hash(reconcile_plan)

        handlers = build_handler_registry(
            mode="read_only",
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            transport=transport,
            fence=self.fence,
            api=client,
        )

        res = run_plan(
            reconcile_plan,
            handlers=handlers,
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            transport=transport,
        )

        self.assertEqual(res["exit_code"], EXIT_OK)
        # Verify ZERO POST occurred during reconciliation
        posts = sum(1 for c in transport.calls if c["method"] == "POST")
        self.assertEqual(posts, 0)
        # Verify operation was updated in durable store
        updated_op = self.store.get_operation("op-crash-01")
        self.assertIsNotNone(updated_op)
        self.assertEqual(updated_op.state, OperationState.EFFECT_OBSERVED)

    def test_s14_t03_resumed_wait(self) -> None:
        """S14-T03: Resumed wait workflow completes cleanly."""
        # Setup operation record in accepted state (not yet observed)
        self.store.save_operation(
            OperationRecord(
                operation_id="op-wait-resumed",
                state=OperationState.ACCEPTED,
                request_hash="sha256:hashwait",
                effect_observed=False,
            )
        )

        wait_action = {
            "id": "act-wait-1",
            "op": "wait",
            "params": {
                "predicate": "operation_observed",
                "operation_id": "op-wait-resumed",
                "timeout_seconds": 0.1,
            },
        }
        wait_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-wait-01",
            "profile": "default",
            "execution": {"mode": "read_only"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [wait_action],
            "output": {"format": "json"},
        }
        wait_plan["plan_hash"] = compute_plan_hash(wait_plan)

        handlers_pass1 = build_handler_registry(
            mode="read_only",
            store=self.store,
            clock=self.clock,
        )

        # Pass 1: Yields status waiting with durable state stored
        res_pass1 = run_plan(
            wait_plan,
            handlers=handlers_pass1,
            store=self.store,
            clock=self.clock,
        )
        self.assertEqual(res_pass1["exit_code"], EXIT_WAITING)
        self.assertEqual(res_pass1["action_results"][0]["status"], "waiting")
        job_id = dict(res_pass1["action_results"][0]["data"]).get("job_id")
        self.assertIsNotNone(job_id)

        # External event: Operation effect observed
        self.store.transition_operation_state(
            "op-wait-resumed",
            OperationState.EFFECT_OBSERVED,
        )

        # Pass 2: Resumed wait with the same job_id now succeeds
        wait_action_resumed = {
            "id": "act-wait-1",
            "op": "wait",
            "params": {
                "predicate": "operation_observed",
                "operation_id": "op-wait-resumed",
                "timeout_seconds": 5.0,
                "job_id": job_id,
            },
        }
        wait_plan_resumed = dict(wait_plan)
        wait_plan_resumed["plan_id"] = "plan-wait-02"
        wait_plan_resumed["actions"] = [wait_action_resumed]
        wait_plan_resumed["plan_hash"] = compute_plan_hash(wait_plan_resumed)

        handlers_pass2 = build_handler_registry(
            mode="read_only",
            store=self.store,
            clock=self.clock,
        )

        res_pass2 = run_plan(
            wait_plan_resumed,
            handlers=handlers_pass2,
            store=self.store,
            clock=self.clock,
        )
        self.assertEqual(res_pass2["exit_code"], EXIT_OK)
        self.assertEqual(res_pass2["action_results"][0]["status"], "ok")
        data_pass2 = dict(res_pass2["action_results"][0]["data"])
        self.assertTrue(data_pass2.get("predicate_matched"))
        self.assertTrue(data_pass2.get("resumed"))
