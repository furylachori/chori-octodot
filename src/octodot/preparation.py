"""Action preparation and read-only validation for mutation dispatch.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from octodot.authorization import DisabledGrantVerifier
from octodot.contracts import (
    Clock,
    CredentialSource,
    GrantBlocker,
    GrantVerifier,
    ReadService,
    canonical_hash,
    compute_plan_hash,
    context_hash,
    request_hash,
    validate_plan,
)
from octodot.errors import ErrorCode, OctodotError
from octodot.models import (
    Binding,
    CandidateBundle,
    Coverage,
    Observation,
    PreparedAction,
    VerifiedGrant,
)


def compute_mutation_request_body(op: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compute exact outgoing transport request body for a mutation operation.

    - chats.reply (sendMessage): {"prompt": <exact text>}
    - plans.approve (approvePlan): {}
    - tasks.create: payload passed literally
    """
    if op == "chats.reply":
        if "prompt" in payload:
            text = payload["prompt"]
        elif "text" in payload:
            text = payload["text"]
        else:
            text = ""
        return {"prompt": text}
    elif op == "plans.approve":
        return {}
    elif op == "tasks.create":
        return dict(payload)
    else:
        return dict(payload)


def compute_mutation_request_hash(
    target: str, op: str, payload: Mapping[str, Any]
) -> str:
    """Compute canonical request_hash over {"target": target, "body": outgoing_body}."""
    body = compute_mutation_request_body(op, payload)
    return request_hash({"target": target, "body": body})


def assert_complete_coverage_and_unambiguous_bundle(
    coverage: Coverage | None = None,
    candidate_bundle: CandidateBundle | None = None,
) -> None:
    """Ensure read observations provide complete coverage and no ambiguity.

    Raises:
    - OctodotError(ErrorCode.PARTIAL_COVERAGE): if coverage is incomplete.
    - OctodotError(ErrorCode.IDENTITY_AMBIGUOUS): if candidate bundle has ambiguity.
    """
    if coverage is not None and not coverage.complete:
        reasons_str = f": {list(coverage.reasons)}" if coverage.reasons else ""
        raise OctodotError(
            ErrorCode.PARTIAL_COVERAGE,
            f"Cannot prepare action for dispatch: read coverage is incomplete{reasons_str}",
        )

    if candidate_bundle is not None and candidate_bundle.has_ambiguity:
        reasons_str = (
            f": {list(candidate_bundle.ambiguity_reasons)}"
            if candidate_bundle.ambiguity_reasons
            else ""
        )
        raise OctodotError(
            ErrorCode.IDENTITY_AMBIGUOUS,
            f"Cannot prepare action for dispatch: candidate bundle has ambiguity{reasons_str}",
        )


def _check_observation_completeness(obs: Any) -> tuple[Coverage | None, CandidateBundle | None]:
    """Inspect an observation or result for coverage and candidate bundle."""
    cov: Coverage | None = None
    cb: CandidateBundle | None = None

    if isinstance(obs, Observation):
        cov = obs.coverage
        cb = obs.candidate_bundle
    elif isinstance(obs, tuple) and len(obs) == 2 and isinstance(obs[1], Coverage):
        cov = obs[1]
    elif hasattr(obs, "coverage") and isinstance(getattr(obs, "coverage"), Coverage):
        cov = getattr(obs, "coverage")
    elif isinstance(obs, Mapping) and "coverage" in obs:
        raw_cov = obs["coverage"]
        if isinstance(raw_cov, Coverage):
            cov = raw_cov
        elif isinstance(raw_cov, Mapping):
            cov = Coverage(
                complete=bool(raw_cov.get("complete", False)),
                reasons=tuple(raw_cov.get("reasons", ())),
            )

    if hasattr(obs, "candidate_bundle") and isinstance(getattr(obs, "candidate_bundle"), CandidateBundle):
        cb = getattr(obs, "candidate_bundle")
    elif isinstance(obs, Mapping) and "candidate_bundle" in obs:
        raw_cb = obs["candidate_bundle"]
        if isinstance(raw_cb, CandidateBundle):
            cb = raw_cb
        elif isinstance(raw_cb, Mapping):
            cb = CandidateBundle(
                has_ambiguity=bool(raw_cb.get("has_ambiguity", False)),
                ambiguity_reasons=tuple(raw_cb.get("ambiguity_reasons", ())),
            )

    assert_complete_coverage_and_unambiguous_bundle(coverage=cov, candidate_bundle=cb)
    return cov, cb


def extract_material_context(
    action: Mapping[str, Any],
    read_service: ReadService | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract material context for an action and verify read completeness.

    Raises OctodotError if coverage is incomplete or candidate bundle is ambiguous.
    """
    if context is not None:
        _check_observation_completeness(context)
        return dict(context)

    if read_service is None:
        return {}

    op = str(action.get("op", ""))
    target = str(action.get("target", ""))

    material: dict[str, Any] = {"op": op, "target": target}

    if op == "chats.reply":
        obs = read_service.chats({"session": target})
        cov, cb = _check_observation_completeness(obs)
        if cb is not None:
            material["messages"] = cb.messages
            material["last_message_text"] = cb.last_message_text
            material["selected_activity_id"] = cb.selected_activity_id
        elif isinstance(obs, Mapping):
            if "messages" in obs:
                material["messages"] = obs["messages"]
            if "last_message_text" in obs:
                material["last_message_text"] = obs["last_message_text"]
    elif op == "plans.approve":
        inspect_binding = Binding(
            profile="default",
            profile_epoch=0,
            source="",
            repository="",
            session=target,
        )
        obs = read_service.inspect(inspect_binding)
        _check_observation_completeness(obs)
        if isinstance(obs, Mapping):
            material.update({k: v for k, v in obs.items() if k not in {"timestamp", "observed_at"}})
        elif hasattr(obs, "session"):
            material["session"] = getattr(obs, "session")
            if hasattr(obs, "state"):
                material["state"] = getattr(obs, "state")
    elif op == "tasks.create":
        # Check source inventory
        obs = read_service.collect({"repository": target}, {})
        _check_observation_completeness(obs)
        material["repository"] = target

    return material


def prepare_action(
    action: Mapping[str, Any],
    plan: Mapping[str, Any],
    current_profile_epoch: int,
    read_service: ReadService | None = None,
    context: Mapping[str, Any] | None = None,
    binding: Binding | None = None,
    source: str | None = None,
    publication_scope: str = "none",
    credential_source: CredentialSource | None = None,
    request_hash_override: str | None = None,
) -> PreparedAction:
    """Build PreparedAction from literal mutation action and ReadService observations.

    Guarantees:
    - Never touches CredentialSource (verified offline).
    - Checks for complete coverage and absence of chronological ambiguity.
    - Computes canonical payload_hash, context_hash, plan_hash, and request_hash.
    """
    # Defensive check: CredentialSource must not be accessed
    if credential_source is not None:
        initial_access_count = credential_source.access_count()
    else:
        initial_access_count = 0

    material_ctx = extract_material_context(
        action=action,
        read_service=read_service,
        context=context,
    )

    # Compute hashes
    payload = dict(action.get("payload", {}))
    payload_h = canonical_hash(payload)
    ctx_h = context_hash(material_ctx)

    plan_dict = dict(plan)
    plan_h = plan_dict.get("plan_hash") or compute_plan_hash(plan_dict)

    op = str(action.get("op", ""))
    target = str(action.get("target", ""))
    req_h = request_hash_override or compute_mutation_request_hash(target, op, payload)

    # Build or resolve Binding
    if binding is not None:
        action_binding = binding
    else:
        scope = plan_dict.get("scope", {})
        repo = scope.get("repository", "")
        branch = scope.get("branch")
        resolved_source = source or f"sources/github/{repo}"
        session = target if op != "tasks.create" else None
        action_binding = Binding(
            profile=str(plan_dict.get("profile", "default")),
            profile_epoch=current_profile_epoch,
            source=resolved_source,
            repository=repo,
            starting_branch=branch,
            session=session,
        )

    pub_scope = str(action.get("publication_scope", publication_scope))
    action_id = str(action.get("id", ""))
    operation_id = str(action.get("operation_id", ""))

    # Verify credential source was not accessed
    if credential_source is not None and credential_source.access_count() != initial_access_count:
        raise OctodotError(
            ErrorCode.INTERNAL_ERROR,
            "CredentialSource was improperly accessed during read-only preparation",
        )

    return PreparedAction(
        action=action_id,
        operation_id=operation_id,
        binding=action_binding,
        payload=payload,
        payload_hash=payload_h,
        context_hash=ctx_h,
        request_hash=req_h,
        publication_scope=pub_scope,
        plan_hash=plan_h,
    )


def prepare_plan_actions(
    plan: Mapping[str, Any],
    current_profile_epoch: int,
    read_service: ReadService | None = None,
    credential_source: CredentialSource | None = None,
) -> tuple[PreparedAction, ...]:
    """Prepare all enabled mutation actions in a plan."""
    prepared: list[PreparedAction] = []
    actions = plan.get("actions", [])
    for act in actions:
        if not isinstance(act, Mapping):
            continue
        # Only prepare enabled mutation operations
        op = str(act.get("op", ""))
        if op in {"chats.reply", "tasks.create", "plans.approve"} and act.get("enabled", False):
            prep = prepare_action(
                action=act,
                plan=plan,
                current_profile_epoch=current_profile_epoch,
                read_service=read_service,
                credential_source=credential_source,
            )
            prepared.append(prep)

    return tuple(prepared)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Report generated by --validate-only execution."""

    plan_id: str
    plan_hash: str
    prepared_actions: tuple[PreparedAction, ...]
    verified_grants: tuple[VerifiedGrant, ...]
    blockers: tuple[GrantBlocker, ...]
    eligible: bool

    @property
    def is_valid(self) -> bool:
        """Return True if plan is eligible and has no blockers."""
        return self.eligible and len(self.blockers) == 0


def validate_only(
    plan: dict[str, Any],
    current_profile_epoch: int,
    read_service: ReadService | None = None,
    grant_verifier: GrantVerifier | None = None,
    credential_source: CredentialSource | None = None,
) -> ValidationReport:
    """Execute the read-only --validate-only verification pathway.

    Never touches CredentialSource or network. Validates plan structure,
    prepares enabled mutation actions, and checks grants with verifier.
    """
    if credential_source is not None and credential_source.was_accessed():
        raise OctodotError(
            ErrorCode.INTERNAL_ERROR,
            "CredentialSource already had recorded accesses prior to validate_only",
        )

    # 1. Structural validation
    validate_plan(plan)

    verifier = grant_verifier or DisabledGrantVerifier()
    plan_id = str(plan.get("plan_id", ""))
    plan_hash = str(plan.get("plan_hash", ""))

    prepared_list: list[PreparedAction] = []
    verified_grants: list[VerifiedGrant] = []
    blockers: list[GrantBlocker] = []

    actions = plan.get("actions", [])
    for act in actions:
        if not isinstance(act, dict):
            continue
        op = str(act.get("op", ""))
        if op in {"chats.reply", "tasks.create", "plans.approve"}:
            if not act.get("enabled", False):
                continue

            prep = prepare_action(
                action=act,
                plan=plan,
                current_profile_epoch=current_profile_epoch,
                read_service=read_service,
                credential_source=credential_source,
            )
            prepared_list.append(prep)

            auth_ref = str(act.get("authorization_ref", ""))
            grant_result = verifier.verify(
                reference=auth_ref,
                prepared_action=prep,
                current_profile_epoch=current_profile_epoch,
            )

            if isinstance(grant_result, VerifiedGrant):
                verified_grants.append(grant_result)
            elif isinstance(grant_result, GrantBlocker):
                blockers.append(grant_result)

    # Verify credentials remained untouched
    if credential_source is not None and credential_source.was_accessed():
        raise OctodotError(
            ErrorCode.INTERNAL_ERROR,
            "CredentialSource was accessed during --validate-only run",
        )

    return ValidationReport(
        plan_id=plan_id,
        plan_hash=plan_hash,
        prepared_actions=tuple(prepared_list),
        verified_grants=tuple(verified_grants),
        blockers=tuple(blockers),
        eligible=True,
    )
