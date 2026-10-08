"""Action handler for 'tasks.create' operations in octodot plans.

Standard library only. Compatible with Python 3.10+.
Provides TasksCreateHandler implementing the ActionHandler protocol.

Pipeline:
literal action -> execution eligibility (contracts) -> fresh full rescan via ReadService
(never a reused selection) -> S04 binding/projection checks -> S05 prepare_action (hashes)
-> GrantVerifier.verify (S05; DisabledGrantVerifier is the default and must block)
-> journal gating (fence, unresolved intents, operation id/hash) -> final session re-check
-> journal.begin_dispatch -> API mutation method (redeems the ticket) exactly once
-> journal.record_outcome -> bounded read-only reconciliation (S08)
-> ActionResult with api_accepted / effect_observed / attribution / ui_verified separate.

Rules:
- tasks.create body contains ONLY the approved title/prompt, sourceContext
  {source, githubRepoContext {startingBranch}}, and requirePlanApproval (defaults to False, optional True).
- publication none => automationMode omitted.
- AUTO_CREATE_PR request without an explicit publication grant,
  or a prompt-level unapproved publication request => fail.
- Never add controller fields (hashes/grant/op IDs) to the body.
- Preflight enumerates complete existing-session identity set, verifies exact source
  and case-sensitive branch with affirmative evidence (absent/stale => branch_unverified),
  and requires logical-task marker to already be present in approved text.
- Exact-commit requirement => unsupported_exact_commit.
- Response: invalid 2xx => unknown.
- Valid session but refresh GET fails => accepted_identity_unverified (never recreate).
- Wrong binding => blocks confirmation.
- Marker collision, lost response, duplicate run, or no candidates after repeated
  full scans => never a second POST.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import re
from typing import Any

from octodot.authorization import DisabledGrantVerifier
from octodot.contracts import (
    ActionHandler,
    Clock,
    GrantBlocker,
    GrantVerifier,
    JulesReadAPI,
    MutationJournal,
    ReadService,
    RecoveryFence,
    canonical_hash,
    compute_plan_hash,
    request_hash,
    _scan_for_placeholders,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
    OctodotError,
)
from octodot.identity import (
    bind_session,
    extract_session_branch,
    resolve_source,
    validate_repository_name,
)
from octodot.journal import Journal, extract_logical_task_marker
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    Binding,
    DispatchTicket,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.preparation import prepare_action
from octodot.reads import session_to_dict
from octodot.reconciliation import Reconciler


# Patterns detecting unapproved prompt-level publication requests
_PROMPT_PUBLICATION_PATTERN = re.compile(
    r"\b(create|open|make|submit|auto[-_ ]?create)\b.*?\b(pr|pull[-_ ]?request)\b|\bAUTO_CREATE_PR\b",
    re.IGNORECASE,
)

# Keys indicating exact commit pinning requests
_EXACT_COMMIT_KEYS = frozenset({
    "commit",
    "commit_sha",
    "exact_commit",
    "base_commit",
    "starting_commit",
    "head_sha",
    "baseCommitId",
    "startingCommit",
    "exactCommit",
    "commit_pinning",
})


def _is_prompt_requesting_publication(text: str) -> bool:
    """Return True if prompt text contains instructions requesting PR creation."""
    if not text:
        return False
    return bool(_PROMPT_PUBLICATION_PATTERN.search(text))


def _has_exact_commit_requirement(
    action: Mapping[str, Any],
    payload: Mapping[str, Any],
    preconditions: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> bool:
    """Return True if any mapping contains a requested exact commit pinning."""
    for mapping in (action, payload, preconditions, scope):
        if not isinstance(mapping, Mapping):
            continue
        for key in _EXACT_COMMIT_KEYS:
            val = mapping.get(key)
            if val is not None and val is not False and val != "":
                return True
    return False


def _session_matches_marker(session: Any, marker: str | None) -> bool:
    """Match session against logical task marker, consistent with reconciliation logic."""
    if not marker:
        return False
    clean = marker.strip()
    title = getattr(session, "title", None) or (
        session.get("title") if isinstance(session, dict) else ""
    )
    prompt = getattr(session, "prompt", None) or (
        session.get("prompt") if isinstance(session, dict) else ""
    )
    name = getattr(session, "name", None) or (
        session.get("name") if isinstance(session, dict) else ""
    )
    if clean in str(title or ""):
        return True
    if clean in str(prompt or ""):
        return True
    if clean == str(name or ""):
        return True
    if isinstance(session, SessionRecord):
        for _k, v in session.unknown_fields:
            if clean in str(v):
                return True
    return False


class TasksCreateHandler:
    """ActionHandler for 'tasks.create'."""

    def __init__(
        self,
        api: JulesReadAPI | Any | None = None,
        read_service: ReadService | Any | None = None,
        journal: MutationJournal | Journal | Any | None = None,
        verifier: GrantVerifier | Any | None = None,
        fence: RecoveryFence | Any | None = None,
        clock: Clock | Any | None = None,
        reconciler: Reconciler | Any | None = None,
        store: Any = None,
    ) -> None:
        self.api = api
        self.read_service = read_service
        self.journal = journal
        self.verifier = verifier
        self.fence = fence
        self.clock = clock
        self.reconciler = reconciler
        self.store = store

    def can_handle(self, op: str) -> bool:
        """Return True if this handler handles the specified op."""
        return op == "tasks.create"

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
    ) -> ActionResult:
        """Execute tasks.create through the mandatory mutation pipeline."""
        action_id = str(action.get("id", "act-create"))
        op = "tasks.create"

        # Resolve runtime dependencies from self or context
        api: JulesReadAPI | Any | None = (
            self.api or context.get("api") or context.get("client")
        )
        read_service: ReadService | Any | None = (
            self.read_service or context.get("read_service")
        )
        journal: MutationJournal | Journal | Any | None = (
            self.journal or context.get("journal")
        )
        verifier: GrantVerifier | Any = (
            self.verifier
            if self.verifier is not None
            else context.get("verifier", DisabledGrantVerifier())
        )
        fence: RecoveryFence | Any | None = (
            self.fence if self.fence is not None else context.get("fence")
        )
        reconciler: Reconciler | Any | None = (
            self.reconciler or context.get("reconciler")
        )
        store: Any = self.store or context.get("store")
        if store is None and hasattr(journal, "store"):
            store = journal.store

        # Check required infrastructure
        if read_service is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "ReadService missing from context", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        if journal is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "MutationJournal missing from context", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Plan context metadata
        raw_plan = context.get("plan")
        if raw_plan:
            plan = dict(raw_plan)
        else:
            plan = {
                "schema_version": "jules-controller.plan.v1",
                "plan_id": f"plan-{action_id}",
                "plan_hash": "",
                "profile": str(context.get("profile", "default")),
                "scope": {"repository": str(action.get("target", "")), "branch": action.get("branch")},
                "actions": [action],
            }
        if not plan.get("plan_hash"):
            plan["plan_hash"] = compute_plan_hash(plan)

        profile: str = str(context.get("profile") or plan.get("profile", "default"))
        current_profile_epoch: int = int(context.get("current_profile_epoch", 0))
        if current_profile_epoch == 0 and fence is not None and hasattr(fence, "get_current_epoch"):
            try:
                current_profile_epoch = fence.get_current_epoch(profile)
            except Exception:
                pass
        limits: dict[str, Any] = context.get("limits") or plan.get("limits") or {}

        # ---------------------------------------------------------------------
        # Step 1: Execution eligibility (contracts)
        # ---------------------------------------------------------------------
        # Disabled template check
        if action.get("enabled") is False:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TEMPLATE_DISABLED,
                data={"error": f"Template mutation action '{action_id}' is disabled", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Placeholder scan
        has_placeholder, token = _scan_for_placeholders(action)
        if has_placeholder:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.PLACEHOLDER_PRESENT,
                data={"error": f"Placeholder token found in action: '{token}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Extract payload and preconditions
        payload = dict(action.get("payload") or {})
        preconditions = dict(action.get("preconditions") or {})
        scope = dict(plan.get("scope") or {})

        # Check exact commit requirement -> unsupported_exact_commit
        if _has_exact_commit_requirement(action, payload, preconditions, scope):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNSUPPORTED,
                exit_code=EXIT_PARTIAL_OR_UNSUPPORTED,
                error_code=ErrorCode.UNSUPPORTED_EXACT_COMMIT,
                data={"error": "Exact-commit pinning is unsupported", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Check requirePlanApproval (defaults to False for autonomous execution, optional True)
        raw_approval = payload.get("requirePlanApproval")
        if raw_approval is None:
            raw_approval = payload.get("require_plan_approval")
        if raw_approval is None:
            raw_approval = action.get("requirePlanApproval")
        if raw_approval is None:
            raw_approval = action.get("require_plan_approval")
        req_approval = bool(raw_approval) if raw_approval is not None else False

        # Extract title and prompt
        title = payload.get("title") or action.get("title")
        prompt = payload.get("prompt") or action.get("prompt")
        if not isinstance(title, str) or not title.strip():
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Task title must be a non-empty string", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )
        if not isinstance(prompt, str) or not prompt.strip():
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Task prompt must be a non-empty string", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Extract publication scope and automationMode
        pub_scope = (
            action.get("publication_scope")
            or payload.get("publication_scope")
            or action.get("automationMode")
            or payload.get("automationMode")
            or "none"
        )
        if pub_scope not in ("none", "AUTO_CREATE_PR"):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": f"Invalid publication scope: {pub_scope}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Prompt-level publication request check
        if pub_scope == "none" and _is_prompt_requesting_publication(prompt):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.AUTH_DENIED,
                data={"error": "Prompt requests publication but publication scope is 'none'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Extract logical-task marker
        marker = (
            extract_logical_task_marker(payload)
            or extract_logical_task_marker(preconditions)
            or extract_logical_task_marker(action)
        )
        if marker is not None:
            # Marker must already be present in approved text (prompt or title)
            if marker not in prompt and marker not in title:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.INVALID_INPUT,
                    data={"error": f"Logical-task marker '{marker}' must already be present in approved text", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                )

        # Target repository
        target = str(action.get("target") or "")
        repo_candidate = (
            action.get("repository")
            or payload.get("repository")
            or scope.get("repository")
            or (target if "/" in target else "")
        )
        try:
            validate_repository_name(repo_candidate)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": f"Invalid repository: {err}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )
        repository = repo_candidate

        # Starting branch
        starting_branch = (
            action.get("branch")
            or preconditions.get("branch")
            or payload.get("branch")
            or scope.get("branch")
        )
        if not starting_branch or not isinstance(starting_branch, str) or not starting_branch.strip():
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BRANCH_UNVERIFIED,
                data={"error": "Starting branch is absent/unverified; branch fallback is forbidden", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )
        starting_branch = starting_branch.strip()

        # ---------------------------------------------------------------------
        # Step 2: Fresh full rescan via ReadService (never a reused selection)
        # ---------------------------------------------------------------------
        try:
            collection, coverage = read_service.collect(
                scope={"repository": repository}, limits=limits
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": f"Preflight read rescan failed: {err}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        if not coverage.complete:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.PARTIAL_COVERAGE,
                coverage=coverage,
                data={"error": "Preflight rescan returned partial coverage", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # ---------------------------------------------------------------------
        # Step 3: S04 Binding and projection checks
        # ---------------------------------------------------------------------
        # 3a. Resolve exact source
        try:
            source_rec = resolve_source(collection.sources, repository)
            source_name = source_rec.name
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": f"Source resolution failed for repository '{repository}': {err}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # 3b. Verify affirmative branch evidence
        # Check sessions from preflight scan for branch evidence
        has_affirmative_branch = False
        for sess in collection.sessions:
            sess_branch = extract_session_branch(sess)
            if sess_branch is not None:
                if sess_branch == starting_branch:
                    has_affirmative_branch = True
                    break
                elif sess_branch.lower() == starting_branch.lower():
                    # Case mismatch
                    return ActionResult.create(
                        action_id=action_id,
                        op=op,
                        status=ActionResultStatus.BLOCKED,
                        exit_code=EXIT_MUTATION_BLOCKED,
                        error_code=ErrorCode.BINDING_MISMATCH,
                        data={"error": f"Starting branch case mismatch: expected '{starting_branch}', observed '{sess_branch}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                    )

        # Check preconditions/context for branch evidence if not found in existing sessions
        if not has_affirmative_branch:
            branch_evidence = (
                preconditions.get("branch_evidence")
                or preconditions.get("verified_branch")
                or preconditions.get("affirmative_branch")
                or context.get("branch_evidence")
            )
            branch_stale = (
                preconditions.get("branch_stale", False)
                or preconditions.get("stale_branch", False)
                or (branch_evidence == "stale")
            )
            branch_verified = preconditions.get("branch_verified")

            if branch_stale or branch_verified is False:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.BRANCH_UNVERIFIED,
                    data={"error": f"Branch metadata is stale for branch '{starting_branch}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                )

            if branch_evidence is not None:
                if isinstance(branch_evidence, str):
                    if branch_evidence == starting_branch:
                        has_affirmative_branch = True
                    elif branch_evidence.lower() == starting_branch.lower():
                        return ActionResult.create(
                            action_id=action_id,
                            op=op,
                            status=ActionResultStatus.BLOCKED,
                            exit_code=EXIT_MUTATION_BLOCKED,
                            error_code=ErrorCode.BINDING_MISMATCH,
                            data={"error": f"Starting branch case mismatch in evidence: '{starting_branch}' vs '{branch_evidence}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                        )
                    else:
                        return ActionResult.create(
                            action_id=action_id,
                            op=op,
                            status=ActionResultStatus.BLOCKED,
                            exit_code=EXIT_MUTATION_BLOCKED,
                            error_code=ErrorCode.BRANCH_UNVERIFIED,
                            data={"error": f"Starting branch evidence mismatch: expected '{starting_branch}', found '{branch_evidence}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                        )
                elif isinstance(branch_evidence, (list, tuple, set)):
                    if starting_branch in branch_evidence:
                        has_affirmative_branch = True
                    elif any(
                        isinstance(b, str) and b.lower() == starting_branch.lower()
                        for b in branch_evidence
                    ):
                        return ActionResult.create(
                            action_id=action_id,
                            op=op,
                            status=ActionResultStatus.BLOCKED,
                            exit_code=EXIT_MUTATION_BLOCKED,
                            error_code=ErrorCode.BINDING_MISMATCH,
                            data={"error": "Starting branch case mismatch in evidence list", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                        )
                    else:
                        return ActionResult.create(
                            action_id=action_id,
                            op=op,
                            status=ActionResultStatus.BLOCKED,
                            exit_code=EXIT_MUTATION_BLOCKED,
                            error_code=ErrorCode.BRANCH_UNVERIFIED,
                            data={"error": f"Starting branch '{starting_branch}' not found in evidence list", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                        )
            elif branch_verified is True:
                has_affirmative_branch = True

        if not has_affirmative_branch:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BRANCH_UNVERIFIED,
                data={"error": f"Affirmative branch evidence absent for branch '{starting_branch}' on repository '{repository}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # 3c. Preflight marker collision check against existing sessions
        if marker is not None:
            for s in collection.sessions:
                if _session_matches_marker(s, marker):
                    return ActionResult.create(
                        action_id=action_id,
                        op=op,
                        status=ActionResultStatus.BLOCKED,
                        exit_code=EXIT_MUTATION_BLOCKED,
                        error_code=ErrorCode.OPERATION_CONFLICT,
                        data={"error": f"Logical-task marker '{marker}' collides with existing session '{getattr(s, 'name', '')}'", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
                    )

        # ---------------------------------------------------------------------
        # Step 4: Construct outgoing body and action preparation (S05)
        # ---------------------------------------------------------------------
        # Outgoing request body contains ONLY the approved fields
        outgoing_body: dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "sourceContext": {
                "source": source_name,
                "githubRepoContext": {
                    "startingBranch": starting_branch,
                },
            },
            "requirePlanApproval": req_approval,
        }
        if pub_scope == "AUTO_CREATE_PR":
            outgoing_body["automationMode"] = "AUTO_CREATE_PR"

        # Construct Action Binding
        action_binding = Binding(
            profile=profile,
            profile_epoch=current_profile_epoch,
            source=source_name,
            repository=repository,
            starting_branch=starting_branch,
            session=None,
        )

        # Compute request hash for ticket redemption
        # Sessions create target at API layer is /v1alpha/sessions
        primary_target = "/v1alpha/sessions"
        req_hash = request_hash({"target": primary_target, "body": outgoing_body})

        # Prepare action
        operation_id = str(action.get("operation_id") or "")
        if not operation_id:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "operation_id is required", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        try:
            prepared_action = prepare_action(
                action=action,
                plan=plan,
                current_profile_epoch=current_profile_epoch,
                read_service=read_service,
                binding=action_binding,
                source=source_name,
                publication_scope=pub_scope,
                request_hash_override=req_hash,
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": f"Action preparation failed: {err}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # ---------------------------------------------------------------------
        # Step 5: Grant verification (S05)
        # ---------------------------------------------------------------------
        auth_ref = str(action.get("authorization_ref") or "")
        if isinstance(verifier, DisabledGrantVerifier):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.VERIFIER_UNAVAILABLE,
                data={"error": "Automated writes disabled by DisabledGrantVerifier", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        verification_result = verifier.verify(
            auth_ref,
            prepared_action,
            current_profile_epoch,
        )

        if isinstance(verification_result, GrantBlocker):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=verification_result.code,
                data={"error": f"Grant verification failed: {verification_result.reason}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        grant: VerifiedGrant = verification_result

        # Explicit publication grant check
        if pub_scope == "AUTO_CREATE_PR" and grant.publication_scope != "AUTO_CREATE_PR":
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.AUTH_DENIED,
                data={"error": "AUTO_CREATE_PR requested without explicit publication grant", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # ---------------------------------------------------------------------
        # Step 6: Journal gating & preparation (fence, unresolved intents)
        # ---------------------------------------------------------------------
        predecessor_op_id = (
            preconditions.get("predecessor_operation_id")
            or action.get("predecessor_operation_id")
            or context.get("predecessor_operation_id")
        )

        try:
            record = journal.prepare(
                prepared_action,
                grant,
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
                data={"error": f"Journal prepare failed: {err}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # Replay / idempotent check: dispatch only when returned state is PREPARED
        if record.state != OperationState.PREPARED:
            status_map = {
                OperationState.ACCEPTED: ActionResultStatus.OK,
                OperationState.EFFECT_OBSERVED: ActionResultStatus.OK,
                OperationState.UNKNOWN: ActionResultStatus.UNKNOWN,
                OperationState.REJECTED: ActionResultStatus.REJECTED,
                OperationState.BLOCKED_BEFORE_DISPATCH: ActionResultStatus.BLOCKED,
                OperationState.CANCELLED_BEFORE_DISPATCH: ActionResultStatus.BLOCKED,
            }
            res_status = status_map.get(record.state, ActionResultStatus.BLOCKED)
            exit_code = EXIT_OK if res_status == ActionResultStatus.OK else EXIT_MUTATION_BLOCKED
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=res_status,
                exit_code=exit_code,
                error_code=record.error_code,
                data={
                    "operation_id": operation_id,
                    "state": record.state.value,
                    "api_accepted": record.api_accepted,
                    "effect_observed": record.effect_observed,
                    "attribution": record.attribution,
                    "ui_verified": record.ui_verified,
                },
            )

        # ---------------------------------------------------------------------
        # Step 7: Begin dispatch (issue single-use ticket)
        # ---------------------------------------------------------------------
        try:
            ticket: DispatchTicket = journal.begin_dispatch(operation_id, req_hash)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": f"Dispatch commit failed: {err}", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        # ---------------------------------------------------------------------
        # Step 8: API mutation method (redeems ticket, exactly one POST)
        # ---------------------------------------------------------------------
        if api is None:
            try:
                journal.record_outcome(
                    ticket,
                    TransportOutcome(
                        status=0,
                        uncertain_effect=True,
                        sanitized_error_code=ErrorCode.INTERNAL_ERROR,
                    ),
                    evidence={"error": "API client missing from context for dispatch"},
                )
            except Exception:
                pass
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNKNOWN,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "API client missing from context for dispatch", "api_accepted": False, "effect_observed": False, "attribution": "", "ui_verified": False},
            )

        evidence_dict: dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "repository": repository,
            "starting_branch": starting_branch,
            "source": source_name,
            "mutation_kind": "tasks.create",
        }
        if marker:
            evidence_dict["logical_task_marker"] = marker

        try:
            mutation_response: MutationResponse = api.sessions_create(ticket, outgoing_body)
            # ---------------------------------------------------------------------
            # Step 9: Journal record outcome
            # ---------------------------------------------------------------------
            operation_record = journal.record_outcome(
                ticket,
                mutation_response,
                evidence=evidence_dict,
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
                    evidence=dict(evidence_dict, error=str(exc)),
                )
            except Exception:
                pass
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNKNOWN,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TRANSPORT_ERROR,
                data={
                    "operation_id": operation_id,
                    "error": str(exc),
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": "",
                    "ui_verified": False,
                },
            )

        # ---------------------------------------------------------------------
        # Step 10: Response handling, refresh GET & binding verification
        # ---------------------------------------------------------------------
        outcome = mutation_response.outcome

        # Case A: 2xx success
        if outcome.status in (200, 201):
            # Check for invalid 2xx: missing session or uncertain_effect
            if outcome.uncertain_effect or mutation_response.session is None or not mutation_response.session.name:
                # Invalid 2xx becomes unknown; bounded read-only reconciliation
                if reconciler is not None:
                    reconciler.reconcile(operation_id, read_api=api, scans=1)
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.UNKNOWN,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.MALFORMED_RESPONSE,
                    data={
                        "operation_id": operation_id,
                        "api_accepted": False,
                        "effect_observed": False,
                        "attribution": "",
                        "ui_verified": False,
                    },
                )

            # Valid 2xx response: api_accepted is True
            created_session = mutation_response.session
            session_name = created_session.name

            # Refresh GET to verify binding
            try:
                refreshed_session: SessionRecord = api.sessions_get(session_name)
            except Exception:
                # Refresh GET failed => accepted_identity_unverified (never recreate)
                if store is not None and hasattr(store, "update_operation_evidence_flags"):
                    try:
                        store.update_operation_evidence_flags(
                            operation_id,
                            accepted_identity_unverified=True,
                            fence=fence,
                        )
                    except Exception:
                        pass
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.UNKNOWN,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.ACCEPTED_IDENTITY_UNVERIFIED,
                    data={
                        "operation_id": operation_id,
                        "session": session_name,
                        "api_accepted": True,
                        "accepted_identity_unverified": True,
                        "effect_observed": False,
                        "attribution": "",
                        "ui_verified": False,
                    },
                )

            # Refresh GET succeeded: verify binding on refreshed session
            try:
                bind_session(
                    profile=profile,
                    profile_epoch=current_profile_epoch,
                    session=refreshed_session,
                    sources=collection.sources,
                    required_repository=repository,
                    required_branch=starting_branch,
                )
            except OctodotError as err:
                # Wrong binding on refreshed session: mutation already occurred, so UNKNOWN, not BLOCKED
                if store is not None and hasattr(store, "update_operation_evidence_flags"):
                    try:
                        store.update_operation_evidence_flags(
                            operation_id,
                            accepted_identity_unverified=True,
                            effect_observed=False,
                            attribution="",
                            fence=fence,
                        )
                    except Exception:
                        pass
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.UNKNOWN,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=err.code,
                    data={
                        "operation_id": operation_id,
                        "session": session_name,
                        "api_accepted": True,
                        "accepted_identity_unverified": True,
                        "effect_observed": False,
                        "attribution": "",
                        "ui_verified": False,
                        "error": f"Binding verification failed on refreshed session: {err}",
                    },
                )

            # Confirmed verified creation!
            if store is not None:
                try:
                    store.transition_operation_state(
                        operation_id,
                        OperationState.EFFECT_OBSERVED,
                        api_accepted=True,
                        effect_observed=True,
                        attribution="confirmed",
                        fence=fence,
                    )
                except Exception as exc:
                    # Store persistence failure: do NOT report success or "confirmed".
                    err_code = (
                        getattr(exc, "code", None)
                        or getattr(exc, "error_code", None)
                        or ErrorCode.INTERNAL_ERROR
                    )
                    persisted_rec = None
                    try:
                        persisted_rec = store.get_operation(operation_id)
                    except Exception:
                        pass
                    effect_obs = persisted_rec.effect_observed if persisted_rec else False
                    attr = persisted_rec.attribution if persisted_rec else ""
                    return ActionResult.create(
                        action_id=action_id,
                        op=op,
                        status=ActionResultStatus.ERROR,
                        exit_code=EXIT_FATAL_READ_OR_LOCAL,
                        error_code=err_code,
                        data={
                            "operation_id": operation_id,
                            "session": session_name,
                            "api_accepted": True,
                            "effect_observed": effect_obs,
                            "attribution": attr,
                            "ui_verified": False,
                            "error": f"Store transition to EFFECT_OBSERVED failed: {exc}",
                        },
                    )

            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.OK,
                exit_code=EXIT_OK,
                data={
                    "operation_id": operation_id,
                    "session": session_to_dict(refreshed_session),
                    "api_accepted": True,
                    "effect_observed": True,
                    "attribution": "confirmed",
                    "ui_verified": False,
                },
            )

        # Case B: Clear 4xx rejection
        elif 400 <= outcome.status < 500:
            err_code = outcome.sanitized_error_code or ErrorCode.INVALID_INPUT
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.REJECTED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err_code,
                data={
                    "operation_id": operation_id,
                    "api_accepted": False,
                    "effect_observed": False,
                    "attribution": "",
                    "ui_verified": False,
                },
            )

        # Case C: 5xx, timeout, disconnect or uncertain effect
        else:
            # Uncertain outcome -> unknown; bounded read-only reconciliation
            effect_obs = False
            attr = ""
            if reconciler is not None:
                try:
                    reconcile_res = reconciler.reconcile(operation_id, read_api=api, scans=1)
                    if reconcile_res.record:
                        effect_obs = reconcile_res.record.effect_observed
                        attr = reconcile_res.record.attribution
                except Exception:
                    pass

            err_code = outcome.sanitized_error_code or ErrorCode.UNCERTAIN_EFFECT
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.UNKNOWN,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err_code,
                data={
                    "operation_id": operation_id,
                    "api_accepted": False,
                    "effect_observed": effect_obs,
                    "attribution": attr,
                    "ui_verified": False,
                },
            )
