"""Action handler for guarded plan approval (plans.approve).

Standard library only. Compatible with Python 3.10+.
Follows the shared mutation pipeline:
literal action -> execution eligibility (contracts) -> fresh full rescan via ReadService
-> S04 binding/projection checks -> S05 prepare_action (hashes)
-> GrantVerifier.verify (S05; DisabledGrantVerifier is default and blocks)
-> journal gating (fence, unresolved intents, operation id/hash)
-> final session re-check -> journal.begin_dispatch
-> API mutation method (sessions_approve_plan) exactly once
-> journal.record_outcome
-> bounded read-only reconciliation (S08)
-> ActionResult with api_accepted / effect_observed / attribution / ui_verified separate.

Never claims atomic approval. Atomic exact-plan approval requirements are rejected
with unsupported_atomic_plan_approval before issuing any POST.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from typing import Any, Sequence

from octodot.authorization import (
    DisabledGrantVerifier,
    parse_grant,
)
from octodot.contracts import (
    ActionHandler,
    Clock,
    GrantBlocker,
    GrantVerifier,
    MutationJournal,
    ReadService as ReadServiceProtocol,
    RecoveryFence,
    _scan_for_placeholders,
    canonical_hash,
    compute_plan_hash,
    request_hash,
)
from octodot.errors import (
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
    OctodotError,
)
from octodot.identity import (
    extract_session_branch,
    extract_session_repository,
    validate_repository_name,
)
from octodot.journal import Journal
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ActivityRecord,
    Binding,
    Coverage,
    LifecycleBucket,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    SessionRecord,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.preparation import (
    compute_mutation_request_body,
    prepare_action,
)
from octodot.projections import (
    ActivityKind,
    AttentionReason,
    project_activity,
    project_attention,
    project_lifecycle,
    project_plan,
)
from octodot.reads import ReadService, session_to_dict
from octodot.transport import SystemClock


def _is_atomic_approval_requested(action: Mapping[str, Any]) -> bool:
    """Return True if atomic exact-plan approval is explicitly required."""
    preconditions = action.get("preconditions") or {}
    payload = action.get("payload") or {}

    candidates = (
        action.get("require_atomic"),
        action.get("require_atomic_approval"),
        action.get("require_atomic_plan_approval"),
        action.get("atomic"),
        action.get("atomic_approval"),
        action.get("atomic_plan_approval"),
        preconditions.get("require_atomic"),
        preconditions.get("require_atomic_approval"),
        preconditions.get("require_atomic_plan_approval"),
        preconditions.get("atomic"),
        preconditions.get("atomic_approval"),
        preconditions.get("atomic_plan_approval"),
        preconditions.get("exact_plan_atomic"),
        payload.get("require_atomic"),
        payload.get("require_atomic_approval"),
        payload.get("atomic"),
        payload.get("atomic_approval"),
    )
    return any(c is True for c in candidates)


def _inspection_to_material_context(inspection: Any) -> dict[str, Any]:
    """Convert SessionInspection to clean JSON-serializable material context."""
    b = getattr(inspection, "binding", None)
    b_dict = {
        "profile": b.profile,
        "profile_epoch": b.profile_epoch,
        "source": b.source,
        "repository": b.repository,
        "starting_branch": b.starting_branch,
        "session": b.session,
    } if b is not None else {}

    sess = getattr(inspection, "session", None)
    sess_dict: dict[str, Any] = {}
    if sess is not None:
        if isinstance(sess, Mapping):
            sess_dict = dict(sess)
        elif hasattr(sess, "to_dict"):
            sess_dict = sess.to_dict()
        elif isinstance(sess, SessionRecord):
            sess_dict = session_to_dict(sess)
        else:
            sess_dict = {"name": getattr(sess, "name", "")}

    cov = getattr(inspection, "coverage", None)
    cov_dict: dict[str, Any] | None = None
    if cov is not None:
        if isinstance(cov, Mapping):
            cov_dict = dict(cov)
        else:
            cov_dict = {
                "complete": bool(getattr(cov, "complete", False)),
                "reasons": list(getattr(cov, "reasons", ())),
            }

    cb = getattr(inspection, "candidate_bundle", None)
    if cb is None:
        cb = getattr(inspection, "feedback_bundle", None)
    cb_dict: dict[str, Any] | None = None
    if cb is not None:
        if isinstance(cb, Mapping):
            cb_dict = dict(cb)
        else:
            cb_dict = {
                "has_ambiguity": bool(getattr(cb, "has_ambiguity", False)),
                "ambiguity_reasons": list(getattr(cb, "ambiguity_reasons", ())),
            }

    return {
        "op": "plans.approve",
        "target": getattr(sess, "name", "") if sess is not None else "",
        "session": sess_dict,
        "state": getattr(inspection, "state", ""),
        "title": getattr(inspection, "title", None),
        "binding": b_dict,
        "latest_plan_id": getattr(inspection, "latest_plan_id", None),
        "latest_plan_hash": getattr(inspection, "latest_plan_hash", None),
        "coverage": cov_dict,
        "candidate_bundle": cb_dict,
    }


class PlansApproveHandler:
    """ActionHandler for 'plans.approve' implementing guarded plan approval.

    Enforces:
    - Preflight requires latest plan ID + content hash from S04 plan projection.
    - Session must be in waiting-for-approval state; wrong or unknown state blocks.
    - Partial history / incomplete coverage blocks before dispatch.
    - Trusted grant verification covering exact task scope and publication scope.
    - Default verifier is DisabledGrantVerifier which fails closed.
    - Single dispatch ticket redemption; transport receives session with empty body {}.
    - Read planApproved activity afterwards and match by plan ID.
    - Plan change after read, wrong plan ID, or missing event => inconclusive.
    - Never claims atomic approval; rejects atomic exact-plan approval requirements.
    - Unresolved approval blocks subsequent new operation IDs.
    """

    def __init__(
        self,
        api: Any = None,
        store: Any = None,
        verifier: GrantVerifier | None = None,
        journal: MutationJournal | None = None,
        read_service: ReadServiceProtocol | None = None,
        clock: Clock | None = None,
        fence: RecoveryFence | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.verifier = verifier
        self.journal = journal
        self.read_service = read_service
        self.clock = clock or SystemClock()
        self.fence = fence

    def can_handle(self, op: str) -> bool:
        """Return True if this handler handles the specified op."""
        return op == "plans.approve"

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Execute guarded plan approval through the complete mutation pipeline."""
        ctx = context or {}
        action_id = str(action.get("id", "act-plans-approve"))
        op = str(action.get("op", "plans.approve"))

        # -------------------------------------------------------------
        # Step 1: Literal action eligibility and atomic-approval checks
        # -------------------------------------------------------------
        if not action.get("enabled", False):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TEMPLATE_DISABLED,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Action '{action_id}' is disabled template",
                },
            )

        # Check for placeholder tokens in action
        has_placeholder, token = _scan_for_placeholders(action)
        if has_placeholder:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.PLACEHOLDER_PRESENT,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Placeholder token found in action: '{token}'",
                },
            )

        # Check if caller requires atomic exact-plan approval
        if _is_atomic_approval_requested(action):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNSUPPORTED,
                exit_code=EXIT_PARTIAL_OR_UNSUPPORTED,
                error_code=ErrorCode.UNSUPPORTED_ATOMIC_PLAN_APPROVAL,
                data={
                    "unsupported": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "atomic_approval_claimed": False,
                    "reason": "Atomic exact-plan approval is unsupported by this endpoint",
                },
            )

        # -------------------------------------------------------------
        # Step 2: Extract targets, binding information, and scope
        # -------------------------------------------------------------
        raw_target = action.get("target")
        if not raw_target or not isinstance(raw_target, str) or not raw_target.strip():
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": "Action must specify a non-empty target session",
                },
            )

        clean_target = raw_target.strip()
        session_name = (
            clean_target if clean_target.startswith("sessions/") else f"sessions/{clean_target}"
        )

        plan_dict = ctx.get("plan") or {}
        scope = plan_dict.get("scope") or ctx.get("scope") or {}
        repository = scope.get("repository") or action.get("repository") or ""
        branch = scope.get("branch") if "branch" in scope else action.get("branch")
        profile = str(ctx.get("profile") or plan_dict.get("profile") or "default")

        fence = ctx.get("fence") or self.fence
        store = ctx.get("store") or self.store
        clock = ctx.get("clock") or self.clock

        current_profile_epoch = ctx.get("profile_epoch")
        if current_profile_epoch is None:
            if fence is not None:
                current_profile_epoch = fence.get_current_epoch(profile)
            elif store is not None and hasattr(store, "get_profile_epoch"):
                current_profile_epoch = store.get_profile_epoch(profile)
            else:
                current_profile_epoch = 0

        if repository:
            try:
                validate_repository_name(repository)
            except OctodotError as err:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=err.code,
                    data={
                        "blocked": True,
                        "api_accepted": False,
                        "effect_observed": False,
                        "attribution": None,
                        "ui_verified": False,
                        "reason": str(err),
                    },
                )

        source = ctx.get("source") or (f"sources/github/{repository}" if repository else "")
        binding = Binding(
            profile=profile,
            profile_epoch=current_profile_epoch,
            source=source,
            repository=repository,
            starting_branch=branch,
            session=session_name,
        )

        # -------------------------------------------------------------
        # Step 3: Fresh full rescan via ReadService (never reused)
        # -------------------------------------------------------------
        api = ctx.get("api") or self.api
        read_service: ReadServiceProtocol | None = (
            ctx.get("read_service") or self.read_service
        )
        if read_service is None and api is not None:
            read_service = ReadService(api=api, store=store, clock=clock)

        if read_service is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": "ReadService is required for mutation preflight",
                },
            )

        try:
            inspection = read_service.inspect(binding, fresh=True)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"ReadService inspection failed: {err.message}",
                },
            )

        coverage = inspection.coverage
        if coverage is not None and not coverage.complete:
            reasons_str = f": {list(coverage.reasons)}" if coverage.reasons else ""
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.PARTIAL_COVERAGE,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Partial history / incomplete read coverage{reasons_str}",
                },
            )

        if inspection.candidate_bundle and inspection.candidate_bundle.has_ambiguity:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.IDENTITY_AMBIGUOUS,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Ambiguous history: {list(inspection.candidate_bundle.ambiguity_reasons)}",
                },
            )

        # -------------------------------------------------------------
        # Step 4: S04 Binding and projection checks
        # -------------------------------------------------------------
        lifecycle = inspection.lifecycle or project_lifecycle(inspection.session)
        if lifecycle.bucket == LifecycleBucket.UNKNOWN:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.UNKNOWN_STATE,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Session is in unknown state '{inspection.session.state}'",
                },
            )

        # Check waiting-for-approval state
        plan_proj = project_plan(inspection.activities, inspection.session)
        raw_state = inspection.session.state.strip().upper()

        is_waiting_for_approval = (
            lifecycle.bucket == LifecycleBucket.OPEN
            and (
                raw_state in ("AWAITING_PLAN_APPROVAL", "PLANNING")
                or (
                    plan_proj.requires_approval
                    and not plan_proj.is_approved
                    and plan_proj.latest_plan_id is not None
                )
            )
        )

        if not is_waiting_for_approval:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Session state '{inspection.session.state}' is not waiting for plan approval",
                },
            )

        latest_plan_id = inspection.latest_plan_id or plan_proj.latest_plan_id
        latest_plan_hash = inspection.latest_plan_hash or plan_proj.latest_plan_hash

        if latest_plan_id is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.NOT_FOUND,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": "No proposed plan found for session",
                },
            )

        if plan_proj.is_approved:
            active_store = ctx.get("store") or self.store
            if active_store is None:
                j = ctx.get("journal") or self.journal
                if j is not None and hasattr(j, "store"):
                    active_store = j.store
            op_id = str(action.get("operation_id", ""))
            existing_op = None
            if active_store is not None and op_id:
                try:
                    existing_op = active_store.get_operation(op_id)
                except Exception:
                    existing_op = None
            if existing_op is None or existing_op.state not in (
                OperationState.ACCEPTED,
                OperationState.EFFECT_OBSERVED,
            ):
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.INVALID_INPUT,
                    coverage=coverage,
                    data={
                        "blocked": True,
                        "api_accepted": False,
                        "effect_observed": False,
                        "attribution": None,
                        "ui_verified": False,
                        "reason": f"Plan '{latest_plan_id}' is already approved",
                    },
                )

        # Check payload/preconditions match latest plan
        payload = action.get("payload") or {}
        preconditions = action.get("preconditions") or {}

        expected_plan_id = payload.get("plan_id") or preconditions.get("plan_id")
        if expected_plan_id and str(expected_plan_id).strip() != latest_plan_id:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BINDING_MISMATCH,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": (
                        f"Expected plan ID '{expected_plan_id}' does not match "
                        f"latest plan ID '{latest_plan_id}'"
                    ),
                },
            )

        expected_plan_hash = (
            payload.get("plan_hash")
            or payload.get("content_hash")
            or preconditions.get("plan_hash")
            or preconditions.get("content_hash")
        )
        if expected_plan_hash and expected_plan_hash != latest_plan_hash:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BINDING_MISMATCH,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": (
                        f"Expected plan hash '{expected_plan_hash}' does not match "
                        f"latest plan hash '{latest_plan_hash}'"
                    ),
                },
            )

        # -------------------------------------------------------------
        # Step 5: S05 prepare_action (canonical hashes)
        # -------------------------------------------------------------
        # Primary target for endpoint is /v1alpha/{session}:approvePlan
        req_hash_override = ctx.get("request_hash") or request_hash(
            {"target": f"/v1alpha/{session_name}:approvePlan", "body": {}}
        )

        try:
            prepared_action = prepare_action(
                action=action,
                plan=plan_dict,
                current_profile_epoch=current_profile_epoch,
                read_service=read_service,
                context=_inspection_to_material_context(inspection),
                binding=binding,
                source=inspection.binding.source if inspection.binding else source,
                publication_scope=action.get("publication_scope", "none"),
                request_hash_override=req_hash_override,
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": str(err),
                },
            )

        # -------------------------------------------------------------
        # Step 6: GrantVerifier.verify (trusted grant boundary)
        # -------------------------------------------------------------
        verifier: GrantVerifier = ctx.get("verifier") or self.verifier or DisabledGrantVerifier()
        auth_ref = str(action.get("authorization_ref", "") or "")
        ver_result = verifier.verify(auth_ref, prepared_action, current_profile_epoch)

        if isinstance(ver_result, GrantBlocker):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ver_result.code,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": ver_result.reason,
                },
            )

        if not isinstance(ver_result, VerifiedGrant):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.GRANT_INVALID,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": "Grant verification did not produce a VerifiedGrant",
                },
            )

        verified_grant = ver_result

        # -------------------------------------------------------------
        # Step 7: Journal gating (fence, unresolved intents, op id/hash)
        # -------------------------------------------------------------
        journal = ctx.get("journal") or self.journal
        if journal is None and store is not None:
            journal = Journal(store=store, verifier=verifier, fence=fence, clock=clock)

        if journal is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INTERNAL_ERROR,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": "MutationJournal is required for mutation dispatch",
                },
            )

        predecessor_op_id = action.get("predecessor_operation_id") or ctx.get(
            "predecessor_operation_id"
        )

        try:
            op_record = journal.prepare(
                prepared_action,
                verified_grant,
                predecessor_operation_id=predecessor_op_id,
                authorization_ref=auth_ref,
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": str(err),
                },
            )

        # Journal.prepare(same id, same hash) returns recorded record;
        # dispatch ONLY when returned state is PREPARED!
        if op_record.state != OperationState.PREPARED:
            if op_record.state == OperationState.BLOCKED_BEFORE_DISPATCH:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=op_record.error_code or ErrorCode.AUTH_DENIED,
                    coverage=coverage,
                    data={
                        "blocked": True,
                        "api_accepted": False,
                        "effect_observed": False,
                        "attribution": None,
                        "ui_verified": False,
                        "reason": "Operation recorded as blocked before dispatch",
                    },
                )
            if op_record.state == OperationState.REJECTED:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.REJECTED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=op_record.error_code or ErrorCode.INVALID_INPUT,
                    coverage=coverage,
                    data={
                        "blocked": False,
                        "api_accepted": False,
                        "effect_observed": False,
                        "attribution": None,
                        "ui_verified": False,
                        "reason": "Operation previously rejected",
                    },
                )
            if op_record.state in (OperationState.ACCEPTED, OperationState.EFFECT_OBSERVED):
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.OK,
                    exit_code=EXIT_OK,
                    coverage=coverage,
                    data={
                        "session": session_name,
                        "plan_id": latest_plan_id,
                        "api_accepted": True,
                        "effect_observed": op_record.effect_observed,
                        "attribution": op_record.attribution,
                        "ui_verified": False,
                        "atomic_approval_claimed": False,
                    },
                )
            if op_record.state in (OperationState.UNKNOWN, OperationState.DISPATCHING):
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.UNKNOWN,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=op_record.error_code or ErrorCode.UNCERTAIN_EFFECT,
                    coverage=coverage,
                    data={
                        "session": session_name,
                        "plan_id": latest_plan_id,
                        "api_accepted": op_record.api_accepted,
                        "effect_observed": False,
                        "attribution": None,
                        "ui_verified": False,
                        "atomic_approval_claimed": False,
                        "reason": "Operation in unresolved state",
                    },
                )

        # -------------------------------------------------------------
        # Step 8: Final session re-check before dispatch
        # -------------------------------------------------------------
        if api is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INTERNAL_ERROR,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": "JulesReadAPI / client is required for dispatch",
                },
            )

        try:
            final_session = api.sessions_get(session_name)
        except Exception as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TRANSPORT_ERROR,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Final session re-check failed: {err}",
                },
            )

        final_lifecycle = project_lifecycle(final_session)
        final_raw_state = final_session.state.strip().upper()
        final_is_open = (
            final_lifecycle.bucket == LifecycleBucket.OPEN
            and (
                final_raw_state in ("AWAITING_PLAN_APPROVAL", "PLANNING")
                or (
                    final_session.require_plan_approval is not False
                    and latest_plan_id is not None
                )
            )
        )

        if not final_is_open:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Session state drifted to '{final_session.state}' before dispatch",
                },
            )

        # -------------------------------------------------------------
        # Step 9: journal.begin_dispatch -> DispatchTicket
        # -------------------------------------------------------------
        try:
            ticket = journal.begin_dispatch(
                action["operation_id"],
                prepared_action.request_hash,
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                coverage=coverage,
                data={
                    "blocked": True,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "reason": f"Begin dispatch failed: {err.message}",
                },
            )

        # -------------------------------------------------------------
        # Step 10: API mutation method (redeems ticket) exactly once
        # -------------------------------------------------------------
        try:
            mutation_resp = api.sessions_approve_plan(ticket, session_name)
            # -------------------------------------------------------------
            # Step 11: journal.record_outcome
            # -------------------------------------------------------------
            outcome_record = journal.record_outcome(
                ticket,
                mutation_resp,
                evidence={
                    "plan_id": latest_plan_id,
                    "plan_hash": latest_plan_hash,
                    "session": session_name,
                },
            )
        except Exception as exc:
            try:
                journal.record_outcome(
                    ticket,
                    TransportOutcome(
                        status=0,
                        uncertain_effect=True,
                        sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                        body=str(exc).encode("utf-8"),
                    ),
                    evidence={
                        "plan_id": latest_plan_id,
                        "plan_hash": latest_plan_hash,
                        "session": session_name,
                        "error": str(exc),
                    },
                )
            except Exception:
                pass
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNKNOWN,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TRANSPORT_ERROR,
                coverage=coverage,
                data={
                    "operation_id": action["operation_id"],
                    "session": session_name,
                    "plan_id": latest_plan_id,
                    "error": str(exc),
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "atomic_approval_claimed": False,
                },
            )

        if outcome_record.state == OperationState.REJECTED:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.REJECTED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=outcome_record.error_code or ErrorCode.INVALID_INPUT,
                coverage=coverage,
                data={
                    "session": session_name,
                    "plan_id": latest_plan_id,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "atomic_approval_claimed": False,
                },
            )

        if outcome_record.state == OperationState.UNKNOWN:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNKNOWN,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=outcome_record.error_code or ErrorCode.UNCERTAIN_EFFECT,
                coverage=coverage,
                data={
                    "session": session_name,
                    "plan_id": latest_plan_id,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": None,
                    "ui_verified": False,
                    "atomic_approval_claimed": False,
                },
            )

        # -------------------------------------------------------------
        # Step 12: Bounded read-only effect verification / reconciliation
        # -------------------------------------------------------------
        post_activities: Sequence[ActivityRecord] = ()
        try:
            post_activities_resp = api.activities_list(session_name)
            if isinstance(post_activities_resp, (list, tuple)):
                if len(post_activities_resp) == 2 and isinstance(
                    post_activities_resp[0], (list, tuple)
                ):
                    post_activities = post_activities_resp[0]
                else:
                    post_activities = post_activities_resp
            elif hasattr(post_activities_resp, "activities"):
                post_activities = getattr(post_activities_resp, "activities") or ()
        except Exception:
            post_activities = ()

        # Check for plan change after read
        post_plan_proj = project_plan(post_activities, final_session)
        plan_changed = False
        if (
            post_plan_proj.latest_plan_id is not None
            and post_plan_proj.latest_plan_id != latest_plan_id
        ):
            plan_changed = True
        elif (
            post_plan_proj.latest_plan_hash is not None
            and post_plan_proj.latest_plan_hash != latest_plan_hash
        ):
            plan_changed = True

        # Scan for planApproved activities
        matching_approval_found = False
        wrong_id_found = False

        for act in post_activities:
            proj = project_activity(act)
            if proj.kind == ActivityKind.PLAN_APPROVED:
                raw_act = dict(proj.raw_data)
                approved_pid = raw_act.get("planId") or raw_act.get("plan_id")
                if approved_pid is not None:
                    approved_pid_str = str(approved_pid)
                    if approved_pid_str == latest_plan_id:
                        matching_approval_found = True
                    else:
                        wrong_id_found = True

        # Evaluate inconclusive vs effect_observed
        if plan_changed:
            effect_observed = False
            attribution = "inconclusive:plan_changed_after_read"
            inconclusive = True
        elif wrong_id_found and not matching_approval_found:
            effect_observed = False
            attribution = "inconclusive:wrong_plan_approved_id"
            inconclusive = True
        elif matching_approval_found and not plan_changed:
            effect_observed = True
            attribution = "inferred:matching_plan_approved_event"
            inconclusive = False
        else:
            # Missing event
            effect_observed = False
            attribution = "inconclusive:missing_plan_approved_event"
            inconclusive = True

        # Record effect in store if observed
        if effect_observed:
            if store is not None and hasattr(store, "transition_operation_state"):
                store.transition_operation_state(
                    ticket.operation_id,
                    OperationState.EFFECT_OBSERVED,
                    effect_observed=True,
                    attribution=attribution,
                    fence=fence,
                )
        else:
            if store is not None and hasattr(store, "update_operation_evidence_flags"):
                store.update_operation_evidence_flags(
                    ticket.operation_id,
                    effect_observed=False,
                    attribution=attribution,
                    fence=fence,
                )

        status = ActionResultStatus.OK if effect_observed else ActionResultStatus.PARTIAL
        exit_code = EXIT_OK if effect_observed else EXIT_PARTIAL_OR_UNSUPPORTED

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=status,
            exit_code=exit_code,
            error_code=None,
            coverage=coverage,
            data={
                "session": session_name,
                "plan_id": latest_plan_id,
                "api_accepted": True,
                "effect_observed": effect_observed,
                "attribution": attribution,
                "ui_verified": False,
                "inconclusive": inconclusive,
                "atomic_approval_claimed": False,
            },
        )


ApproveActionHandler = PlansApproveHandler
