"""Immutable domain models and data records for octodot.

Standard library only. Compatible with Python 3.10+.
Frozen dataclasses, slots, tuples for collections, no mutable defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
from typing import Any, Mapping

from octodot.errors import ErrorCode


@dataclass(frozen=True, slots=True)
class Binding:
    """Exact case-sensitive repository and branch binding."""

    profile: str
    profile_epoch: int
    source: str
    repository: str
    starting_branch: str | None = None
    session: str | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    """Bounded query coverage and completeness metadata."""

    complete: bool
    snapshot_atomic: bool = False
    pages: int = 0
    items: int = 0
    skipped_scope: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    resume_ref: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    """Candidate messages and activities preserving chronological ambiguity."""

    messages: tuple[dict[str, Any], ...] = ()
    activities: tuple[dict[str, Any], ...] = ()
    has_ambiguity: bool = False
    ambiguity_reasons: tuple[str, ...] = ()
    selected_activity_id: str | None = None
    last_message_text: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """Action prepared for grant verification and journal dispatch."""

    action: str
    operation_id: str
    binding: Binding
    payload: dict[str, Any]
    payload_hash: str
    context_hash: str
    request_hash: str
    publication_scope: str
    plan_hash: str


@dataclass(frozen=True, slots=True)
class VerifiedGrant:
    """Verified grant authorizing exactly one mutation dispatch attempt."""

    action: str
    operation_id: str
    profile: str
    profile_epoch: int
    source: str
    repository: str
    branch: str
    payload_hash: str
    context_hash: str
    plan_hash: str
    publication_scope: str
    authorizing_source: str
    session: str | None = None
    expiry: str | None = None
    revocation_ref: str | None = None
    max_attempts: int = 1


class OperationState(str, enum.Enum):
    """Operation lifecycle state machine in the durable journal."""

    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    EFFECT_OBSERVED = "effect_observed"
    BLOCKED_BEFORE_DISPATCH = "blocked_before_dispatch"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"


LEGAL_OPERATION_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.PREPARED: frozenset({
        OperationState.DISPATCHING,
        OperationState.BLOCKED_BEFORE_DISPATCH,
        OperationState.CANCELLED_BEFORE_DISPATCH,
        OperationState.UNKNOWN,
    }),
    OperationState.DISPATCHING: frozenset({
        OperationState.ACCEPTED,
        OperationState.REJECTED,
        OperationState.UNKNOWN,
    }),
    OperationState.ACCEPTED: frozenset({
        OperationState.EFFECT_OBSERVED,
        OperationState.UNKNOWN,
    }),
    OperationState.UNKNOWN: frozenset({
        OperationState.EFFECT_OBSERVED,
        OperationState.REJECTED,
    }),
    OperationState.EFFECT_OBSERVED: frozenset(),
    OperationState.BLOCKED_BEFORE_DISPATCH: frozenset(),
    OperationState.REJECTED: frozenset(),
    OperationState.CANCELLED_BEFORE_DISPATCH: frozenset(),
}


def is_legal_operation_transition(
    from_state: OperationState, to_state: OperationState
) -> bool:
    """Return True if transitioning from from_state to to_state is legal."""
    return to_state in LEGAL_OPERATION_TRANSITIONS.get(from_state, frozenset())


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """Durable mutation operation record with segregated evidence fields."""

    operation_id: str
    state: OperationState
    request_hash: str
    binding: Binding | None = None
    ticket_id: str | None = None
    api_accepted: bool = False
    effect_observed: bool = False
    attribution: str = ""
    ui_verified: bool = False
    accepted_identity_unverified: bool = False
    error_code: ErrorCode | None = None
    created_at: str | None = None
    updated_at: str | None = None
    evidence: tuple[tuple[str, Any], ...] = ()


class ActionResultStatus(str, enum.Enum):
    """Outcome status for an individual action."""

    OK = "ok"
    WAITING = "waiting"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Structured result of executing a single plan action."""

    action_id: str
    op: str
    status: ActionResultStatus
    exit_code: int
    error_code: ErrorCode | str | None = None
    coverage: Coverage | None = None
    data: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def create(
        cls,
        action_id: str,
        op: str,
        status: ActionResultStatus | str,
        exit_code: int,
        error_code: ErrorCode | str | None = None,
        coverage: Coverage | None = None,
        data: Mapping[str, Any] | tuple[tuple[str, Any], ...] | None = None,
    ) -> ActionResult:
        """Helper to construct ActionResult from dict or tuple."""
        st = ActionResultStatus(status) if isinstance(status, str) else status
        if data is None:
            data_tuple: tuple[tuple[str, Any], ...] = ()
        elif isinstance(data, Mapping):
            data_tuple = tuple(data.items())
        else:
            data_tuple = tuple(data)
        return cls(
            action_id=action_id,
            op=op,
            status=st,
            exit_code=exit_code,
            error_code=error_code,
            coverage=coverage,
            data=data_tuple,
        )

    @property
    def data_dict(self) -> dict[str, Any]:
        """Return data as a dictionary."""
        return dict(self.data)


@dataclass(frozen=True, slots=True)
class Event:
    """Durable event with stable identifier."""

    event_id: str
    event_type: str
    resource_id: str
    payload: tuple[tuple[str, Any], ...] = ()
    created_at: str | None = None
    session_id: str | None = None
    transition_id: str | None = None

    @classmethod
    def create(
        cls,
        event_id: str,
        event_type: str,
        resource_id: str,
        payload: Mapping[str, Any] | tuple[tuple[str, Any], ...] | None = None,
        created_at: str | None = None,
        session_id: str | None = None,
        transition_id: str | None = None,
    ) -> Event:
        if payload is None:
            p_tuple: tuple[tuple[str, Any], ...] = ()
        elif isinstance(payload, Mapping):
            p_tuple = tuple(payload.items())
        else:
            p_tuple = tuple(payload)
        return cls(
            event_id=event_id,
            event_type=event_type,
            resource_id=resource_id,
            payload=p_tuple,
            created_at=created_at,
            session_id=session_id,
            transition_id=transition_id,
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    """Outbox and channel delivery receipt with distinct stages."""

    receipt_id: str
    event_id: str
    receiver_accepted: bool = False
    channel_send_accepted: bool = False
    delivery_unknown: bool = False
    channel: str | None = None
    timestamp: str | None = None
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Manifest describing an exported or cached artifact."""

    artifact_id: str
    path: str
    content_hash: str
    byte_count: int
    media_type: str = "text/plain"
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class Capability:
    """Independent capability evidence record."""

    name: str
    documented: bool
    enabled: bool
    live_tested: bool
    source: str
    evidence_time: str | None = None


class LifecycleBucket(str, enum.Enum):
    """Exclusive lifecycle categorization."""

    OPEN = "open"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Derived terminal property: completed + failed."""
        return self in (LifecycleBucket.COMPLETED, LifecycleBucket.FAILED)


@dataclass(frozen=True, slots=True)
class DispatchTicket:
    """Opaque, single-use dispatch ticket bound to operation_id + request_hash."""

    ticket_id: str
    operation_id: str
    request_hash: str
    nonce: str = ""
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class MutationResponse:
    """Outcome of an internal mutation method.

    Never raises for transport-level failures; returns outcome with uncertain_effect=True
    for timeout/disconnect/5xx/malformed-or-oversized success, and uncertain_effect=False
    for clear 4xx rejections. Raises only for local pre-dispatch validation failures.
    """

    outcome: TransportOutcome
    session: SessionRecord | None = None


@dataclass(frozen=True, slots=True)
class TransportOutcome:
    """Sanitized transport outcome without exposing secrets or raw HTTP headers."""

    status: int
    body: bytes | None = None
    request_count: int = 1
    byte_count: int = 0
    uncertain_effect: bool = False
    sanitized_error_code: ErrorCode | None = None
    retry_after: float | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    """Observation collected by ReadService or Provider."""

    binding: Binding | None = None
    sources: tuple[dict[str, Any], ...] = ()
    sessions: tuple[dict[str, Any], ...] = ()
    activities: tuple[dict[str, Any], ...] = ()
    coverage: Coverage | None = None
    candidate_bundle: CandidateBundle | None = None
    metadata: tuple[tuple[str, Any], ...] = ()


# Remote response records (tolerate unknown fields, preserve unknown state strings verbatim)

@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Remote source record preserving unknown fields."""

    name: str
    id: str | None = None
    github_repo_owner: str | None = None
    github_repo_name: str | None = None
    unknown_fields: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRecord:
        known = {"name", "id", "githubRepo", "github_repo_owner", "github_repo_name"}
        github_repo = data.get("githubRepo") or {}
        owner = data.get("github_repo_owner") or github_repo.get("owner")
        repo = data.get("github_repo_name") or github_repo.get("repo")
        unknown = tuple((k, v) for k, v in data.items() if k not in known)
        return cls(
            name=data["name"],
            id=data.get("id"),
            github_repo_owner=owner,
            github_repo_name=repo,
            unknown_fields=unknown,
        )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Remote session record preserving unknown fields and unfamiliar state strings."""

    name: str
    state: str  # Preserves unknown state verbatim!
    id: str | None = None
    title: str | None = None
    create_time: str | None = None
    update_time: str | None = None
    require_plan_approval: bool | None = None
    source_context: tuple[tuple[str, Any], ...] = ()
    unknown_fields: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord:
        known = {
            "name",
            "state",
            "id",
            "title",
            "createTime",
            "create_time",
            "updateTime",
            "update_time",
            "requirePlanApproval",
            "require_plan_approval",
            "sourceContext",
            "source_context",
        }
        sc = data.get("sourceContext") or data.get("source_context") or {}
        sc_tuple = tuple(sc.items()) if isinstance(sc, Mapping) else ()
        unknown = tuple((k, v) for k, v in data.items() if k not in known)
        return cls(
            name=data["name"],
            state=str(data.get("state", "UNKNOWN")),
            id=data.get("id"),
            title=data.get("title"),
            create_time=data.get("createTime") or data.get("create_time"),
            update_time=data.get("updateTime") or data.get("update_time"),
            require_plan_approval=data.get("requirePlanApproval")
            if "requirePlanApproval" in data
            else data.get("require_plan_approval"),
            source_context=sc_tuple,
            unknown_fields=unknown,
        )


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """Remote activity record preserving unknown fields and activity types."""

    name: str
    activity_type: str  # Preserves unknown type verbatim!
    id: str | None = None
    create_time: str | None = None
    originator: str | None = None
    unknown_fields: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActivityRecord:
        known = {
            "name",
            "type",
            "activity_type",
            "id",
            "createTime",
            "create_time",
            "originator",
        }
        act_type = str(data.get("type") or data.get("activity_type", "UNKNOWN"))
        unknown = tuple((k, v) for k, v in data.items() if k not in known)
        return cls(
            name=data["name"],
            activity_type=act_type,
            id=data.get("id"),
            create_time=data.get("createTime") or data.get("create_time"),
            originator=data.get("originator"),
            unknown_fields=unknown,
        )
