"""Deterministic lifecycle, conversation, plan, and failure projections.

Standard library only. Compatible with Python 3.10+.
Pure functions over models.SourceRecord, SessionRecord, and ActivityRecord; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import enum
import re
from typing import Any, Iterable, Mapping, Sequence

from octodot.contracts import canonical_hash
from octodot.errors import ErrorCode, OctodotError
from octodot.identity import extract_session_repository, extract_session_source
from octodot.models import (
    ActivityRecord,
    Binding,
    CandidateBundle,
    Coverage,
    LifecycleBucket,
    SessionRecord,
    SourceRecord,
)

# RFC3339 strict pattern capturing nanosecond precision and timezone offset
_RFC3339_REGEX = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:([Zz])|([+-]\d{2}):?(\d{2}))?$"
)

# Standard lifecycle state mappings
_OPEN_STATES = frozenset({
    "ACTIVE",
    "PLANNING",
    "AWAITING_USER_FEEDBACK",
    "IN_PROGRESS",
    "PAUSED",
    "QUEUED",
    "PENDING",
    "RUNNING",
})

_COMPLETED_STATES = frozenset({
    "COMPLETED",
    "SUCCEEDED",
    "SUCCESS",
})

_FAILED_STATES = frozenset({
    "FAILED",
    "ERROR",
    "CANCELLED",
})


def parse_rfc3339_nanoseconds(ts_str: str) -> int:
    """Parse RFC3339 timestamp string into exact integer nanoseconds since UTC epoch.

    Avoids float rounding or precision loss by doing exact integer arithmetic.
    Nanosecond fraction is right-padded or truncated to exactly 9 digits.
    """
    if not isinstance(ts_str, str):
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Timestamp must be a string, got {type(ts_str).__name__}",
        )

    match = _RFC3339_REGEX.match(ts_str.strip())
    if not match:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Malformed RFC3339 timestamp: '{ts_str}'",
        )

    year, month, day, hour, minute, second, frac, z, tz_hr, tz_min = match.groups()
    dt = datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
        tzinfo=timezone.utc,
    )
    epoch_sec = int(dt.timestamp())

    # Adjust for timezone offset if present
    if tz_hr and tz_min:
        hrs = int(tz_hr)
        mins = int(tz_min)
        offset_sec = hrs * 3600 + (mins * 60 if hrs >= 0 else -mins * 60)
        epoch_sec -= offset_sec

    # Nanosecond fraction (up to 9 decimal digits)
    nanos = 0
    if frac:
        nanos = int(frac[:9].ljust(9, "0"))

    return epoch_sec * 1_000_000_000 + nanos


# =====================================================================
# 1. Lifecycle Projection
# =====================================================================

@dataclass(frozen=True, slots=True)
class LifecycleProjection:
    """Exclusive lifecycle categorization of a session.

    - bucket: Exactly one of OPEN, COMPLETED, FAILED, UNKNOWN.
    - is_terminal: Derived as COMPLETED or FAILED.
    - blocks_writes: True if state is UNKNOWN (blocks state-dependent writes).
    - raw_state: Verbatim state string preserved without loss.
    """

    bucket: LifecycleBucket
    raw_state: str
    is_terminal: bool
    blocks_writes: bool


def project_lifecycle(
    session_or_state: SessionRecord | str,
) -> LifecycleProjection:
    """Project session state into exclusive LifecycleBucket.

    Unknown states are preserved verbatim, mapped to UNKNOWN bucket, and
    flagged as blocking state-dependent writes.
    """
    if isinstance(session_or_state, SessionRecord):
        raw_state = session_or_state.state
    elif isinstance(session_or_state, str):
        raw_state = session_or_state
    else:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Expected SessionRecord or str, got {type(session_or_state).__name__}",
        )

    upper_state = raw_state.strip().upper()

    if upper_state in _COMPLETED_STATES:
        bucket = LifecycleBucket.COMPLETED
    elif upper_state in _FAILED_STATES:
        bucket = LifecycleBucket.FAILED
    elif upper_state in _OPEN_STATES:
        bucket = LifecycleBucket.OPEN
    else:
        bucket = LifecycleBucket.UNKNOWN

    is_terminal = bucket.is_terminal
    blocks_writes = bucket == LifecycleBucket.UNKNOWN

    return LifecycleProjection(
        bucket=bucket,
        raw_state=raw_state,
        is_terminal=is_terminal,
        blocks_writes=blocks_writes,
    )


# =====================================================================
# 2. Activity & Plan Projections
# =====================================================================

class ActivityKind(str, enum.Enum):
    """Activity kind classification."""

    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    PLAN_GENERATED = "plan_generated"
    PLAN_APPROVED = "plan_approved"
    COMMAND_EXECUTION = "command_execution"
    CODE_EDIT = "code_edit"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ActivityProjection:
    """Projected activity preserving unknown activity types without guessing semantics."""

    activity_id: str
    kind: ActivityKind
    raw_type: str
    is_unknown_type: bool
    create_time: str | None
    nanos: int | None
    originator: str | None
    message_text: str | None
    raw_data: tuple[tuple[str, Any], ...] = ()


def _extract_activity_fields(
    act: ActivityRecord | dict[str, Any],
) -> tuple[str, str, str | None, str | None, str | None, dict[str, Any]]:
    """Extract standard fields from ActivityRecord or dict."""
    if isinstance(act, ActivityRecord):
        act_id = act.id or act.name.split("/")[-1]
        raw_type = act.activity_type
        create_time = act.create_time
        originator = act.originator
        uf = dict(act.unknown_fields)
        text = uf.get("text") or uf.get("message") or uf.get("prompt") or uf.get("content")
        if text is None and isinstance(uf.get("userMessage"), Mapping):
            text = uf["userMessage"].get("text")
        if text is None and isinstance(uf.get("agentMessage"), Mapping):
            text = uf["agentMessage"].get("text")
        raw_dict = {
            "name": act.name,
            "id": act.id,
            "type": act.activity_type,
            "createTime": act.create_time,
            "originator": act.originator,
            **uf,
        }
        return act_id, raw_type, create_time, originator, text, raw_dict
    elif isinstance(act, Mapping):
        act_id = str(act.get("id") or act.get("name", "").split("/")[-1])
        raw_type = str(act.get("type") or act.get("activity_type", "UNKNOWN"))
        create_time = act.get("createTime") or act.get("create_time")
        originator = act.get("originator")
        text = act.get("text") or act.get("message") or act.get("prompt") or act.get("content")
        if text is None and isinstance(act.get("userMessage"), Mapping):
            text = act["userMessage"].get("text")
        if text is None and isinstance(act.get("agentMessage"), Mapping):
            text = act["agentMessage"].get("text")
        return act_id, raw_type, create_time, originator, text, dict(act)
    else:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Expected ActivityRecord or Mapping, got {type(act).__name__}",
        )


def project_activity(
    activity: ActivityRecord | dict[str, Any],
) -> ActivityProjection:
    """Project an activity, classifying kind and preserving unknown types without guessing."""
    act_id, raw_type, create_time, originator, text, raw_dict = _extract_activity_fields(activity)

    upper_type = raw_type.strip().upper()
    upper_orig = (originator or "").strip().upper()

    is_unknown = False
    if upper_type in ("USER_MESSAGE", "USERMESSAGE", "USER_PROMPT") or upper_orig in ("USER", "HUMAN"):
        kind = ActivityKind.USER_MESSAGE
    elif upper_type in ("AGENT_MESSAGE", "AGENTMESSAGE", "AGENT_RESPONSE") or upper_orig in ("AGENT", "ASSISTANT", "MODEL"):
        kind = ActivityKind.AGENT_MESSAGE
    elif upper_type in ("PLAN_GENERATED", "PLANGENERATED", "PLAN", "PLAN_CREATED", "PLAN_PROPOSED"):
        kind = ActivityKind.PLAN_GENERATED
    elif upper_type in ("PLAN_APPROVED", "PLANAPPROVED"):
        kind = ActivityKind.PLAN_APPROVED
    elif upper_type in ("COMMAND_EXECUTION", "COMMANDEXECUTION", "EXEC_COMMAND"):
        kind = ActivityKind.COMMAND_EXECUTION
    elif upper_type in ("CODE_EDIT", "CODEEDIT"):
        kind = ActivityKind.CODE_EDIT
    else:
        kind = ActivityKind.UNKNOWN
        is_unknown = True

    nanos = None
    if create_time:
        try:
            nanos = parse_rfc3339_nanoseconds(create_time)
        except OctodotError:
            nanos = None

    return ActivityProjection(
        activity_id=act_id,
        kind=kind,
        raw_type=raw_type,
        is_unknown_type=is_unknown,
        create_time=create_time,
        nanos=nanos,
        originator=originator,
        message_text=text,
        raw_data=tuple(raw_dict.items()),
    )


@dataclass(frozen=True, slots=True)
class PlanProjection:
    """Plan projection retaining exact plan content hash and latest plan ID."""

    latest_plan_id: str | None
    latest_plan_hash: str | None
    is_approved: bool
    requires_approval: bool
    plan_activity_id: str | None = None
    raw_plan: tuple[tuple[str, Any], ...] = ()


def project_plan(
    activities: Sequence[ActivityRecord | dict[str, Any]] = (),
    session: SessionRecord | dict[str, Any] | None = None,
) -> PlanProjection:
    """Project latest plan proposal and approval state from activities.

    Computes canonical_hash over the exact approved/proposed plan content.
    """
    requires_approval = True
    if isinstance(session, SessionRecord) and session.require_plan_approval is not None:
        requires_approval = session.require_plan_approval
    elif isinstance(session, Mapping) and "requirePlanApproval" in session:
        requires_approval = bool(session["requirePlanApproval"])

    latest_plan_id: str | None = None
    latest_plan_hash: str | None = None
    latest_act_id: str | None = None
    raw_plan_dict: dict[str, Any] = {}
    is_approved = False

    approved_plan_ids: set[str] = set()

    # Scan for plans and approvals
    for act in activities:
        proj = project_activity(act)
        act_dict = dict(proj.raw_data)

        if proj.kind == ActivityKind.PLAN_APPROVED:
            p_id = act_dict.get("planId") or act_dict.get("plan_id")
            if p_id:
                approved_plan_ids.add(str(p_id))
            else:
                is_approved = True

        elif proj.kind == ActivityKind.PLAN_GENERATED:
            latest_act_id = proj.activity_id
            plan_content = act_dict.get("plan") or act_dict.get("planContent") or act_dict
            latest_plan_id = str(act_dict.get("planId") or act_dict.get("plan_id") or proj.activity_id)
            latest_plan_hash = canonical_hash(plan_content)
            raw_plan_dict = act_dict

    if latest_plan_id and latest_plan_id in approved_plan_ids:
        is_approved = True

    return PlanProjection(
        latest_plan_id=latest_plan_id,
        latest_plan_hash=latest_plan_hash,
        is_approved=is_approved,
        requires_approval=requires_approval,
        plan_activity_id=latest_act_id,
        raw_plan=tuple(raw_plan_dict.items()),
    )


# =====================================================================
# 3. CandidateBundle / Conversation Projection
# =====================================================================

def project_candidate_bundle(
    activities: Sequence[ActivityRecord | dict[str, Any]] = (),
    coverage: Coverage | None = None,
    prior_session: SessionRecord | dict[str, Any] | None = None,
    current_session: SessionRecord | dict[str, Any] | None = None,
) -> CandidateBundle:
    """Project candidate feedback bundle from conversation activities.

    Rules:
    - Retains all relevant agent messages since the last user message.
    - No question-mark heuristics: messages without '?' are included.
    - Multi-message feedback: multiple agent messages are preserved in chronological order.
    - Flags tied timestamps using exact integer nanoseconds.
    - Flags incomplete history (coverage.complete is False).
    - Flags manual replies (user replied since last agent turn).
    - Flags before/after session drift.
    """
    ambiguity_reasons: list[str] = []

    # 1. Coverage check
    if coverage is not None and not coverage.complete:
        ambiguity_reasons.append("incomplete_history")

    # 2. Before/after drift check
    if prior_session is not None and current_session is not None:
        p_dict = dict(prior_session.source_context) if isinstance(prior_session, SessionRecord) else dict(prior_session.get("sourceContext", {}))
        c_dict = dict(current_session.source_context) if isinstance(current_session, SessionRecord) else dict(current_session.get("sourceContext", {}))
        p_state = prior_session.state if isinstance(prior_session, SessionRecord) else prior_session.get("state")
        c_state = current_session.state if isinstance(current_session, SessionRecord) else current_session.get("state")
        p_up = prior_session.update_time if isinstance(prior_session, SessionRecord) else prior_session.get("updateTime")
        c_up = current_session.update_time if isinstance(current_session, SessionRecord) else current_session.get("updateTime")

        if p_state != c_state or p_up != c_up or p_dict != c_dict:
            ambiguity_reasons.append("session_drift_detected")

    # 3. Project and sort activities
    projected_acts = [project_activity(a) for a in activities]

    # Check for missing/unparseable timestamps
    valid_acts = []
    for a in projected_acts:
        if a.nanos is None:
            ambiguity_reasons.append("unparseable_timestamp")
        valid_acts.append(a)

    # Check tied timestamps using exact integer comparison
    nanos_seen: set[int] = set()
    for a in valid_acts:
        if a.nanos is not None:
            if a.nanos in nanos_seen:
                ambiguity_reasons.append("tied_timestamps")
                break
            nanos_seen.add(a.nanos)

    # Sort stably by nanoseconds
    sorted_acts = sorted(valid_acts, key=lambda x: (x.nanos or 0))

    # 4. Find the last user message
    last_user_idx = -1
    for idx, act in enumerate(sorted_acts):
        if act.kind == ActivityKind.USER_MESSAGE:
            last_user_idx = idx

    # If the very last message in the sequence is a user message, a manual reply occurred!
    if last_user_idx >= 0 and last_user_idx == len(sorted_acts) - 1:
        ambiguity_reasons.append("manual_reply_detected")

    # Collect agent messages since the last user message
    candidate_acts: list[ActivityProjection] = []
    if last_user_idx >= 0:
        for act in sorted_acts[last_user_idx + 1:]:
            if act.kind in (ActivityKind.AGENT_MESSAGE, ActivityKind.PLAN_GENERATED):
                candidate_acts.append(act)
    else:
        for act in sorted_acts:
            if act.kind in (ActivityKind.AGENT_MESSAGE, ActivityKind.PLAN_GENERATED):
                candidate_acts.append(act)

    messages = tuple(dict(a.raw_data) for a in candidate_acts)
    act_tuples = tuple(dict(a.raw_data) for a in candidate_acts)

    last_text = candidate_acts[-1].message_text if candidate_acts else None
    selected_act_id = candidate_acts[-1].activity_id if candidate_acts else None

    unique_reasons = tuple(sorted(set(ambiguity_reasons)))
    has_ambiguity = len(unique_reasons) > 0

    return CandidateBundle(
        messages=messages,
        activities=act_tuples,
        has_ambiguity=has_ambiguity,
        ambiguity_reasons=unique_reasons,
        selected_activity_id=selected_act_id,
        last_message_text=last_text,
    )


# =====================================================================
# 4. Failure Projection
# =====================================================================

class FailureKind(str, enum.Enum):
    """Mutually distinct failure classifications."""

    NONE = "none"
    CURRENT_FAILURE = "current_failure"
    HISTORICAL_FAILURE = "historical_failure_after_recovery"
    NONZERO_EXPECTED_TEST_COMMAND = "nonzero_expected_test_command"
    TRANSPORT_ERROR = "transport_error"
    SUSPECTED_STALL = "suspected_stall"


@dataclass(frozen=True, slots=True)
class FailureProjection:
    """Projected failure status distinguishing 5 distinct failure categories."""

    kind: FailureKind
    is_failed: bool
    description: str
    details: tuple[tuple[str, Any], ...] = ()


def project_failure(
    session: SessionRecord | dict[str, Any] | None = None,
    activities: Sequence[ActivityRecord | dict[str, Any]] = (),
    transport_error: ErrorCode | str | None = None,
    is_stalled: bool = False,
) -> FailureProjection:
    """Distinguish current failure, historical failure, nonzero expected test command, transport error, and stall."""
    # 1. Transport error
    if transport_error is not None:
        return FailureProjection(
            kind=FailureKind.TRANSPORT_ERROR,
            is_failed=True,
            description=f"Transport error: {transport_error}",
            details=(("transport_error", str(transport_error)),),
        )

    raw_state = ""
    if isinstance(session, SessionRecord):
        raw_state = session.state
    elif isinstance(session, Mapping):
        raw_state = str(session.get("state", ""))

    upper_state = raw_state.strip().upper()

    # 2. Current failure
    if upper_state in _FAILED_STATES:
        return FailureProjection(
            kind=FailureKind.CURRENT_FAILURE,
            is_failed=True,
            description=f"Session is in terminal failure state: '{raw_state}'",
            details=(("state", raw_state),),
        )

    # Check activities for nonzero commands, historical failures
    has_historical_failure = False
    has_nonzero_expected_test = False
    failed_act_details: dict[str, Any] = {}

    for act in activities:
        proj = project_activity(act)
        data = dict(proj.raw_data)

        exit_code = data.get("exitCode") or data.get("exit_code")
        expected_failure = data.get("expectedFailure") or data.get("expected_failure")
        is_test = data.get("isTestCommand") or data.get("is_test_command") or "test" in str(data.get("command", "")).lower()

        if exit_code is not None and isinstance(exit_code, int) and exit_code != 0:
            if expected_failure or is_test:
                has_nonzero_expected_test = True
            else:
                has_historical_failure = True
                failed_act_details = data

        if data.get("status") in ("FAILED", "ERROR") and not expected_failure:
            has_historical_failure = True
            failed_act_details = data

    # 3. Nonzero expected test command
    if has_nonzero_expected_test and upper_state not in _FAILED_STATES:
        return FailureProjection(
            kind=FailureKind.NONZERO_EXPECTED_TEST_COMMAND,
            is_failed=False,
            description="Nonzero output observed from expected test command; session remains healthy",
            details=(("command_exit", "nonzero_expected"),),
        )

    # 4. Historical failure after recovery
    if has_historical_failure and upper_state in ("ACTIVE", "PLANNING", "COMPLETED", "SUCCEEDED", "IN_PROGRESS"):
        return FailureProjection(
            kind=FailureKind.HISTORICAL_FAILURE,
            is_failed=False,
            description="Historical command/step failure recovered; current state is non-failed",
            details=tuple(failed_act_details.items()),
        )

    # 5. Suspected stall
    if is_stalled and upper_state in _OPEN_STATES:
        return FailureProjection(
            kind=FailureKind.SUSPECTED_STALL,
            is_failed=False,
            description="Session has exceeded inactivity threshold without terminal outcome",
            details=(("stall", True),),
        )

    return FailureProjection(
        kind=FailureKind.NONE,
        is_failed=False,
        description="No failure detected",
    )


# =====================================================================
# 5. Attention & Session Projections
# =====================================================================

class AttentionReason(str, enum.Enum):
    """Overlapping attention reason categories."""

    NEEDS_REPLY = "needs_reply"
    NEEDS_PLAN_APPROVAL = "needs_plan_approval"
    FAILED = "failed"
    SUSPECTED_STALL = "suspected_stall"
    UNKNOWN_STATE = "unknown_state"
    AMBIGUOUS_CHRONOLOGY = "ambiguous_chronology"


@dataclass(frozen=True, slots=True)
class AttentionProjection:
    """Projected attention reasons (reasons may freely overlap)."""

    needs_attention: bool
    reasons: tuple[AttentionReason, ...]

    def has_reason(self, reason: AttentionReason | str) -> bool:
        r = AttentionReason(reason) if isinstance(reason, str) else reason
        return r in self.reasons


def project_attention(
    lifecycle: LifecycleProjection,
    candidate_bundle: CandidateBundle | None = None,
    plan: PlanProjection | None = None,
    failure: FailureProjection | None = None,
    session: SessionRecord | dict[str, Any] | None = None,
) -> AttentionProjection:
    """Project attention reasons. Attention subsets may freely overlap."""
    reasons: list[AttentionReason] = []

    if lifecycle.bucket == LifecycleBucket.UNKNOWN:
        reasons.append(AttentionReason.UNKNOWN_STATE)

    if lifecycle.bucket == LifecycleBucket.FAILED or (failure and failure.is_failed):
        reasons.append(AttentionReason.FAILED)

    if failure and failure.kind == FailureKind.SUSPECTED_STALL:
        reasons.append(AttentionReason.SUSPECTED_STALL)

    raw_state = lifecycle.raw_state.strip().upper()

    # Needs reply
    if raw_state == "AWAITING_USER_FEEDBACK" or (candidate_bundle and len(candidate_bundle.messages) > 0):
        reasons.append(AttentionReason.NEEDS_REPLY)

    # Needs plan approval
    if raw_state in ("PLANNING", "AWAITING_PLAN_APPROVAL") or (plan and plan.requires_approval and not plan.is_approved and plan.latest_plan_id is not None):
        reasons.append(AttentionReason.NEEDS_PLAN_APPROVAL)

    # Ambiguous chronology
    if candidate_bundle and candidate_bundle.has_ambiguity:
        reasons.append(AttentionReason.AMBIGUOUS_CHRONOLOGY)

    unique_reasons = tuple(sorted(set(reasons), key=lambda x: x.value))
    return AttentionProjection(
        needs_attention=len(unique_reasons) > 0,
        reasons=unique_reasons,
    )


@dataclass(frozen=True, slots=True)
class SessionProjection:
    """Comprehensive projected state of a session separating distinct dimensions."""

    session_id: str
    binding: Binding | None
    lifecycle: LifecycleProjection
    attention: AttentionProjection
    candidate_bundle: CandidateBundle
    failure: FailureProjection
    plan: PlanProjection
    disposition: str
    delivery: str
    publication: str


def project_session(
    session: SessionRecord,
    activities: Sequence[ActivityRecord | dict[str, Any]] = (),
    sources: Iterable[SourceRecord] | None = None,
    coverage: Coverage | None = None,
    prior_session: SessionRecord | None = None,
    profile: str = "default",
    profile_epoch: int = 0,
    transport_error: ErrorCode | str | None = None,
    is_stalled: bool = False,
) -> SessionProjection:
    """Pure, deterministic projection of a session across lifecycle, attention, candidate, failure, and plan."""
    # 1. Lifecycle
    lifecycle = project_lifecycle(session)

    # 2. Plan
    plan = project_plan(activities, session)

    # 3. Candidate bundle
    candidate_bundle = project_candidate_bundle(
        activities=activities,
        coverage=coverage,
        prior_session=prior_session,
        current_session=session,
    )

    # 4. Failure
    failure = project_failure(
        session=session,
        activities=activities,
        transport_error=transport_error,
        is_stalled=is_stalled,
    )

    # 5. Attention (reasons may overlap)
    attention = project_attention(
        lifecycle=lifecycle,
        candidate_bundle=candidate_bundle,
        plan=plan,
        failure=failure,
        session=session,
    )

    # 6. Binding (if resolvable)
    repo = extract_session_repository(session, sources)
    source_name = extract_session_source(session)
    binding: Binding | None = None
    if repo and source_name:
        branch = None
        for k, v in session.source_context:
            if k in ("githubRepoContext", "github_repo_context") and isinstance(v, Mapping):
                branch = v.get("startingBranch") or v.get("starting_branch")
        binding = Binding(
            profile=profile,
            profile_epoch=profile_epoch,
            source=source_name,
            repository=repo,
            starting_branch=branch,
            session=session.name,
        )

    # 7. Local disposition
    if lifecycle.blocks_writes:
        disposition = "blocked_by_unknown_state"
    elif lifecycle.bucket == LifecycleBucket.FAILED:
        disposition = "terminal_failed"
    elif lifecycle.bucket == LifecycleBucket.COMPLETED:
        disposition = "terminal_completed"
    elif attention.has_reason(AttentionReason.NEEDS_REPLY):
        disposition = "waiting_for_user_feedback"
    elif attention.has_reason(AttentionReason.NEEDS_PLAN_APPROVAL):
        disposition = "waiting_for_plan_approval"
    elif attention.has_reason(AttentionReason.SUSPECTED_STALL):
        disposition = "suspected_stall"
    else:
        disposition = "in_progress"

    # 8. Delivery & Publication
    delivery = "delivered" if not candidate_bundle.has_ambiguity else "delivery_unknown"
    publication = "none"

    return SessionProjection(
        session_id=session.name,
        binding=binding,
        lifecycle=lifecycle,
        attention=attention,
        candidate_bundle=candidate_bundle,
        failure=failure,
        plan=plan,
        disposition=disposition,
        delivery=delivery,
        publication=publication,
    )
