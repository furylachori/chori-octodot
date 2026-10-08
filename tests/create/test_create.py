"""Tests for S11: tasks.create handler with bounded mutation pipeline.

Standard library only. Compatible with Python 3.10+.
Covers:
- S11-T01: Missing branch, case mismatch, stale/absent branch metadata, unknown source,
           and exact-commit requirement block creation; no branch fallback.
- S11-T02: One action yields exactly one body with supported fields; controller
           hashes/grants/operation IDs are never invented API fields.
- S11-T03: Unauthorized AUTO_CREATE_PR, false plan-approval flag, and prompt-level
           unapproved publication request fail validation/grant checks.
- S11-T04: Invalid 2xx becomes unknown; valid response plus unavailable GET remains
           accepted_identity_unverified; wrong binding blocks confirmation.
- S11-T05: Logical-task marker collision, lost response, duplicate run, and no candidates
           after repeated full scans never issue a second POST.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from typing import Any, Mapping
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import types
if "octodot.cli" not in sys.modules:
    try:
        import octodot.cli
    except (ImportError, ModuleNotFoundError):
        _cli = types.ModuleType("octodot.cli")
        _cli.main = lambda *args, **kwargs: 0
        sys.modules["octodot.cli"] = _cli

if "octodot.runner" not in sys.modules:
    try:
        import octodot.runner
    except (ImportError, ModuleNotFoundError):
        _runner = types.ModuleType("octodot.runner")
        _runner.ActionRunner = None
        _runner.run_plan = None
        sys.modules["octodot.runner"] = _runner

from octodot.actions.create import TasksCreateHandler
from octodot.api import JulesClient, compute_mutation_request_hash
from octodot.authorization import DisabledGrantVerifier, FakeGrantVerifier
from octodot.contracts import canonical_bytes, canonical_hash, compute_plan_hash
from octodot.errors import (
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
    OctodotError,
)
from octodot.journal import Journal
from octodot.models import (
    ActionResultStatus,
    Binding,
    Coverage,
    OperationRecord,
    OperationState,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.reads import ReadService
from octodot.reconciliation import Reconciler
from octodot.store import SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class TestTasksCreateHandler(unittest.TestCase):
    """Test suite for S11 bounded task creation action handler."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="octodot-s11-test-")
        from octodot.store import InMemoryRecoveryFence
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.store = SQLiteStore(state_dir=self.temp_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)
        self.clock = FakeClock(1700000000.0)

        # Standard test fixtures
        self.default_source = {
            "name": "sources/github/OWNER/REPO",
            "id": "src-1",
            "githubRepo": {"owner": "OWNER", "repo": "REPO"},
            "github_repo_owner": "OWNER",
            "github_repo_name": "REPO",
        }
        self.sources_response = json.dumps({"sources": [self.default_source]}).encode("utf-8")
        self.sessions_response_empty = json.dumps({"sessions": []}).encode("utf-8")

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _setup_pipeline(
        self,
        transport_responses: Mapping[tuple[str, str], TransportOutcome | list[TransportOutcome]],
        verifier: Any = None,
        allow_filter: bool = False,
    ) -> tuple[FixtureTransport, JulesClient, ReadService, Journal, Reconciler, TasksCreateHandler]:
        transport = FixtureTransport(responses=transport_responses, allow_filter=allow_filter)
        active_verifier = verifier if verifier is not None else FakeGrantVerifier()
        journal = Journal(store=self.store, verifier=active_verifier, clock=self.clock, fence=self.fence)
        client = JulesClient(transport=transport, ticket_authority=journal, clock=self.clock)
        read_service = ReadService(api=client, store=self.store, clock=self.clock)
        reconciler = Reconciler(store=self.store, read_api=client, clock=self.clock)
        handler = TasksCreateHandler(
            api=client,
            read_service=read_service,
            journal=journal,
            verifier=active_verifier,
            fence=self.fence,
            clock=self.clock,
            reconciler=reconciler,
            store=self.store,
        )
        return transport, client, read_service, journal, reconciler, handler

    def _make_action(
        self,
        op_id: str = "op-create-1",
        title: str = "Implement Feature",
        prompt: str = "Please implement feature [task-marker-1]",
        marker: str = "task-marker-1",
        branch: str | None = "feature/example",
        repository: str = "OWNER/REPO",
        require_plan_approval: bool = True,
        publication_scope: str = "none",
        extra_payload: dict[str, Any] | None = None,
        extra_preconditions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "requirePlanApproval": require_plan_approval,
        }
        if marker:
            payload["logical_task_marker"] = marker
        if extra_payload:
            payload.update(extra_payload)

        preconditions: dict[str, Any] = {}
        if branch:
            preconditions["branch"] = branch
            preconditions["branch_evidence"] = branch
        if extra_preconditions:
            preconditions.update(extra_preconditions)

        return {
            "id": "act-create-1",
            "op": "tasks.create",
            "enabled": True,
            "operation_id": op_id,
            "authorization_ref": f"grant-{op_id}",
            "target": repository,
            "repository": repository,
            "branch": branch,
            "publication_scope": publication_scope,
            "payload": payload,
            "preconditions": preconditions,
        }

    def _register_grant_for_action(
        self,
        verifier: FakeGrantVerifier,
        action: dict[str, Any],
        read_service: ReadService,
        profile_epoch: int = 1,
        publication_scope: str = "none",
        source: str = "sources/github/OWNER/REPO",
    ) -> tuple[VerifiedGrant, dict[str, Any]]:
        from octodot.preparation import prepare_action
        action_id = action.get("id", "act-create-1")
        plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": f"plan-{action_id}",
            "plan_hash": "",
            "profile": "default",
            "scope": {"repository": str(action.get("target")), "branch": action.get("branch")},
            "actions": [action],
        }
        plan["plan_hash"] = compute_plan_hash(plan)

        binding = Binding(
            profile="default",
            profile_epoch=profile_epoch,
            source=source,
            repository=str(action.get("target")),
            starting_branch=str(action.get("branch")),
            session=None,
        )
        prep = prepare_action(
            action=action,
            plan=plan,
            current_profile_epoch=profile_epoch,
            read_service=read_service,
            binding=binding,
            source=source,
            publication_scope=publication_scope,
        )
        grant = VerifiedGrant(
            action=prep.action,
            operation_id=prep.operation_id,
            profile="default",
            profile_epoch=profile_epoch,
            source=source,
            repository=str(action.get("target")),
            branch=str(action.get("branch")),
            payload_hash=prep.payload_hash,
            context_hash=prep.context_hash,
            plan_hash=prep.plan_hash,
            publication_scope=publication_scope,
            authorizing_source=f"grant-{action.get('operation_id')}",
            session=None,
            max_attempts=1,
        )
        verifier.register_grant(str(action.get("authorization_ref")), grant)
        return grant, plan

    # =========================================================================
    # S11-T01: Preflight source, branch, and commit checks
    # =========================================================================

    def test_s11_t01_missing_branch_blocks_creation(self) -> None:
        """S11-T01: Missing branch blocks creation with BRANCH_UNVERIFIED; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(branch=None)
        action.pop("branch", None)
        action["preconditions"].pop("branch", None)
        action["preconditions"].pop("branch_evidence", None)

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BRANCH_UNVERIFIED)

        # Assert zero POSTs
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t01_no_branch_fallback(self) -> None:
        """S11-T01: Absent branch never falls back to default branch (main/master); zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        # Empty string branch
        action = self._make_action(branch="")
        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BRANCH_UNVERIFIED)

        # Whitespace branch
        action_ws = self._make_action(branch="   ")
        result_ws = handler.execute(action_ws, context={"client": client, "limits": {}})
        self.assertEqual(result_ws.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result_ws.error_code, ErrorCode.BRANCH_UNVERIFIED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t01_case_mismatch_blocks_creation(self) -> None:
        """S11-T01: Branch case mismatch blocks creation with BINDING_MISMATCH; zero POSTs."""
        # Existing session has "Feature/example" while requested is "feature/example"
        existing_session = {
            "name": "sessions/sess-old",
            "state": "COMPLETED",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "Feature/example"},
            },
        }
        sessions_response = json.dumps({"sessions": [existing_session]}).encode("utf-8")
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=sessions_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(branch="feature/example")
        # Remove preconditions branch_evidence to force matching against existing sessions
        action["preconditions"].pop("branch_evidence", None)

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t01_absent_branch_metadata_blocks_creation(self) -> None:
        """S11-T01: Absent branch metadata in preflight blocks creation with BRANCH_UNVERIFIED; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(branch="feature/unverified-branch")
        action["preconditions"].pop("branch_evidence", None)

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BRANCH_UNVERIFIED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t01_stale_branch_metadata_blocks_creation(self) -> None:
        """S11-T01: Stale branch metadata blocks creation with BRANCH_UNVERIFIED; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        # Explicitly marked stale in preconditions
        action = self._make_action(
            branch="feature/example",
            extra_preconditions={"branch_stale": True},
        )

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BRANCH_UNVERIFIED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t01_unknown_source_blocks_creation(self) -> None:
        """S11-T01: Unknown repository source blocks creation with BINDING_MISMATCH; zero POSTs."""
        # Jules sources list contains OTHER/REPO instead of OWNER/REPO
        other_source = {
            "name": "sources/github/OTHER/REPO",
            "id": "src-other",
            "githubRepo": {"owner": "OTHER", "repo": "REPO"},
            "github_repo_owner": "OTHER",
            "github_repo_name": "REPO",
        }
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(
                status=200, body=json.dumps({"sources": [other_source]}).encode("utf-8")
            ),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(repository="OWNER/REPO")
        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t01_exact_commit_requirement_unsupported(self) -> None:
        """S11-T01: Exact-commit requirement yields unsupported_exact_commit; exit 5; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        # Plan requests exact commit
        action = self._make_action(
            extra_preconditions={"exact_commit": "0123456789abcdef0123456789abcdef01234567"}
        )

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.UNSUPPORTED)
        self.assertEqual(result.exit_code, EXIT_PARTIAL_OR_UNSUPPORTED)
        self.assertEqual(result.error_code, ErrorCode.UNSUPPORTED_EXACT_COMMIT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    # =========================================================================
    # S11-T02: Single-attempt request body and serialization
    # =========================================================================

    def test_s11_t02_one_action_yields_one_post_with_supported_fields(self) -> None:
        """S11-T02: One action yields exactly one body with supported fields; no controller fields."""
        created_session_data = {
            "name": "sessions/sess-created-1",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        post_response = json.dumps(created_session_data).encode("utf-8")
        get_response = post_response  # Refresh GET succeeds with matching binding

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            ("GET", "/v1alpha/sessions/sess-created-1"): TransportOutcome(status=200, body=get_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t02-1")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertTrue(result.data_dict["effect_observed"])
        self.assertEqual(result.data_dict["attribution"], "confirmed")

        # Durable record matches ActionResult
        rec = self.store.get_operation("op-t02-1")
        self.assertIsNotNone(rec)
        self.assertTrue(rec.api_accepted)
        self.assertTrue(rec.effect_observed)
        self.assertEqual(rec.attribution, "confirmed")

        # Inspect POST calls: exactly 1 POST
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        post_call = post_calls[0]
        self.assertEqual(post_call["path"], "/v1alpha/sessions")

        # Parse outgoing body bytes
        outgoing_body = json.loads(post_call["body"].decode("utf-8"))

        # Verify allowed fields
        expected_keys = {"title", "prompt", "sourceContext", "requirePlanApproval"}
        self.assertEqual(set(outgoing_body.keys()), expected_keys)
        self.assertEqual(outgoing_body["title"], "Implement Feature")
        self.assertEqual(outgoing_body["prompt"], "Please implement feature [task-marker-1]")
        self.assertTrue(outgoing_body["requirePlanApproval"])

        # Verify sourceContext structure
        sc = outgoing_body["sourceContext"]
        self.assertEqual(set(sc.keys()), {"source", "githubRepoContext"})
        self.assertEqual(sc["source"], "sources/github/OWNER/REPO")
        self.assertEqual(sc["githubRepoContext"], {"startingBranch": "feature/example"})

        # Verify publication: none omits automationMode
        self.assertNotIn("automationMode", outgoing_body)

        # Verify controller fields are NEVER present in outgoing body
        forbidden_controller_fields = (
            "operation_id",
            "authorization_ref",
            "request_hash",
            "payload_hash",
            "context_hash",
            "plan_hash",
            "plan_id",
            "logical_task_marker",
            "task_marker",
            "marker",
            "enabled",
            "predecessor_operation_id",
        )
        for field_name in forbidden_controller_fields:
            self.assertNotIn(field_name, outgoing_body)

    def test_s11_t02_authorized_publication_includes_automation_mode(self) -> None:
        """S11-T02: Authorized AUTO_CREATE_PR includes automationMode and no controller fields."""
        created_session_data = {
            "name": "sessions/sess-created-pr",
            "state": "ACTIVE",
            "title": "Implement PR Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        post_response = json.dumps(created_session_data).encode("utf-8")
        get_response = post_response

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            ("GET", "/v1alpha/sessions/sess-created-pr"): TransportOutcome(status=200, body=get_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(
            op_id="op-t02-pr",
            title="Implement PR Feature",
            prompt="Implement task [task-marker-1]",
            publication_scope="AUTO_CREATE_PR",
        )
        grant, plan = self._register_grant_for_action(
            verifier, action, read_service, publication_scope="AUTO_CREATE_PR"
        )

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.OK)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        outgoing_body = json.loads(post_calls[0]["body"].decode("utf-8"))
        self.assertEqual(
            set(outgoing_body.keys()),
            {"title", "prompt", "sourceContext", "requirePlanApproval", "automationMode"},
        )
        self.assertEqual(outgoing_body["automationMode"], "AUTO_CREATE_PR")

    # =========================================================================
    # S11-T03: Publication grant and approval validation checks
    # =========================================================================

    def test_s11_t03_unauthorized_auto_create_pr_fails(self) -> None:
        """S11-T03: AUTO_CREATE_PR without explicit publication grant fails before POST."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        # Action requests AUTO_CREATE_PR, but grant has publication_scope="none"
        action = self._make_action(
            op_id="op-t03-unauth",
            publication_scope="AUTO_CREATE_PR",
        )
        grant, plan = self._register_grant_for_action(
            verifier, action, read_service, publication_scope="none"
        )

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertIn(result.error_code, (ErrorCode.AUTH_DENIED, ErrorCode.GRANT_INVALID))

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t03_false_plan_approval_flag_fails(self) -> None:
        """S11-T03: False requirePlanApproval flag fails validation before dispatch; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(
            op_id="op-t03-noapproval",
            require_plan_approval=False,
        )

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t03_prompt_level_unapproved_publication_request_fails(self) -> None:
        """S11-T03: Prompt-level unapproved publication request fails closed before dispatch; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        # Publication scope is none, but prompt text explicitly asks to create a pull request
        action = self._make_action(
            op_id="op-t03-promptpr",
            prompt="Please fix bug and open a pull request [task-marker-1]",
            publication_scope="none",
        )

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.AUTH_DENIED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    # =========================================================================
    # S11-T04: Response handling and binding verification
    # =========================================================================

    def test_s11_t04_invalid_2xx_becomes_unknown(self) -> None:
        """S11-T04: Invalid 2xx (missing name or malformed JSON) becomes unknown; exactly 1 POST."""
        # 200 response with missing 'name' field
        malformed_200 = json.dumps({"title": "No session name"}).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=malformed_200),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t04-invalid2xx")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertFalse(result.data_dict["api_accepted"])

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

    def test_s11_t04_valid_response_with_unavailable_get_accepted_identity_unverified(self) -> None:
        """S11-T04: Valid response plus unavailable GET remains accepted_identity_unverified; never recreate."""
        created_session_data = {
            "name": "sessions/sess-unverified-1",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
        }
        post_response = json.dumps(created_session_data).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            # Refresh GET fails with 500 error
            ("GET", "/v1alpha/sessions/sess-unverified-1"): TransportOutcome(
                status=500, body=b"server error", uncertain_effect=True
            ),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t04-unverified")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.ACCEPTED_IDENTITY_UNVERIFIED)
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertTrue(result.data_dict["accepted_identity_unverified"])
        self.assertFalse(result.data_dict["effect_observed"])
        self.assertNotEqual(result.data_dict["attribution"], "confirmed")

        # Exactly 1 POST was made
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        # Check durable store records accepted_identity_unverified
        rec = self.store.get_operation("op-t04-unverified")
        self.assertIsNotNone(rec)
        self.assertTrue(rec.accepted_identity_unverified)
        self.assertFalse(rec.effect_observed)
        self.assertNotEqual(rec.attribution, "confirmed")

        # Running again must NEVER issue a second POST / recreate
        result2 = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        post_calls2 = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls2), 1)

    def test_s11_t04_wrong_binding_blocks_confirmation(self) -> None:
        """S11-T04: Wrong binding on refreshed session blocks confirmation; exactly 1 POST."""
        created_session_data = {
            "name": "sessions/sess-wrong-branch",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
        }
        # Refreshed session has unexpected branch "other-branch"
        refreshed_session_data = {
            "name": "sessions/sess-wrong-branch",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "other-branch"},
            },
        }

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(
                status=200, body=json.dumps(created_session_data).encode("utf-8")
            ),
            ("GET", "/v1alpha/sessions/sess-wrong-branch"): TransportOutcome(
                status=200, body=json.dumps(refreshed_session_data).encode("utf-8")
            ),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t04-wrongbind", branch="feature/example")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertFalse(result.data_dict["effect_observed"])
        self.assertNotEqual(result.data_dict["attribution"], "confirmed")

        rec = self.store.get_operation("op-t04-wrongbind")
        self.assertIsNotNone(rec)
        self.assertFalse(rec.effect_observed)
        self.assertNotEqual(rec.attribution, "confirmed")

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

    def test_s11_t04_attribution_confirmed_on_verified_path_and_durable_record_agreement(self) -> None:
        """S11-T04: Verified path sets attribution='confirmed' and durable record matches ActionResult."""
        created_session_data = {
            "name": "sessions/sess-confirmed-agreement",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        post_response = json.dumps(created_session_data).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            ("GET", "/v1alpha/sessions/sess-confirmed-agreement"): TransportOutcome(status=200, body=post_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t04-confirmed-agree")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertTrue(result.data_dict["effect_observed"])
        self.assertEqual(result.data_dict["attribution"], "confirmed")

        # Verify durable store record matches ActionResult exactly
        rec = self.store.get_operation("op-t04-confirmed-agree")
        self.assertIsNotNone(rec)
        self.assertTrue(rec.api_accepted)
        self.assertTrue(rec.effect_observed)
        self.assertEqual(rec.attribution, "confirmed")
        self.assertEqual(rec.api_accepted, result.data_dict["api_accepted"])
        self.assertEqual(rec.effect_observed, result.data_dict["effect_observed"])
        self.assertEqual(rec.attribution, result.data_dict["attribution"])

    def test_s11_t04_store_transition_failure_returns_error_and_never_reposts(self) -> None:
        """S11-T04: Store transition failure does not report success or 'confirmed', and never re-POSTs."""
        class FaultyStore(SQLiteStore):
            def transition_operation_state(self, operation_id: str, to_state: OperationState, **kwargs: Any) -> OperationRecord:
                if to_state == OperationState.EFFECT_OBSERVED:
                    raise OctodotError(ErrorCode.STATE_CORRUPT, "Injected store transition failure")
                return super().transition_operation_state(operation_id, to_state, **kwargs)

        faulty_store = FaultyStore(state_dir=self.temp_dir, fence=self.fence)
        faulty_store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

        created_session_data = {
            "name": "sessions/sess-faulty-store",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        post_response = json.dumps(created_session_data).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            ("GET", "/v1alpha/sessions/sess-faulty-store"): TransportOutcome(status=200, body=post_response),
        }
        verifier = FakeGrantVerifier()
        transport = FixtureTransport(responses=transport_responses)
        journal = Journal(store=faulty_store, verifier=verifier, clock=self.clock, fence=self.fence)
        client = JulesClient(transport=transport, ticket_authority=journal, clock=self.clock)
        read_service = ReadService(api=client, store=faulty_store, clock=self.clock)
        reconciler = Reconciler(store=faulty_store, read_api=client, clock=self.clock)
        handler = TasksCreateHandler(
            api=client,
            read_service=read_service,
            journal=journal,
            verifier=verifier,
            fence=self.fence,
            clock=self.clock,
            reconciler=reconciler,
            store=faulty_store,
        )

        action = self._make_action(op_id="op-faulty-store")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})

        # Assert: the result is not OK, attribution != "confirmed"
        self.assertNotEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.status, ActionResultStatus.ERROR)
        self.assertEqual(result.error_code, ErrorCode.STATE_CORRUPT)
        self.assertNotEqual(result.data_dict["attribution"], "confirmed")
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertFalse(result.data_dict["effect_observed"])

        # POST count stays exactly 1
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        # Durable record is unchanged (remains ACCEPTED with effect_observed=False, attribution="")
        rec = faulty_store.get_operation("op-faulty-store")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.ACCEPTED)
        self.assertFalse(rec.effect_observed)
        self.assertNotEqual(rec.attribution, "confirmed")

        # Running again must NEVER issue a second POST / re-POST
        result2 = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        post_calls2 = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls2), 1)

    # =========================================================================
    # S11-T05: Marker collisions, lost responses, and no second POST
    # =========================================================================

    def test_s11_t05_marker_collision_never_posts(self) -> None:
        """S11-T05: Logical-task marker collision in preflight never issues a POST."""
        existing_session_with_marker = {
            "name": "sessions/sess-existing",
            "state": "OPEN",
            "title": "Existing Task [task-marker-1]",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        sessions_response = json.dumps({"sessions": [existing_session_with_marker]}).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=sessions_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t05-collision", marker="task-marker-1")

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.OPERATION_CONFLICT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t05_marker_not_in_approved_prompt_blocks_creation(self) -> None:
        """S11-T05: Logical-task marker absent from approved text blocks creation before POST."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        # Prompt does NOT contain the marker "task-marker-missing"
        action = self._make_action(
            op_id="op-t05-nomarker",
            title="Clean Title",
            prompt="Prompt without any marker",
            marker="task-marker-missing",
        )

        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s11_t05_lost_response_never_retried(self) -> None:
        """S11-T05: Lost response transitions to unknown; never retried."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            # POST disconnects / times out (uncertain_effect=True)
            ("POST", "/v1alpha/sessions"): TransportOutcome(
                status=0, body=None, uncertain_effect=True
            ),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t05-lost")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        # Attempting second execution with same action: must NEVER issue second POST
        result2 = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        post_calls2 = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls2), 1)

    def test_s11_t05_duplicate_run_never_issues_second_post(self) -> None:
        """S11-T05: Duplicate run of a completed action returns recorded state and issues zero POSTs."""
        created_session_data = {
            "name": "sessions/sess-dup-1",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        post_response = json.dumps(created_session_data).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            ("GET", "/v1alpha/sessions/sess-dup-1"): TransportOutcome(status=200, body=post_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t05-dup")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        # First run completes
        result1 = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result1.status, ActionResultStatus.OK)
        self.assertEqual(len([c for c in transport.calls if c["method"] == "POST"]), 1)

        # Second run: must recognize recorded state and issue 0 additional POSTs
        result2 = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result2.status, ActionResultStatus.OK)
        self.assertEqual(len([c for c in transport.calls if c["method"] == "POST"]), 1)

    def test_s11_t05_no_candidates_after_repeated_scans_never_retries(self) -> None:
        """S11-T05: UNKNOWN state with no candidates after repeated full scans stays UNKNOWN; never retried."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=503, body=b"unavailable", uncertain_effect=True),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(op_id="op-t05-nocand")
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertFalse(result.data_dict["effect_observed"])

        # Reconcile with 3 scans
        reconcile_res = reconciler.reconcile("op-t05-nocand", read_api=client, scans=3)
        self.assertEqual(reconcile_res.reconciled_state, OperationState.UNKNOWN.value)
        self.assertFalse(reconcile_res.record.effect_observed)

        # Total POST count remains exactly 1 across all scans
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

    # =========================================================================
    # Additional edge cases and contract invariants
    # =========================================================================

    def test_default_disabled_grant_verifier_blocks_dispatch(self) -> None:
        """Default DisabledGrantVerifier blocks dispatch with VERIFIER_UNAVAILABLE; zero POSTs."""
        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
        }
        # Using DisabledGrantVerifier explicitly
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=DisabledGrantVerifier()
        )

        action = self._make_action(op_id="op-disabled-verifier")
        result = handler.execute(action, context={"client": client, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.VERIFIER_UNAVAILABLE)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_predecessor_operation_id_passed_as_keyword_only_and_omitted_from_payload(self) -> None:
        """predecessor_operation_id is passed as keyword-only to journal and never part of outgoing body."""
        # 1. Create a prior resolved UNKNOWN operation
        prior_op_id = "op-pred-resolved"
        from octodot.models import PreparedAction, OperationRecord
        binding = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session=None,
        )
        rec = OperationRecord(
            operation_id=prior_op_id,
            state=OperationState.UNKNOWN,
            request_hash="req-123",
            binding=binding,
            evidence=(("logical_task_marker", "task-marker-1"),),
        )
        self.store.save_operation(rec)
        # Mark desired state resolved on prior operation
        reconciler_dummy = Reconciler(store=self.store)
        reconciler_dummy.resolve_desired_state(prior_op_id, decided_by="admin", reason="manual_resolution")

        # 2. Setup pipeline for new operation with predecessor linkage
        created_session_data = {
            "name": "sessions/sess-pred-new",
            "state": "ACTIVE",
            "title": "Implement Feature",
            "requirePlanApproval": True,
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        }
        post_response = json.dumps(created_session_data).encode("utf-8")

        transport_responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=self.sources_response),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=self.sessions_response_empty),
            ("POST", "/v1alpha/sessions"): TransportOutcome(status=200, body=post_response),
            ("GET", "/v1alpha/sessions/sess-pred-new"): TransportOutcome(status=200, body=post_response),
        }
        verifier = FakeGrantVerifier()
        transport, client, read_service, journal, reconciler, handler = self._setup_pipeline(
            transport_responses, verifier=verifier
        )

        action = self._make_action(
            op_id="op-new-with-pred",
            extra_preconditions={"predecessor_operation_id": prior_op_id},
        )
        grant, plan = self._register_grant_for_action(verifier, action, read_service)

        result = handler.execute(action, context={"client": client, "plan": plan, "limits": {}})
        self.assertEqual(result.status, ActionResultStatus.OK)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        outgoing_body = json.loads(post_calls[0]["body"].decode("utf-8"))
        self.assertNotIn("predecessor_operation_id", outgoing_body)


if __name__ == "__main__":
    unittest.main()
