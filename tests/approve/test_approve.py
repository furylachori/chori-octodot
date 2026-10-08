"""Tests for S12 Guarded Plan Approval (plans.approve).

Standard library only. Compatible with Python 3.10+.
Covers:
- S12-T01: Changed/latest plan mismatch, expired grant, wrong state, unknown state and partial history block POST.
- S12-T02: Valid fixture posts once with no invented planId/body; planApproved activity is matched by plan ID.
- S12-T03: Plan changes after final read, wrong planApproved ID or missing event remains inconclusive; do not claim atomic approval.
- S12-T04: Timeout/malformed success/restart and new operation ID cannot bypass an unresolved approval.
- S12-T05: Task scope or publication change requires a new actual authorization, not a transformed grant.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

from octodot.actions.approve import (
    PlansApproveHandler,
    _inspection_to_material_context,
)
from octodot.api import JulesClient
from octodot.authorization import (
    DisabledGrantVerifier,
    FakeGrantVerifier,
)
from octodot.contracts import (
    canonical_hash,
    compute_plan_hash,
    request_hash,
)
from octodot.errors import (
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
)
from octodot.journal import Journal
from octodot.models import (
    ActionResultStatus,
    ActivityRecord,
    Binding,
    Coverage,
    OperationRecord,
    OperationState,
    PreparedAction,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.preparation import prepare_action
from octodot.reads import ReadService
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


def _make_sample_session(
    name: str = "sessions/sess-1",
    state: str = "PLANNING",
    require_plan_approval: bool = True,
    repo: str = "OWNER/REPO",
    branch: str = "feature/example",
) -> SessionRecord:
    owner, repo_name = repo.split("/", 1) if "/" in repo else ("OWNER", "REPO")
    return SessionRecord(
        name=name,
        state=state,
        title="Sample task",
        require_plan_approval=require_plan_approval,
        source_context=(
            ("source", f"sources/github/{repo}"),
            ("repository", repo),
            (
                "githubRepoContext",
                {"startingBranch": branch},
            ),
            (
                "githubRepo",
                {"owner": owner, "repo": repo_name},
            ),
        ),
    )


def _make_plan_generated_activity(
    session: str = "sessions/sess-1",
    act_id: str = "act-plan-1",
    plan_id: str = "plan-alpha",
    content: str = "Proposed plan steps",
    create_time: str = "2026-10-07T11:00:00Z",
) -> ActivityRecord:
    return ActivityRecord(
        name=f"{session}/activities/{act_id}",
        activity_type="PLAN_GENERATED",
        create_time=create_time,
        unknown_fields=(
            ("planId", plan_id),
            ("plan", {"steps": [content]}),
            ("planContent", content),
        ),
    )


def _make_plan_approved_activity(
    session: str = "sessions/sess-1",
    act_id: str = "act-app-1",
    plan_id: str = "plan-alpha",
    create_time: str = "2026-10-07T12:01:00Z",
) -> ActivityRecord:
    return ActivityRecord(
        name=f"{session}/activities/{act_id}",
        activity_type="PLAN_APPROVED",
        create_time=create_time,
        unknown_fields=(
            ("planId", plan_id),
        ),
    )


def _make_plan_dict(
    plan_id: str = "plan-approve-test",
    repo: str = "OWNER/REPO",
    branch: str = "feature/example",
    session: str = "sessions/sess-1",
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    acts = actions or [
        {
            "id": "act-approve-1",
            "op": "plans.approve",
            "enabled": True,
            "operation_id": "op-approve-1",
            "authorization_ref": "grant-ref-1",
            "target": session,
            "payload": {
                "plan_id": "plan-alpha",
            },
            "preconditions": {},
        }
    ]
    plan: dict[str, Any] = {
        "schema_version": "jules-controller.plan.v1",
        "plan_id": plan_id,
        "profile": "default",
        "execution": {"mode": "mutation"},
        "scope": {
            "repository": repo,
            "branch": branch,
            "sessions": [session],
        },
        "limits": {
            "deadline_seconds": 180,
            "request_timeout_seconds": 20,
            "max_http_requests": 120,
            "max_posts": 1,
            "max_pages": 100,
            "max_sessions": 200,
            "max_response_bytes": 8388608,
            "max_total_bytes": 33554432,
            "max_output_bytes": 65536,
        },
        "actions": acts,
        "output": {"format": "json"},
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


class BaseApproveTestCase(unittest.TestCase):
    """Base test fixture setting up clean SQLite store, fake clock, and transport."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_state.db")
        self.store = SQLiteStore(self.db_path)
        self.clock = FakeClock(datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc))
        self.fence = InMemoryRecoveryFence()
        self.fence.set_epoch("default", 1)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _setup_environment(
        self,
        session_record: SessionRecord | None = None,
        initial_activities: Sequence[ActivityRecord] = (),
        post_activities: Sequence[ActivityRecord] | None = None,
        post_response: TransportOutcome | None = None,
        grant: VerifiedGrant | None = None,
        verifier: Any = None,
    ) -> tuple[PlansApproveHandler, dict[str, Any], FixtureTransport]:
        session = session_record or _make_sample_session()
        target_path = f"/v1alpha/{session.name}:approvePlan"

        # Responses dictionary for FixtureTransport
        sess_outcome = TransportOutcome(
            status=200,
            body=json.dumps({
                "name": session.name,
                "state": session.state,
                "title": session.title,
                "requirePlanApproval": session.require_plan_approval,
                "sourceContext": dict(session.source_context),
            }).encode("utf-8"),
        )

        acts_json_initial = {
            "activities": [
                {
                    "name": a.name,
                    "type": a.activity_type,
                    "createTime": a.create_time,
                    **dict(a.unknown_fields),
                }
                for a in initial_activities
            ]
        }
        acts_outcome_initial = TransportOutcome(
            status=200,
            body=json.dumps(acts_json_initial).encode("utf-8"),
        )

        acts_list = [acts_outcome_initial]
        if post_activities is not None:
            acts_json_post = {
                "activities": [
                    {
                        "name": a.name,
                        "type": a.activity_type,
                        "createTime": a.create_time,
                        **dict(a.unknown_fields),
                    }
                    for a in post_activities
                ]
            }
            acts_outcome_post = TransportOutcome(
                status=200,
                body=json.dumps(acts_json_post).encode("utf-8"),
            )
            # Subsequent activity reads will return the post-activities outcome
            acts_list.append(acts_outcome_post)

        outcome_post = post_response or TransportOutcome(status=200, body=b"{}")

        responses: dict[tuple[str, str], TransportOutcome | Sequence[TransportOutcome]] = {
            ("GET", f"/v1alpha/{session.name}"): sess_outcome,
            ("GET", f"/v1alpha/{session.name}/activities"): acts_list,
            ("POST", target_path): outcome_post,
            ("GET", "/v1alpha/sources"): TransportOutcome(
                status=200,
                body=json.dumps({"sources": [{
                    "name": "sources/github/OWNER/REPO",
                    "github_repo_owner": "OWNER",
                    "github_repo_name": "REPO",
                }]}).encode("utf-8"),
            ),
        }

        active_verifier = verifier
        if active_verifier is None:
            if grant is not None:
                active_verifier = FakeGrantVerifier(
                    grants={"grant-ref-1": grant},
                    clock=self.clock,
                )
            else:
                active_verifier = DisabledGrantVerifier()

        transport = FixtureTransport(responses=responses)
        journal = Journal(store=self.store, verifier=active_verifier, clock=self.clock, fence=self.fence)
        client = JulesClient(transport=transport, ticket_authority=journal, clock=self.clock)
        read_service = ReadService(api=client, store=self.store, clock=self.clock)

        handler = PlansApproveHandler(
            api=client,
            store=self.store,
            journal=journal,
            read_service=read_service,
            clock=self.clock,
            fence=self.fence,
            verifier=active_verifier,
        )

        plan = _make_plan_dict(session=session.name)
        context = {
            "plan": plan,
            "api": client,
            "read_service": read_service,
            "store": self.store,
            "journal": journal,
            "clock": self.clock,
            "fence": self.fence,
            "verifier": active_verifier,
            "grant": grant,
            "profile": "default",
            "profile_epoch": 1,
        }

        return handler, context, transport

    def _make_matching_grant(
        self,
        plan_dict: dict[str, Any],
        action: dict[str, Any],
        read_service: ReadService,
        inspection_context: Any,
        session_name: str = "sessions/sess-1",
        authorizing_source: str = "authority-signer-1",
        expiry: str | None = None,
        publication_scope: str = "none",
        repository: str = "OWNER/REPO",
        branch: str = "feature/example",
    ) -> VerifiedGrant:
        binding = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository=repository,
            starting_branch=branch,
            session=session_name,
        )
        req_h = request_hash({"target": f"/v1alpha/{session_name}:approvePlan", "body": {}})
        prepared = prepare_action(
            action=action,
            plan=plan_dict,
            current_profile_epoch=1,
            read_service=read_service,
            context=_inspection_to_material_context(inspection_context),
            binding=binding,
            source="sources/github/OWNER/REPO",
            publication_scope=publication_scope,
            request_hash_override=req_h,
        )
        return VerifiedGrant(
            action=action["id"],
            operation_id=action["operation_id"],
            profile="default",
            profile_epoch=1,
            source="sources/github/OWNER/REPO",
            repository=repository,
            branch=branch,
            payload_hash=prepared.payload_hash,
            context_hash=prepared.context_hash,
            plan_hash=prepared.plan_hash,
            publication_scope=publication_scope,
            authorizing_source=authorizing_source,
            session=session_name,
            expiry=expiry,
            max_attempts=1,
        )


class TestS12T01GuardedPreflightAndBlockers(BaseApproveTestCase):
    """S12-T01: Changed/latest plan mismatch, expired grant, wrong state, unknown state and partial history block POST."""

    def test_s12_t01_plan_id_mismatch_blocks_post(self) -> None:
        """S12-T01: Payload specifying non-matching expected plan_id blocks before dispatch with 0 POST calls."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        # Action expects plan-beta, but session has plan-alpha
        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]
        action["payload"] = {"plan_id": "plan-beta"}

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)
        self.assertFalse(result.data_dict["api_accepted"])

        # Zero POST calls issued
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_plan_hash_mismatch_blocks_post(self) -> None:
        """S12-T01: Precondition specifying different plan_hash blocks before dispatch with 0 POST calls."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha", content="Original content")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]
        action["preconditions"] = {"plan_hash": "sha256:differenthash999"}

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_expired_grant_blocks_post(self) -> None:
        """S12-T01: Expired grant fails verification before dispatch with 0 POST calls."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
        )

        # Build grant with expiry in the past
        read_service = context["read_service"]
        insp = read_service.inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        expired_grant = self._make_matching_grant(
            plan_dict, action, read_service, insp,
            expiry="2026-10-07T11:50:00Z",  # Clock is at 12:00:00Z
        )
        verifier = FakeGrantVerifier(grants={"grant-ref-1": expired_grant}, clock=self.clock)
        context["verifier"] = verifier
        context["grant"] = expired_grant

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.GRANT_EXPIRED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_wrong_state_not_waiting_approval_blocks_post(self) -> None:
        """S12-T01: Session in wrong state (e.g. COMPLETED or IN_PROGRESS) blocks dispatch with 0 POST calls."""
        session_completed = _make_sample_session(state="COMPLETED")
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        handler, context, transport = self._setup_environment(
            session_record=session_completed,
            initial_activities=[plan_act],
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_already_approved_plan_blocks_post(self) -> None:
        """S12-T01: Proposed plan that is already marked approved blocks dispatch with 0 POST calls."""
        session = _make_sample_session(state="PLANNING")
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")
        app_act = _make_plan_approved_activity(plan_id="plan-alpha", create_time="2026-10-07T11:30:00Z")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act, app_act],
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_unknown_state_blocks_post(self) -> None:
        """S12-T01: Unknown session state blocks dispatch with 0 POST calls and UNKNOWN_STATE."""
        session_unknown = _make_sample_session(state="MYSTERY_REMOTE_STATE")
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        handler, context, transport = self._setup_environment(
            session_record=session_unknown,
            initial_activities=[plan_act],
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.UNKNOWN_STATE)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_partial_history_blocks_post(self) -> None:
        """S12-T01: Incomplete read coverage / partial history blocks dispatch with PARTIAL_COVERAGE."""
        session = _make_sample_session()

        # Mock ReadService that returns partial coverage
        class PartialCoverageReadService:
            def inspect(self, binding: Binding, fresh: bool = True) -> Any:
                class PartialInspection(dict):
                    def __init__(self) -> None:
                        super().__init__()
                        self.coverage = Coverage(
                            complete=False,
                            snapshot_atomic=False,
                            reasons=("page_cap_reached",),
                        )
                        self.candidate_bundle = None
                        self.lifecycle = None
                        self.session = session
                        self.latest_plan_id = "plan-alpha"
                        self.latest_plan_hash = "sha256:hash"
                        self.activities = ()
                        self.binding = binding
                return PartialInspection()

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[],
        )
        context["read_service"] = PartialCoverageReadService()

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.PARTIAL_COVERAGE)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t01_disabled_grant_verifier_default_blocks_post(self) -> None:
        """S12-T01: DisabledGrantVerifier is default and blocks automated dispatch."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            verifier=DisabledGrantVerifier(),
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.VERIFIER_UNAVAILABLE)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

        # Also verify that when using a verifier without the grant registered, fails with GRANT_MISSING
        fake_verifier = FakeGrantVerifier(grants={}, clock=self.clock)
        handler_missing, ctx_missing, _ = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            verifier=fake_verifier,
        )
        result_missing = handler_missing.execute(action, ctx_missing)
        self.assertEqual(result_missing.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result_missing.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result_missing.error_code, ErrorCode.GRANT_MISSING)


class TestS12T02ValidApprovalAndEventMatching(BaseApproveTestCase):
    """S12-T02: Valid fixture posts once with no invented planId/body; planApproved activity is matched by plan ID."""

    def test_s12_t02_valid_fixture_posts_once_no_invented_body(self) -> None:
        """S12-T02: Valid approval posts once with empty body {}; planApproved activity is matched by plan ID."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")
        app_act = _make_plan_approved_activity(plan_id="plan-alpha", create_time="2026-10-07T12:01:00Z")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        # First pass to inspect and compute exact matching grant
        temp_handler, temp_ctx, _ = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
        )
        read_service = temp_ctx["read_service"]
        insp = read_service.inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, read_service, insp)

        # Full environment with matching grant and post activities
        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            post_activities=[plan_act, app_act],
            grant=valid_grant,
        )

        result = handler.execute(action, context)

        # Outcome assertions
        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIsNone(result.error_code)

        data = result.data_dict
        self.assertTrue(data["api_accepted"])
        self.assertTrue(data["effect_observed"])
        self.assertEqual(data["attribution"], "inferred:matching_plan_approved_event")
        self.assertFalse(data["ui_verified"])
        self.assertFalse(data["inconclusive"])
        self.assertFalse(data["atomic_approval_claimed"])
        self.assertEqual(data["plan_id"], "plan-alpha")

        # Exactly 1 POST call
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        post_call = post_calls[0]
        self.assertEqual(post_call["path"], f"/v1alpha/{session.name}:approvePlan")
        # Empty body only - no invented planId in the HTTP body!
        self.assertEqual(post_call["body"], b"{}")

        # Operation record updated in store
        op_rec = self.store.get_operation(action["operation_id"])
        self.assertIsNotNone(op_rec)
        self.assertEqual(op_rec.state, OperationState.EFFECT_OBSERVED)
        self.assertTrue(op_rec.effect_observed)

    def test_s12_t02_replay_same_operation_returns_recorded_without_second_post(self) -> None:
        """S12-T02: Replaying the same operation returns recorded outcome with 0 additional POST calls."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")
        app_act = _make_plan_approved_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        temp_handler, temp_ctx, _ = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
        )
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            post_activities=[plan_act, app_act],
            grant=valid_grant,
        )

        res1 = handler.execute(action, context)
        self.assertEqual(res1.status, ActionResultStatus.OK)
        post_count_1 = len([c for c in transport.calls if c["method"] == "POST"])
        self.assertEqual(post_count_1, 1)

        # Replay same action
        res2 = handler.execute(action, context)
        self.assertEqual(res2.status, ActionResultStatus.OK)
        post_count_2 = len([c for c in transport.calls if c["method"] == "POST"])
        self.assertEqual(post_count_2, 1)  # No second POST!


class TestS12T03InconclusiveOutcomesAndAtomicDisapproval(BaseApproveTestCase):
    """S12-T03: Plan changes after final read, wrong planApproved ID or missing event remains inconclusive; do not claim atomic approval."""

    def test_s12_t03_plan_changes_after_final_read_inconclusive(self) -> None:
        """S12-T03: If a new plan appears after dispatch, result is inconclusive (status partial, exit 5)."""
        session = _make_sample_session()
        plan_act_1 = _make_plan_generated_activity(plan_id="plan-alpha", create_time="2026-10-07T11:00:00Z")
        app_act_1 = _make_plan_approved_activity(plan_id="plan-alpha", create_time="2026-10-07T12:01:00Z")
        # Plan changes after read!
        plan_act_2 = _make_plan_generated_activity(plan_id="plan-beta", create_time="2026-10-07T12:02:00Z")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act_1])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act_1],
            post_activities=[plan_act_1, app_act_1, plan_act_2],
            grant=valid_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.PARTIAL)
        self.assertEqual(result.exit_code, EXIT_PARTIAL_OR_UNSUPPORTED)

        data = result.data_dict
        self.assertTrue(data["api_accepted"])
        self.assertFalse(data["effect_observed"])
        self.assertTrue(data["inconclusive"])
        self.assertEqual(data["attribution"], "inconclusive:plan_changed_after_read")
        self.assertFalse(data["atomic_approval_claimed"])

        # Exactly 1 POST occurred
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

    def test_s12_t03_wrong_plan_approved_id_inconclusive(self) -> None:
        """S12-T03: Observed planApproved activity with different planId remains inconclusive."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")
        wrong_app_act = _make_plan_approved_activity(plan_id="plan-different-999")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            post_activities=[plan_act, wrong_app_act],
            grant=valid_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.PARTIAL)
        self.assertEqual(result.exit_code, EXIT_PARTIAL_OR_UNSUPPORTED)

        data = result.data_dict
        self.assertTrue(data["api_accepted"])
        self.assertFalse(data["effect_observed"])
        self.assertTrue(data["inconclusive"])
        self.assertEqual(data["attribution"], "inconclusive:wrong_plan_approved_id")
        self.assertFalse(data["atomic_approval_claimed"])

    def test_s12_t03_missing_plan_approved_event_inconclusive(self) -> None:
        """S12-T03: Missing planApproved activity after POST remains inconclusive."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            post_activities=[plan_act],  # No planApproved event returned
            grant=valid_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.PARTIAL)
        self.assertEqual(result.exit_code, EXIT_PARTIAL_OR_UNSUPPORTED)

        data = result.data_dict
        self.assertTrue(data["api_accepted"])
        self.assertFalse(data["effect_observed"])
        self.assertTrue(data["inconclusive"])
        self.assertEqual(data["attribution"], "inconclusive:missing_plan_approved_event")
        self.assertFalse(data["atomic_approval_claimed"])

    def test_s12_t03_user_requires_atomic_plan_approval_unsupported(self) -> None:
        """S12-T03: If user requires atomic exact-plan approval => unsupported_atomic_plan_approval with 0 POST calls."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]
        action["preconditions"] = {"require_atomic": True}

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.UNSUPPORTED)
        self.assertEqual(result.exit_code, EXIT_PARTIAL_OR_UNSUPPORTED)
        self.assertEqual(result.error_code, ErrorCode.UNSUPPORTED_ATOMIC_PLAN_APPROVAL)
        self.assertFalse(result.data_dict["api_accepted"])
        self.assertFalse(result.data_dict["atomic_approval_claimed"])

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)


class TestS12T04UnresolvedApprovalGatingAndNoBypass(BaseApproveTestCase):
    """S12-T04: Timeout/malformed success/restart and new operation ID cannot bypass an unresolved approval."""

    def test_s12_t04_timeout_leads_to_unknown_and_blocks_retry(self) -> None:
        """S12-T04: Timeout yields unknown state; never automatically retried; same ID replay does not re-post."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        # POST experiences timeout
        timeout_outcome = TransportOutcome(
            status=0,
            uncertain_effect=True,
            sanitized_error_code=ErrorCode.TIMEOUT,
        )

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            post_response=timeout_outcome,
            grant=valid_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.TIMEOUT)
        self.assertFalse(result.data_dict["api_accepted"])

        op_rec = self.store.get_operation(action["operation_id"])
        self.assertIsNotNone(op_rec)
        self.assertEqual(op_rec.state, OperationState.UNKNOWN)

        # Second execute call with same action cannot issue another POST
        res2 = handler.execute(action, context)
        self.assertEqual(res2.status, ActionResultStatus.UNKNOWN)
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)  # Stays at 1, no second POST!

    def test_s12_t04_malformed_success_leads_to_unknown(self) -> None:
        """S12-T04: Malformed success response marks operation UNKNOWN."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        valid_grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        malformed_outcome = TransportOutcome(
            status=200,
            body=b"not valid json {{{",
            uncertain_effect=True,
            sanitized_error_code=ErrorCode.MALFORMED_RESPONSE,
        )

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            post_response=malformed_outcome,
            grant=valid_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)

        op_rec = self.store.get_operation(action["operation_id"])
        self.assertEqual(op_rec.state, OperationState.UNKNOWN)

    def test_s12_t04_new_operation_id_cannot_bypass_unresolved_approval(self) -> None:
        """S12-T04: A new operation ID cannot bypass an unresolved same-session approval."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        # Record operation 1 in UNKNOWN state for this session in the store
        op1 = OperationRecord(
            operation_id="op-approve-earlier",
            state=OperationState.UNKNOWN,
            request_hash="sha256:hash1",
            binding=Binding(
                profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
                repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
            ),
            error_code=ErrorCode.TIMEOUT,
        )
        self.store.save_operation(op1, fence=self.fence)

        # Attempt to run a NEW operation ID on the same session
        plan_dict = _make_plan_dict()
        action2 = plan_dict["actions"][0]
        action2["operation_id"] = "op-approve-new-id"

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        grant2 = self._make_matching_grant(plan_dict, action2, temp_ctx["read_service"], insp)

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            grant=grant2,
        )

        result = handler.execute(action2, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.OPERATION_CONFLICT)

        # Zero POST calls for the new operation ID!
        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)


class TestS12T05ScopeDriftAndPublicationBoundary(BaseApproveTestCase):
    """S12-T05: Task scope or publication change requires a new actual authorization, not a transformed grant."""

    def test_s12_t05_repository_scope_drift_blocks_post(self) -> None:
        """S12-T05: Repository mismatch between grant and action fails verification with 0 POST calls."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict(repo="OWNER/REPO")
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        # Grant was issued for OWNER/OTHER_REPO
        drifted_grant = self._make_matching_grant(
            plan_dict, action, temp_ctx["read_service"], insp,
            repository="OWNER/OTHER_REPO",
        )

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            grant=drifted_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.GRANT_INVALID)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t05_branch_case_drift_blocks_post(self) -> None:
        """S12-T05: Branch case mismatch (Feature/Example vs feature/example) fails closed with 0 POST calls."""
        session = _make_sample_session(branch="feature/example")
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict(branch="feature/example")
        action = plan_dict["actions"][0]

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        # Grant was issued for Feature/Example with different casing
        cased_grant = self._make_matching_grant(
            plan_dict, action, temp_ctx["read_service"], insp,
            branch="Feature/Example",
        )

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            grant=cased_grant,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.GRANT_INVALID)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s12_t05_publication_scope_change_blocks_post(self) -> None:
        """S12-T05: Changing publication scope from none to pr requires new authorization; fails closed."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")

        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]
        # Action requests publication_scope="pr"
        action["publication_scope"] = "pr"

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        # Grant has publication_scope="none"
        grant_none = self._make_matching_grant(
            plan_dict, action, temp_ctx["read_service"], insp,
            publication_scope="none",
        )

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            grant=grant_none,
        )

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.GRANT_INVALID)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_fr4_approve_post_dispatch_transport_exception_returns_unknown(self) -> None:
        """FR4: Transport/network error after dispatch in approve records uncertain outcome and returns UNKNOWN."""
        session = _make_sample_session()
        plan_act = _make_plan_generated_activity(plan_id="plan-alpha")
        plan_dict = _make_plan_dict()
        action = plan_dict["actions"][0]
        action["operation_id"] = "op-fr4-approve-crash"
        action["authorization_ref"] = "grant-ref-1"

        temp_h, temp_ctx, _ = self._setup_environment(session_record=session, initial_activities=[plan_act])
        insp = temp_ctx["read_service"].inspect(Binding(
            profile="default", profile_epoch=1, source="sources/github/OWNER/REPO",
            repository="OWNER/REPO", starting_branch="feature/example", session=session.name,
        ))
        grant = self._make_matching_grant(plan_dict, action, temp_ctx["read_service"], insp)

        handler, context, transport = self._setup_environment(
            session_record=session,
            initial_activities=[plan_act],
            grant=grant,
        )
        orig_request = transport.request
        def crash_on_post(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            if method == "POST":
                raise RuntimeError("Network reset during plan approval")
            return orig_request(method, path, *args, **kwargs)
        transport.request = crash_on_post

        result = handler.execute(action, context)
        self.assertEqual(result.status, ActionResultStatus.UNKNOWN)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertFalse(result.data_dict["api_accepted"])

        rec = self.store.get_operation("op-fr4-approve-crash")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OperationState.UNKNOWN)
        self.assertFalse(rec.api_accepted)
        self.assertEqual(rec.error_code, ErrorCode.TRANSPORT_ERROR)


if __name__ == "__main__":
    unittest.main()
