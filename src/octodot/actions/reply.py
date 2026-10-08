"""Chats reply action handler implementing exact approved replies.

Standard library only. Compatible with Python 3.10+.
Follows the mutation pipeline:
  literal action -> execution eligibility -> fresh full rescan via ReadService
  -> S04 binding/projection checks -> S05 prepare_action (hashes)
  -> GrantVerifier.verify (S05; DisabledGrantVerifier default blocks)
  -> journal gating (fence, unresolved intents, operation id/hash)
  -> final session re-check -> journal.begin_dispatch
  -> API mutation method (redeems ticket) exactly once
  -> journal.record_outcome -> bounded read-only reconciliation (S08)
  -> ActionResult with api_accepted / effect_observed / attribution / ui_verified separate.

Any blocker before dispatch => zero POST and status blocked.
Uncertain => unknown, never retried.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from octodot.api import JulesClient, compute_mutation_request_hash
from octodot.authorization import DisabledGrantVerifier
from octodot.contracts import (
    ActionHandler,
    Clock,
    canonical_hash,
)
from octodot.transport import SystemClock
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    ErrorCode,
    OctodotError,
)
from octodot.identity import (
    extract_session_branch,
    extract_session_repository,
    extract_session_source,
)
from octodot.journal import Journal
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    Binding,
    CandidateBundle,
    Coverage,
    OperationRecord,
    OperationState,
    PreparedAction,
    SessionRecord,
    TransportOutcome,
    VerifiedGrant,
)
from octodot.preparation import prepare_action
from octodot.reads import ReadService
from octodot.reconciliation import Reconciler

# =====================================================================
# Conservative Detectors
# =====================================================================

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub Tokens
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # Google API Key
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    # OpenAI / Anthropic
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"),
    # Slack
    re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"),
    # AWS Access Key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Bearer tokens
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{16,}\b"),
    # Private Key Header
    re.compile(r"-----BEGIN\s+[A-Z\s]+PRIVATE\s+KEY-----"),
    # Generic key / token assignments
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{12,}['\"]?"
    ),
)

_CONSEQUENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:deploy\s+to\s+production|deploy\s+to\s+prod)\b"),
    re.compile(r"(?i)\b(?:trigger\s+deploy|auto_create_pr)\b"),
    re.compile(
        r"(?i)\b(?:create\s+(?:a\s+)?pull\s+request|publish\s+pr|delete\s+repo(?:sitory)?)\b"
    ),
    re.compile(r"(?i)\b(?:drop\s+database|destroy\s+infrastructure)\b"),
    re.compile(r"(?i)\b(?:approve_plan|approvePlan)\b"),
)

_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"<[A-Za-z0-9_ -]+>")


def detect_secrets(text: str) -> str | None:
    """Conservative detector for secrets, API keys, and tokens.

    Returns description if pattern matched, else None.
    """
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            return f"Matched secret pattern: {pat.pattern}"
    return None


def detect_unauthorized_consequential(text: str) -> str | None:
    """Conservative detector for consequential directives in chat reply.

    Returns description if pattern matched, else None.
    """
    for pat in _CONSEQUENTIAL_PATTERNS:
        m = pat.search(text)
        if m:
            return f"Unauthorized consequential directive: {m.group(0)}"
    return None


def scan_placeholders(obj: Any) -> bool:
    """Check for template placeholder tokens in strings or structures."""
    if isinstance(obj, str):
        if _PLACEHOLDER_PATTERN.search(obj):
            return True
        if any(tok in obj for tok in ("REPLACE_ME", "TODO", "CHANGEME")):
            return True
    elif isinstance(obj, Mapping):
        return any(scan_placeholders(k) or scan_placeholders(v) for k, v in obj.items())
    elif isinstance(obj, (list, tuple)):
        return any(scan_placeholders(item) for item in obj)
    return False


def compute_candidate_bundle_hash(bundle: CandidateBundle) -> str:
    """Compute deterministic canonical hash for a CandidateBundle."""
    bundle_dict = {
        "messages": list(bundle.messages),
        "selected_activity_id": bundle.selected_activity_id,
        "last_message_text": bundle.last_message_text,
    }
    return canonical_hash(bundle_dict)


def clean_session_name(session: str) -> str:
    """Normalize session name to sessions/<id>."""
    clean = session.strip()
    if clean.startswith("/v1alpha/"):
        clean = clean[len("/v1alpha/") :]
    if clean.endswith(":sendMessage"):
        clean = clean[: -len(":sendMessage")]
    if clean.endswith(":approvePlan"):
        clean = clean[: -len(":approvePlan")]
    if not clean.startswith("sessions/"):
        clean = f"sessions/{clean}"
    return clean


class _ReconciliationReadAdapter:
    """Adapter unwrapping (records, next_token) from JulesClient.activities_list for Reconciler."""

    def __init__(self, api: Any) -> None:
        self._api = api

    def activities_list(self, session_name: str) -> tuple[Any, ...]:
        if not hasattr(self._api, "activities_list"):
            return ()
        res = self._api.activities_list(session_name)
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], (list, tuple)):
            return tuple(res[0])
        if isinstance(res, (list, tuple)):
            return tuple(res)
        return ()


# =====================================================================
# ChatsReplyHandler
# =====================================================================


class ChatsReplyHandler:
    """Handler for 'chats.reply' mutation operations."""

    def can_handle(self, op: str) -> bool:
        return op == "chats.reply"

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
    ) -> ActionResult:
        """Execute one exact approved reply under strict gating.

        Full pipeline:
        1. Action eligibility and structural checks (enabled, placeholders, text)
        2. Conservative security checks (secrets, consequential content, publication scope)
        3. Fresh full rescan via ReadService (never reused selection)
        4. S04 binding and projection checks (history coverage, branch drift, bundle ambiguity)
        5. S05 prepare_action (canonical hashes)
        6. GrantVerifier.verify (S05; DisabledGrantVerifier default blocks)
        7. Journal gating (recovery fence, unresolved intents, prepare state)
        8. Final session re-check
        9. journal.begin_dispatch
        10. API mutation method (exactly one POST, ticket redeemed)
        11. journal.record_outcome
        12. Bounded read-only reconciliation (S08)
        13. ActionResult with api_accepted, effect_observed, attribution, ui_verified separate.
        """
        action_id = str(action.get("id", "act-chats-reply"))
        op = str(action.get("op", "chats.reply"))

        # -------------------------------------------------------------
        # Step 1: Execution eligibility and literal action checks
        # -------------------------------------------------------------
        if op != "chats.reply":
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": f"ChatsReplyHandler cannot handle operation '{op}'"},
            )

        if not action.get("enabled", False):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TEMPLATE_DISABLED,
                data={"error": f"Mutation action '{action_id}' is disabled"},
            )

        if scan_placeholders(action):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.PLACEHOLDER_PRESENT,
                data={"error": "Placeholder token detected in action specification"},
            )

        target_raw = action.get("target")
        if not target_raw or not isinstance(target_raw, str) or not target_raw.strip():
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Action target session must be a non-empty string"},
            )
        session_name = clean_session_name(target_raw)

        payload = action.get("payload")
        if not isinstance(payload, dict):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Action payload must be an object"},
            )

        # Extract text preserving exact bytes
        raw_text = payload.get("prompt")
        if raw_text is None:
            raw_text = payload.get("text")
        if raw_text is None or not isinstance(raw_text, str) or not raw_text.strip():
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Reply message content is missing, blank, or malformed"},
            )
        exact_approved_text = raw_text

        # -------------------------------------------------------------
        # Step 2: Conservative security and publication checks
        # -------------------------------------------------------------
        secret_match = detect_secrets(exact_approved_text)
        if secret_match:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.AUTH_DENIED,
                data={"error": f"Secret-pattern detected in reply text: {secret_match}"},
            )

        pub_scope = str(action.get("publication_scope", "none"))
        if pub_scope != "none":
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.GRANT_INVALID,
                data={"error": f"Publication scope '{pub_scope}' unsupported for chats.reply"},
            )

        conseq_match = detect_unauthorized_consequential(exact_approved_text)
        if conseq_match:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.AUTH_DENIED,
                data={"error": f"Unauthorized consequential directive: {conseq_match}"},
            )

        # -------------------------------------------------------------
        # Step 3: Fresh full rescan via ReadService
        # -------------------------------------------------------------
        read_service: ReadService | None = context.get("read_service")
        if read_service is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "ReadService missing from context"},
            )

        try:
            chats_coll = read_service.chats({"session": session_name}, fresh=True)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": str(err)},
            )
        except Exception as exc:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.TRANSPORT_ERROR,
                data={"error": str(exc)},
            )

        session_rec = chats_coll.session
        if session_rec is None:
            try:
                session_rec = read_service.api.sessions_get(session_name)
            except OctodotError as err:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=err.code,
                    data={"error": str(err)},
                )
            except Exception as exc:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.TRANSPORT_ERROR,
                    data={"error": str(exc)},
                )

        # Check session terminal / stale state
        if session_rec.state.upper() in ("COMPLETED", "FAILED", "CANCELLED", "TERMINATED"):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.OPERATION_CONFLICT,
                data={"error": f"Session '{session_name}' is in terminal state '{session_rec.state}'"},
            )

        preconditions = action.get("preconditions") or {}
        expected_state = preconditions.get("state")
        if expected_state and session_rec.state != expected_state:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.OPERATION_CONFLICT,
                data={"error": f"Session state mismatch: expected '{expected_state}', observed '{session_rec.state}'"},
            )

        # -------------------------------------------------------------
        # Step 4: S04 Binding and projection checks
        # -------------------------------------------------------------
        coverage = chats_coll.coverage
        if not coverage.complete:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.PARTIAL_COVERAGE,
                coverage=coverage,
                data={"error": "Incomplete activity history coverage", "reasons": list(coverage.reasons)},
            )

        bundle = chats_coll.candidate_bundle
        if bundle.has_ambiguity:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.IDENTITY_AMBIGUOUS,
                data={"error": f"Candidate bundle has ambiguity: {list(bundle.ambiguity_reasons)}"},
            )

        if not bundle.messages:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Candidate bundle contains no agent messages to reply to"},
            )

        # Check bundle hash precondition
        bundle_h = compute_candidate_bundle_hash(bundle)
        for h_key in ("candidate_bundle_hash", "feedback_bundle_hash", "bundle_hash"):
            expected_bh = preconditions.get(h_key)
            if expected_bh and expected_bh != bundle_h:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.BINDING_MISMATCH,
                    data={"error": f"Feedback bundle hash mismatch: expected '{expected_bh}', observed '{bundle_h}'"},
                )

        # Verify repository binding
        sources_list = ()
        try:
            sources_coll, _ = read_service.collect(scope={"scope": "all"}, limits={"max_pages": 10})
            sources_list = sources_coll.sources
        except Exception:
            pass

        observed_repo = extract_session_repository(session_rec, sources_list)
        plan = context.get("plan") or {}
        scope = plan.get("scope") or {}
        expected_repo = preconditions.get("repository") or scope.get("repository")
        if expected_repo and observed_repo != expected_repo:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BINDING_MISMATCH,
                data={"error": f"Repository mismatch: expected '{expected_repo}', observed '{observed_repo}'"},
            )

        # Verify starting branch binding (affirmative evidence required)
        observed_branch = extract_session_branch(session_rec)
        if observed_branch is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BRANCH_UNVERIFIED,
                data={"error": f"Session '{session_name}' starting branch is absent/unverified"},
            )

        expected_branch = preconditions.get("branch") or scope.get("branch")
        if expected_branch and observed_branch != expected_branch:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BINDING_MISMATCH,
                data={"error": f"Branch drift: expected '{expected_branch}', observed '{observed_branch}'"},
            )

        # Manual identical message check: reject second attempt if identical text exists in user messages
        for act in chats_coll.activities:
            originator = getattr(act, "originator", "") or (
                act.get("originator") if isinstance(act, dict) else ""
            )
            if originator and str(originator).upper() in ("USER", "HUMAN", "MANUAL"):
                act_text = (
                    getattr(act, "text", None)
                    or getattr(act, "prompt", None)
                    or (act.get("text") if isinstance(act, dict) else None)
                    or (act.get("prompt") if isinstance(act, dict) else None)
                )
                if act_text and str(act_text) == exact_approved_text:
                    return ActionResult.create(
                        action_id=action_id,
                        op=op,
                        status=ActionResultStatus.BLOCKED,
                        exit_code=EXIT_MUTATION_BLOCKED,
                        error_code=ErrorCode.OPERATION_CONFLICT,
                        data={"error": "Identical user message already exists in conversation"},
                    )

        # -------------------------------------------------------------
        # Step 5: S05 prepare_action (canonical hashes)
        # -------------------------------------------------------------
        profile = str(context.get("profile") or plan.get("profile") or "default")
        profile_epoch = int(context.get("profile_epoch", 0))
        resolved_source = (
            preconditions.get("source")
            or extract_session_source(session_rec)
            or f"sources/github/{observed_repo or 'UNKNOWN'}"
        )

        action_binding = Binding(
            profile=profile,
            profile_epoch=profile_epoch,
            source=resolved_source,
            repository=observed_repo or "",
            starting_branch=observed_branch,
            session=session_name,
        )

        primary_target = f"/v1alpha/{session_name}:sendMessage"
        req_hash = compute_mutation_request_hash(primary_target, {"prompt": exact_approved_text})

        try:
            prepared_action = prepare_action(
                action=action,
                plan=plan,
                current_profile_epoch=profile_epoch,
                read_service=read_service,
                binding=action_binding,
                source=resolved_source,
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
                data={"error": str(err)},
            )

        # Verify context hash precondition if specified
        if preconditions.get("context_hash") and preconditions["context_hash"] != prepared_action.context_hash:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.BINDING_MISMATCH,
                data={"error": f"Context hash mismatch: expected '{preconditions['context_hash']}', computed '{prepared_action.context_hash}'"},
            )

        # -------------------------------------------------------------
        # Step 6: GrantVerifier.verify (S05; DisabledGrantVerifier default)
        # -------------------------------------------------------------
        verifier = (
            context.get("grant_verifier")
            or context.get("verifier")
            or DisabledGrantVerifier()
        )
        auth_ref = str(action.get("authorization_ref", ""))

        grant_res = verifier.verify(
            reference=auth_ref,
            prepared_action=prepared_action,
            current_profile_epoch=profile_epoch,
        )
        if hasattr(grant_res, "code"):
            # GrantBlocker returned
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=grant_res.code,
                data={"error": f"Grant authorization rejected: {grant_res.reason}"},
            )
        if not isinstance(grant_res, VerifiedGrant):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.GRANT_INVALID,
                data={"error": "Verifier did not return a VerifiedGrant"},
            )

        # Strict publication scope / effect check on grant
        if grant_res.publication_scope != prepared_action.publication_scope:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.GRANT_INVALID,
                data={"error": f"Grant publication effect mismatch: grant '{grant_res.publication_scope}', action '{prepared_action.publication_scope}'"},
            )

        # -------------------------------------------------------------
        # Step 7: Journal gating (fence, unresolved intents, prepare state)
        # -------------------------------------------------------------
        clock = context.get("clock")
        fence = context.get("fence")
        store = context.get("store")
        journal = context.get("journal")
        if journal is None and store is not None:
            journal = Journal(store=store, verifier=verifier, fence=fence, clock=clock)

        if journal is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "Journal missing from context"},
            )

        operation_id = str(action.get("operation_id", ""))

        # Check existing operation in journal
        existing_rec = journal.get_record(operation_id)
        if existing_rec is not None:
            if existing_rec.state in (OperationState.UNKNOWN, OperationState.DISPATCHING):
                st = (
                    ActionResultStatus.UNKNOWN
                    if existing_rec.state == OperationState.UNKNOWN
                    else ActionResultStatus.BLOCKED
                )
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=st,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=ErrorCode.UNRESOLVED_INTENT,
                    data={
                        "error": f"Existing intent in unresolved state '{existing_rec.state.value}'",
                        "api_accepted": existing_rec.api_accepted,
                        "effect_observed": existing_rec.effect_observed,
                        "attribution": existing_rec.attribution,
                        "ui_verified": existing_rec.ui_verified,
                        "operation_id": operation_id,
                    },
                )
            if existing_rec.state in (OperationState.ACCEPTED, OperationState.EFFECT_OBSERVED):
                # Never a second dispatch attempt on restart or replay!
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.OK,
                    exit_code=EXIT_OK,
                    data={
                        "api_accepted": existing_rec.api_accepted,
                        "effect_observed": existing_rec.effect_observed,
                        "attribution": existing_rec.attribution,
                        "ui_verified": existing_rec.ui_verified,
                        "operation_id": operation_id,
                        "session": session_name,
                        "prompt": exact_approved_text,
                    },
                )
            if existing_rec.state == OperationState.BLOCKED_BEFORE_DISPATCH:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.BLOCKED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=existing_rec.error_code or ErrorCode.AUTH_DENIED,
                    data={"error": f"Operation '{operation_id}' was previously blocked before dispatch"},
                )
            if existing_rec.state == OperationState.REJECTED:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.REJECTED,
                    exit_code=EXIT_MUTATION_BLOCKED,
                    error_code=existing_rec.error_code or ErrorCode.INVALID_INPUT,
                    data={"error": f"Operation '{operation_id}' was previously rejected"},
                )

        pred_op_id = action.get("predecessor_operation_id")
        try:
            prep_record = journal.prepare(
                prepared_action,
                grant=grant_res,
                predecessor_operation_id=pred_op_id,
                authorization_ref=auth_ref,
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": str(err)},
            )

        if prep_record.state != OperationState.PREPARED:
            # Dispatch ONLY when returned state is PREPARED
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=prep_record.error_code or ErrorCode.AUTH_DENIED,
                data={
                    "error": f"Journal prepare recorded state '{prep_record.state.value}' (not PREPARED)",
                    "operation_id": operation_id,
                },
            )

        # -------------------------------------------------------------
        # Step 8: Final session re-check
        # -------------------------------------------------------------
        client_in_ctx: JulesClient | None = context.get("client") or context.get("api")
        if client_in_ctx is None:
            transport = context.get("transport")
            if transport is None:
                return ActionResult.create(
                    action_id=action_id,
                    op=op,
                    status=ActionResultStatus.ERROR,
                    exit_code=EXIT_FATAL_READ_OR_LOCAL,
                    error_code=ErrorCode.INTERNAL_ERROR,
                    data={"error": "Transport missing from context"},
                )
            client = JulesClient(transport=transport, ticket_authority=journal, clock=clock)
        else:
            client = JulesClient(
                transport=client_in_ctx.transport,
                ticket_authority=journal,
                clock=client_in_ctx.clock,
            )

        try:
            final_sess = client.sessions_get(session_name)
        except Exception as exc:
            journal.block_before_dispatch(
                operation_id, ErrorCode.SESSION_NOT_FOUND, str(exc)
            )
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.SESSION_NOT_FOUND,
                data={"error": f"Final session re-check failed: {exc}"},
            )

        if (
            final_sess.state != session_rec.state
            or final_sess.update_time != session_rec.update_time
            or final_sess.state.upper() in ("COMPLETED", "FAILED", "CANCELLED", "TERMINATED")
        ):
            journal.block_before_dispatch(
                operation_id,
                ErrorCode.OPERATION_CONFLICT,
                "Session state drifted during final check",
            )
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=ErrorCode.OPERATION_CONFLICT,
                data={"error": "Session state drifted during final re-check before dispatch"},
            )

        # -------------------------------------------------------------
        # Step 9: journal.begin_dispatch
        # -------------------------------------------------------------
        try:
            ticket = journal.begin_dispatch(operation_id, prepared_action.request_hash)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.BLOCKED,
                exit_code=EXIT_MUTATION_BLOCKED,
                error_code=err.code,
                data={"error": str(err)},
            )

        # -------------------------------------------------------------
        # Step 10: API mutation method (exactly one POST, ticket redeemed)
        # -------------------------------------------------------------
        outgoing_body = {"prompt": exact_approved_text}
        try:
            mutation_resp = client.sessions_send_message(ticket, session_name, outgoing_body)
            # -------------------------------------------------------------
            # Step 11: journal.record_outcome
            # -------------------------------------------------------------
            op_record = journal.record_outcome(
                ticket,
                mutation_resp,
                evidence={"prompt": exact_approved_text, "payload": outgoing_body},
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
                    evidence={"prompt": exact_approved_text, "payload": outgoing_body, "error": str(exc)},
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

        # -------------------------------------------------------------
        # Step 12: Bounded read-only reconciliation (S08)
        # -------------------------------------------------------------
        reconciler = context.get("reconciler")
        rec_api = _ReconciliationReadAdapter(client)
        if reconciler is None and store is not None:
            reconciler = Reconciler(store=store, read_api=rec_api, clock=clock, fence=fence)
        if reconciler is not None and op_record.state in (OperationState.ACCEPTED, OperationState.UNKNOWN):
            try:
                rec_res = reconciler.reconcile(operation_id, read_api=rec_api, scans=1)
                if rec_res.record is not None:
                    op_record = rec_res.record
            except Exception:
                pass

        # -------------------------------------------------------------
        # Step 13: ActionResult with distinct flags
        # -------------------------------------------------------------
        api_accepted = op_record.api_accepted
        effect_observed = op_record.effect_observed
        attribution = op_record.attribution
        ui_verified = op_record.ui_verified

        if op_record.state in (OperationState.ACCEPTED, OperationState.EFFECT_OBSERVED):
            status = ActionResultStatus.OK
            exit_code = EXIT_OK
            err_code = None
        elif op_record.state == OperationState.UNKNOWN:
            status = ActionResultStatus.UNKNOWN
            exit_code = EXIT_MUTATION_BLOCKED
            err_code = op_record.error_code or ErrorCode.TRANSPORT_ERROR
        elif op_record.state == OperationState.REJECTED:
            status = ActionResultStatus.REJECTED
            exit_code = EXIT_MUTATION_BLOCKED
            err_code = op_record.error_code or ErrorCode.INVALID_INPUT
        else:
            status = ActionResultStatus.UNKNOWN
            exit_code = EXIT_MUTATION_BLOCKED
            err_code = op_record.error_code or ErrorCode.TRANSPORT_ERROR

        data: dict[str, Any] = {
            "operation_id": operation_id,
            "session": session_name,
            "prompt": exact_approved_text,
            "api_accepted": api_accepted,
            "effect_observed": effect_observed,
            "attribution": attribution,
            "ui_verified": ui_verified,
            "state": op_record.state.value,
        }

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=status,
            exit_code=exit_code,
            error_code=err_code,
            data=data,
        )


# Alias
ReplyActionHandler = ChatsReplyHandler
