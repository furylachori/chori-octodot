"""Executable contracts, strict JSON encoding/loading, schemas, and typed ports.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import enum
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from octodot.errors import (
    ALL_EXIT_CODES,
    EXIT_OK,
    ErrorCode,
    OctodotError,
    PlanValidationError,
    ResultValidationError,
    combine_exit_codes,
)
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ActivityRecord,
    ArtifactManifest,
    Binding,
    CandidateBundle,
    Capability,
    Coverage,
    DispatchTicket,
    Event,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    Receipt,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
    VerifiedGrant,
)

# Canonical encoding version constant
CANONICAL_ENCODING_VERSION: str = "octodot.canon.v1"

# Plan schema constant
PLAN_SCHEMA_VERSION: str = "jules-controller.plan.v1"

# Result schema constant
RESULT_SCHEMA_VERSION: str = "jules-controller.result.v1"

# Volatile context keys excluded during context hash computation
VOLATILE_CONTEXT_KEYS: frozenset[str] = frozenset({
    "timestamp",
    "timestamps",
    "scan_id",
    "scan_ids",
    "scanned_at",
    "observed_at",
    "local_time",
    "local_timestamp",
    "evidence_time",
    "created_at",
    "read_at",
    "fetched_at",
})

# Default live invocation limits
LIVE_INVOCATION_DEFAULTS: dict[str, int] = {
    "deadline_seconds": 180,
    "request_timeout_seconds": 20,
    "max_http_requests": 120,
    "max_posts": 0,
    "max_pages": 100,
    "max_sessions": 200,
    "max_response_bytes": 8388608,
    "max_total_bytes": 33554432,
    "max_output_bytes": 65536,
}


# =====================================================================
# Strict JSON Loading
# =====================================================================

def _object_pairs_strict_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Hook for json.loads that rejects duplicate keys."""
    d: dict[str, Any] = {}
    for key, value in pairs:
        if key in d:
            raise OctodotError(ErrorCode.DUPLICATE_KEY, f"Duplicate JSON key: '{key}'")
        d[key] = value
    return d


def _reject_nonfinite_constant(constant: str) -> None:
    """Hook for json.loads that rejects NaN, Infinity, -Infinity."""
    raise OctodotError(
        ErrorCode.NONFINITE_NUMBER,
        f"Nonfinite floating point value not allowed: '{constant}'",
    )


def load_strict_json(content: str | bytes, max_bytes: int = 1_048_576) -> dict[str, Any]:
    """Parse strict JSON document.

    Rejects:
    - Input exceeding max_bytes (default 1 MiB)
    - Non-UTF-8 bytes
    - Duplicate dictionary keys
    - NaN, Infinity, -Infinity
    - Top-level non-object
    """
    if isinstance(content, bytes):
        if len(content) > max_bytes:
            raise OctodotError(
                ErrorCode.OVERSIZED_INPUT,
                f"Input size {len(content)} exceeds maximum byte limit of {max_bytes}",
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as err:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Invalid UTF-8 encoding in input: {err}",
            ) from err
    elif isinstance(content, str):
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            raise OctodotError(
                ErrorCode.OVERSIZED_INPUT,
                f"Input size {len(encoded)} exceeds maximum byte limit of {max_bytes}",
            )
        text = content
    else:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Expected str or bytes, got {type(content).__name__}",
        )

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_object_pairs_strict_hook,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as err:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Malformed JSON: {err}") from err

    if not isinstance(parsed, dict):
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Top-level JSON value must be an object, got {type(parsed).__name__}",
        )

    return parsed


# =====================================================================
# Canonical Encoding and Hashing
# =====================================================================

def canonical_bytes(obj: Any) -> bytes:
    """Encode object to canonical UTF-8 JSON bytes under octodot.canon.v1 rules.

    - UTF-8
    - sort_keys=True
    - separators=(",", ":")
    - ensure_ascii=False
    - allow_nan=False (strictly no NaN/Infinity)
    - Exact strings (no Unicode normalization, no newline normalization)
    """
    try:
        json_str = json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (ValueError, TypeError) as err:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Cannot canonically serialize object: {err}",
        ) from err

    return json_str.encode("utf-8")


def canonical_hash(obj: Any) -> str:
    """Compute canonical hash formatted as 'sha256:<64-hex>'."""
    digest = hashlib.sha256(canonical_bytes(obj)).hexdigest()
    return f"sha256:{digest}"


def binding_hash(binding: Binding | Mapping[str, Any]) -> str:
    """Compute canonical hash of an exact case-sensitive repository/branch binding."""
    if isinstance(binding, Binding):
        b_dict = {
            "profile": binding.profile,
            "profile_epoch": binding.profile_epoch,
            "repository": binding.repository,
            "session": binding.session,
            "source": binding.source,
            "starting_branch": binding.starting_branch,
        }
    elif isinstance(binding, Mapping):
        b_dict = {
            "profile": str(binding["profile"]),
            "profile_epoch": int(binding.get("profile_epoch", 0)),
            "repository": str(binding["repository"]),
            "session": binding.get("session"),
            "source": str(binding["source"]),
            "starting_branch": binding.get("starting_branch"),
        }
    else:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Invalid binding type: {type(binding).__name__}",
        )

    return canonical_hash(b_dict)


def request_hash(payload: Any) -> str:
    """Compute canonical hash of request payload."""
    return canonical_hash(payload)


def _filter_volatile_keys(obj: Any) -> Any:
    """Recursively filter out volatile observation timestamps and scan IDs."""
    if isinstance(obj, Mapping):
        return {
            k: _filter_volatile_keys(v)
            for k, v in obj.items()
            if k not in VOLATILE_CONTEXT_KEYS
        }
    if isinstance(obj, (list, tuple)):
        return [_filter_volatile_keys(item) for item in obj]
    return obj


def context_hash(context: Mapping[str, Any] | Any) -> str:
    """Compute canonical context hash excluding volatile timestamps and scan IDs."""
    cleaned = _filter_volatile_keys(context)
    return canonical_hash(cleaned)


# =====================================================================
# Fixed Operation Inventory
# =====================================================================

class OperationClassification(str, enum.Enum):
    """Operation category classification."""

    READ = "read"
    MUTATION = "mutation"
    LOCAL = "local"
    DIAGNOSTIC = "diagnostic"


class CapabilityClassification(str, enum.Enum):
    """Operation capability availability classification."""

    CORE = "core"
    OPTIONAL = "optional"
    UNSUPPORTED_PUBLIC_API = "unsupported_public_api"


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Formal specification of an inventory operation."""

    op: str
    classification: OperationClassification
    capability: CapabilityClassification
    description: str
    allowed_selections: tuple[str, ...] = ()


OPERATION_INVENTORY: dict[str, OperationSpec] = {
    "inventory.collect": OperationSpec(
        op="inventory.collect",
        classification=OperationClassification.READ,
        capability=CapabilityClassification.CORE,
        description="Collect connected sources and paginated sessions",
        allowed_selections=("sessions", "sources", "session_names", "source_names", "active_session"),
    ),
    "session.inspect": OperationSpec(
        op="session.inspect",
        classification=OperationClassification.READ,
        capability=CapabilityClassification.CORE,
        description="Inspect specific session binding, state, and latest plan",
        allowed_selections=("session", "binding", "state", "title", "latest_plan", "latest_plan_id", "latest_plan_hash", "feedback_bundle"),
    ),
    "chats.collect": OperationSpec(
        op="chats.collect",
        classification=OperationClassification.READ,
        capability=CapabilityClassification.CORE,
        description="Collect session conversation activities and feedback bundle",
        allowed_selections=("messages", "activities", "candidate_bundle", "latest_activity_id", "feedback_bundle", "last_message"),
    ),
    "chats.reply": OperationSpec(
        op="chats.reply",
        classification=OperationClassification.MUTATION,
        capability=CapabilityClassification.CORE,
        description="Send exactly one approved reply message to a session",
        allowed_selections=(),
    ),
    "tasks.create": OperationSpec(
        op="tasks.create",
        classification=OperationClassification.MUTATION,
        capability=CapabilityClassification.CORE,
        description="Create one bounded task session (requirePlanApproval defaults to False, optional True)",
        allowed_selections=(),
    ),
    "plans.approve": OperationSpec(
        op="plans.approve",
        classification=OperationClassification.MUTATION,
        capability=CapabilityClassification.CORE,
        description="Approve reviewed plan on a waiting session",
        allowed_selections=(),
    ),
    "suggestions.collect": OperationSpec(
        op="suggestions.collect",
        classification=OperationClassification.READ,
        capability=CapabilityClassification.UNSUPPORTED_PUBLIC_API,
        description="Collect suggestions (unsupported in public API mode)",
        allowed_selections=("suggestions", "items"),
    ),
    "artifacts.export_patch": OperationSpec(
        op="artifacts.export_patch",
        classification=OperationClassification.LOCAL,
        capability=CapabilityClassification.CORE,
        description="Inert export of patch artifacts to local filesystem",
        allowed_selections=("patch", "manifest", "artifact_id"),
    ),
    "publication.verify": OperationSpec(
        op="publication.verify",
        classification=OperationClassification.READ,
        capability=CapabilityClassification.CORE,
        description="Read-only verification of GitHub publication state",
        allowed_selections=("verified", "details", "publication_state"),
    ),
    "operations.reconcile": OperationSpec(
        op="operations.reconcile",
        classification=OperationClassification.LOCAL,
        capability=CapabilityClassification.CORE,
        description="Read-only uncertain-effect reconciliation for recorded mutations",
        allowed_selections=("reconciled_state", "operation_record"),
    ),
    "events.read": OperationSpec(
        op="events.read",
        classification=OperationClassification.READ,
        capability=CapabilityClassification.CORE,
        description="Read durable events from outbox",
        allowed_selections=("events", "event_ids"),
    ),
    "events.ack": OperationSpec(
        op="events.ack",
        classification=OperationClassification.LOCAL,
        capability=CapabilityClassification.CORE,
        description="Acknowledge delivered events",
        allowed_selections=("acked_event_ids", "status"),
    ),
    "wait": OperationSpec(
        op="wait",
        classification=OperationClassification.LOCAL,
        capability=CapabilityClassification.CORE,
        description="Bounded resumable wait for predicates",
        allowed_selections=("resumed", "predicate_matched", "job_id"),
    ),
    "capabilities.inspect": OperationSpec(
        op="capabilities.inspect",
        classification=OperationClassification.DIAGNOSTIC,
        capability=CapabilityClassification.CORE,
        description="Inspect current profile capabilities and evidence",
        allowed_selections=("capabilities",),
    ),
    "healthcheck": OperationSpec(
        op="healthcheck",
        classification=OperationClassification.DIAGNOSTIC,
        capability=CapabilityClassification.CORE,
        description="Check controller health and store accessibility",
        allowed_selections=("healthy", "status"),
    ),
}


# =====================================================================
# Typed Argument and Result Contracts for Operations (S01-T05)
# =====================================================================

@dataclass(frozen=True, slots=True)
class InventoryCollectArgs:
    scope: str = "all"
    repository: str | None = None

@dataclass(frozen=True, slots=True)
class InventoryCollectResult:
    sources: tuple[SourceRecord, ...] = ()
    sessions: tuple[SessionRecord, ...] = ()
    coverage: Coverage | None = None

@dataclass(frozen=True, slots=True)
class SessionInspectArgs:
    session: str
    binding: Binding | None = None

@dataclass(frozen=True, slots=True)
class SessionInspectResult:
    session: SessionRecord | None = None
    binding: Binding | None = None
    state: str = ""

@dataclass(frozen=True, slots=True)
class ChatsCollectArgs:
    session: str
    since_time: str | None = None

@dataclass(frozen=True, slots=True)
class ChatsCollectResult:
    activities: tuple[ActivityRecord, ...] = ()
    candidate_bundle: CandidateBundle | None = None

@dataclass(frozen=True, slots=True)
class ChatsReplyArgs:
    session: str
    text: str
    operation_id: str
    authorization_ref: str

@dataclass(frozen=True, slots=True)
class ChatsReplyResult:
    operation_id: str
    status: str
    delivered: bool = False

@dataclass(frozen=True, slots=True)
class TasksCreateArgs:
    repository: str
    branch: str
    title: str
    prompt: str
    operation_id: str
    authorization_ref: str
    require_plan_approval: bool = False

@dataclass(frozen=True, slots=True)
class TasksCreateResult:
    session: SessionRecord | None = None
    operation_id: str = ""

@dataclass(frozen=True, slots=True)
class PlansApproveArgs:
    session: str
    plan_id: str
    operation_id: str
    authorization_ref: str

@dataclass(frozen=True, slots=True)
class PlansApproveResult:
    session: str
    plan_id: str
    approved: bool = False

@dataclass(frozen=True, slots=True)
class SuggestionsCollectArgs:
    repository: str

@dataclass(frozen=True, slots=True)
class SuggestionsCollectResult:
    suggestions: tuple[dict[str, Any], ...] = ()
    unsupported: bool = True

@dataclass(frozen=True, slots=True)
class ArtifactsExportPatchArgs:
    session: str
    destination_dir: str

@dataclass(frozen=True, slots=True)
class ArtifactsExportPatchResult:
    manifest: ArtifactManifest | None = None
    exported_path: str = ""

@dataclass(frozen=True, slots=True)
class PublicationVerifyArgs:
    repository: str
    branch: str

@dataclass(frozen=True, slots=True)
class PublicationVerifyResult:
    verified: bool = False
    publication_state: str = ""

@dataclass(frozen=True, slots=True)
class OperationsReconcileArgs:
    operation_id: str

@dataclass(frozen=True, slots=True)
class OperationsReconcileResult:
    reconciled_state: str = ""
    record: OperationRecord | None = None

@dataclass(frozen=True, slots=True)
class EventsReadArgs:
    limit: int = 100
    since_id: str | None = None

@dataclass(frozen=True, slots=True)
class EventsReadResult:
    events: tuple[Event, ...] = ()

@dataclass(frozen=True, slots=True)
class EventsAckArgs:
    event_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class EventsAckResult:
    acked_event_ids: tuple[str, ...] = ()
    success: bool = True

@dataclass(frozen=True, slots=True)
class WaitArgs:
    predicate: str
    timeout_seconds: float = 30.0

@dataclass(frozen=True, slots=True)
class WaitResult:
    resumed: bool = False
    predicate_matched: bool = False
    job_id: str | None = None

@dataclass(frozen=True, slots=True)
class CapabilitiesInspectArgs:
    profile: str

@dataclass(frozen=True, slots=True)
class CapabilitiesInspectResult:
    capabilities: tuple[Capability, ...] = ()

@dataclass(frozen=True, slots=True)
class HealthcheckArgs:
    profile: str = "default"

@dataclass(frozen=True, slots=True)
class HealthcheckResult:
    healthy: bool = True
    details: tuple[tuple[str, Any], ...] = ()


# =====================================================================
# Plan Validation (jules-controller.plan.v1)
# =====================================================================

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PLACEHOLDER_PATTERN = re.compile(r"<[A-Za-z0-9_ -]+>")

PLAN_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "plan_id",
    "plan_hash",
    "profile",
    "execution",
    "scope",
    "limits",
    "actions",
    "output",
})

EXECUTION_KEYS: frozenset[str] = frozenset({"mode"})
SCOPE_KEYS: frozenset[str] = frozenset({"repository", "branch", "sessions"})
OUTPUT_KEYS: frozenset[str] = frozenset({"format", "destination"})
LIMIT_KEYS: frozenset[str] = frozenset({
    "deadline_seconds",
    "request_timeout_seconds",
    "max_http_requests",
    "max_posts",
    "max_pages",
    "max_sessions",
    "max_response_bytes",
    "max_total_bytes",
    "max_output_bytes",
})

READ_ACTION_KEYS: frozenset[str] = frozenset({"id", "op", "params"})
MUTATION_ACTION_KEYS: frozenset[str] = frozenset({
    "id",
    "op",
    "enabled",
    "operation_id",
    "authorization_ref",
    "target",
    "payload",
    "preconditions",
})


def compute_plan_hash(plan_dict: Mapping[str, Any]) -> str:
    """Compute canonical hash of a plan minus the 'plan_hash' field."""
    cleaned = {k: v for k, v in plan_dict.items() if k != "plan_hash"}
    return canonical_hash(cleaned)


def _validate_no_dynamic_mutation_references(obj: Any, path: str = "mutation") -> None:
    """Reject any reference dict or JSONPath in mutation target/payload."""
    if isinstance(obj, Mapping):
        if "from" in obj or "select" in obj:
            raise OctodotError(
                ErrorCode.DYNAMIC_MUTATION_TARGET,
                f"Dynamic reference ('from'/'select') not permitted in {path}",
            )
        for k, v in obj.items():
            _validate_no_dynamic_mutation_references(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _validate_no_dynamic_mutation_references(item, f"{path}[{idx}]")
    elif isinstance(obj, str):
        if obj.startswith("$.") or obj.startswith("$[") or "jsonpath:" in obj.lower():
            raise OctodotError(
                ErrorCode.DYNAMIC_MUTATION_TARGET,
                f"Dynamic expression not permitted in {path}: '{obj}'",
            )


def _validate_read_action_references(
    params: Any,
    action_id: str,
    seen_read_actions: dict[str, str],
    all_action_ids: Sequence[str],
    current_index: int,
) -> None:
    """Validate references in read action params."""
    if isinstance(params, Mapping):
        if "from" in params or "select" in params:
            # Must contain ONLY 'from' and 'select'
            extra = set(params.keys()) - {"from", "select"}
            if extra:
                raise OctodotError(
                    ErrorCode.UNKNOWN_FIELD,
                    f"Unknown field in selection reference: {sorted(extra)}",
                )
            from_id = params.get("from")
            select_key = params.get("select")
            if not isinstance(from_id, str) or not isinstance(select_key, str):
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    f"Selection reference fields must be strings in action '{action_id}'",
                )

            # Check self-reference
            if from_id == action_id:
                raise OctodotError(
                    ErrorCode.INVALID_REFERENCE,
                    f"Action '{action_id}' cannot reference itself",
                )

            # Check if referenced action is earlier
            if from_id not in seen_read_actions:
                if from_id in all_action_ids:
                    ref_idx = all_action_ids.index(from_id)
                    if ref_idx > current_index:
                        raise OctodotError(
                            ErrorCode.INVALID_REFERENCE,
                            f"Forward reference: action '{action_id}' references later action '{from_id}'",
                        )
                    raise OctodotError(
                        ErrorCode.INVALID_REFERENCE,
                        f"Action '{action_id}' cannot reference mutation or non-selection action '{from_id}'",
                    )
                raise OctodotError(
                    ErrorCode.INVALID_REFERENCE,
                    f"Referenced action '{from_id}' not found in plan",
                )

            # Check selection is in allowed selections
            earlier_op = seen_read_actions[from_id]
            spec = OPERATION_INVENTORY.get(earlier_op)
            if not spec or select_key not in spec.allowed_selections:
                allowed = spec.allowed_selections if spec else ()
                raise OctodotError(
                    ErrorCode.INVALID_REFERENCE,
                    f"Selection '{select_key}' not allowed for op '{earlier_op}' (allowed: {allowed})",
                )
            return

        # Recursively validate dict values
        for k, v in params.items():
            _validate_read_action_references(
                v, action_id, seen_read_actions, all_action_ids, current_index
            )
    elif isinstance(params, (list, tuple)):
        for item in params:
            _validate_read_action_references(
                item, action_id, seen_read_actions, all_action_ids, current_index
            )
    elif isinstance(params, str):
        if params.startswith("$.") or params.startswith("$[") or "jsonpath:" in params.lower():
            raise OctodotError(
                ErrorCode.INVALID_REFERENCE,
                f"Arbitrary JSONPath or expression not allowed: '{params}'",
            )


def validate_plan(plan: dict[str, Any]) -> None:
    """Validate a plan strictly against jules-controller.plan.v1 specification.

    Raises PlanValidationError / OctodotError on failure.
    """
    if not isinstance(plan, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "Plan must be a JSON object")

    # Check top-level unknown fields
    unknown_top = set(plan.keys()) - PLAN_TOP_LEVEL_KEYS
    if unknown_top:
        raise OctodotError(
            ErrorCode.UNKNOWN_FIELD,
            f"Unknown field in plan root: {sorted(unknown_top)}",
        )

    # Check required top-level keys
    missing_top = PLAN_TOP_LEVEL_KEYS - set(plan.keys())
    if missing_top:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Missing required plan fields: {sorted(missing_top)}",
        )

    # Validate schema_version
    version = plan["schema_version"]
    if version != PLAN_SCHEMA_VERSION:
        if isinstance(version, str) and version.startswith("jules-controller.plan."):
            raise OctodotError(ErrorCode.SCHEMA_TOO_NEW, f"Unsupported schema version: '{version}'")
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Invalid schema_version: '{version}'")

    # Validate plan_id
    plan_id = plan["plan_id"]
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise OctodotError(ErrorCode.INVALID_INPUT, "plan_id must be a non-empty string")

    # Validate profile
    profile = plan["profile"]
    if not isinstance(profile, str) or not profile.strip():
        raise OctodotError(ErrorCode.INVALID_INPUT, "profile must be a non-empty string")

    # Validate execution
    execution = plan["execution"]
    if not isinstance(execution, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "execution must be an object")
    unknown_exec = set(execution.keys()) - EXECUTION_KEYS
    if unknown_exec:
        raise OctodotError(ErrorCode.UNKNOWN_FIELD, f"Unknown field in execution: {sorted(unknown_exec)}")
    mode = execution.get("mode")
    if mode not in {"read_only", "authorized_get", "mutation"}:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Invalid execution mode: '{mode}'")

    # Validate scope
    scope = plan["scope"]
    if not isinstance(scope, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "scope must be an object")
    unknown_scope = set(scope.keys()) - SCOPE_KEYS
    if unknown_scope:
        raise OctodotError(ErrorCode.UNKNOWN_FIELD, f"Unknown field in scope: {sorted(unknown_scope)}")
    if "repository" not in scope or not isinstance(scope["repository"], str):
        raise OctodotError(ErrorCode.INVALID_INPUT, "scope.repository must be a string")
    if not _REPO_PATTERN.match(scope["repository"]):
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"scope.repository must be OWNER/REPO format, got '{scope['repository']}'",
        )
    if "branch" in scope and not isinstance(scope["branch"], str):
        raise OctodotError(ErrorCode.INVALID_INPUT, "scope.branch must be a string")
    if "sessions" in scope:
        if not isinstance(scope["sessions"], (list, tuple)) or not all(
            isinstance(s, str) for s in scope["sessions"]
        ):
            raise OctodotError(ErrorCode.INVALID_INPUT, "scope.sessions must be a list of strings")

    # Validate limits
    limits = plan["limits"]
    if not isinstance(limits, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "limits must be an object")
    unknown_limits = set(limits.keys()) - LIMIT_KEYS
    if unknown_limits:
        raise OctodotError(ErrorCode.UNKNOWN_FIELD, f"Unknown field in limits: {sorted(unknown_limits)}")
    missing_limits = LIMIT_KEYS - set(limits.keys())
    if missing_limits:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Missing required limits: {sorted(missing_limits)}")

    for k in LIMIT_KEYS:
        val = limits[k]
        if not isinstance(val, int) or isinstance(val, bool):
            raise OctodotError(ErrorCode.INVALID_INPUT, f"Limit '{k}' must be an integer")
        if k in {"deadline_seconds", "request_timeout_seconds", "max_pages", "max_sessions", "max_response_bytes", "max_total_bytes", "max_output_bytes"} and val <= 0:
            raise OctodotError(ErrorCode.INVALID_INPUT, f"Limit '{k}' must be positive")
        if k in {"max_http_requests", "max_posts"} and val < 0:
            raise OctodotError(ErrorCode.INVALID_INPUT, f"Limit '{k}' cannot be negative")

    # max_posts constraint
    max_posts = limits["max_posts"]
    if mode != "mutation" and max_posts != 0:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"limits.max_posts must be 0 for execution mode '{mode}'",
        )
    if mode == "mutation" and max_posts > 1:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            "limits.max_posts cannot exceed 1 for mutation plans",
        )

    # Validate output
    output = plan["output"]
    if not isinstance(output, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "output must be an object")
    unknown_output = set(output.keys()) - OUTPUT_KEYS
    if unknown_output:
        raise OctodotError(ErrorCode.UNKNOWN_FIELD, f"Unknown field in output: {sorted(unknown_output)}")
    if output.get("format") not in {"json", "summary"}:
        raise OctodotError(ErrorCode.INVALID_INPUT, "output.format must be 'json' or 'summary'")
    if "destination" in output and not isinstance(output["destination"], str):
        raise OctodotError(ErrorCode.INVALID_INPUT, "output.destination must be a string")

    # Validate actions
    actions = plan["actions"]
    if not isinstance(actions, list) or not actions:
        raise OctodotError(ErrorCode.INVALID_INPUT, "actions must be a non-empty list")

    action_ids: list[str] = []
    seen_ids: set[str] = set()
    for act in actions:
        if not isinstance(act, dict):
            raise OctodotError(ErrorCode.INVALID_INPUT, "Each action must be an object")
        act_id = act.get("id")
        if not isinstance(act_id, str) or not act_id.strip():
            raise OctodotError(ErrorCode.INVALID_INPUT, "Action id must be a non-empty string")
        if act_id in seen_ids:
            raise OctodotError(ErrorCode.DUPLICATE_KEY, f"Duplicate action id: '{act_id}'")
        seen_ids.add(act_id)
        action_ids.append(act_id)

    seen_read_actions: dict[str, str] = {}
    mutation_count = 0

    for idx, act in enumerate(actions):
        act_id = act["id"]
        op = act.get("op")
        if not isinstance(op, str) or op not in OPERATION_INVENTORY:
            raise OctodotError(ErrorCode.INVALID_INPUT, f"Unknown or invalid operation: '{op}'")

        spec = OPERATION_INVENTORY[op]

        if spec.classification == OperationClassification.MUTATION:
            mutation_count += 1
            if mode != "mutation":
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    f"Mutation op '{op}' not permitted in execution mode '{mode}'",
                )

            # Check unknown fields
            unknown_act = set(act.keys()) - MUTATION_ACTION_KEYS
            if unknown_act:
                raise OctodotError(
                    ErrorCode.UNKNOWN_FIELD,
                    f"Unknown field in mutation action '{act_id}': {sorted(unknown_act)}",
                )

            # Check required fields
            missing_mut = {"enabled", "operation_id", "authorization_ref", "target", "payload"} - set(act.keys())
            if missing_mut:
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    f"Missing fields in mutation action '{act_id}': {sorted(missing_mut)}",
                )

            if not isinstance(act["enabled"], bool):
                raise OctodotError(ErrorCode.INVALID_INPUT, f"Action '{act_id}'.enabled must be boolean")
            if not isinstance(act["operation_id"], str) or not act["operation_id"].strip():
                raise OctodotError(ErrorCode.INVALID_INPUT, f"Action '{act_id}'.operation_id must be non-empty string")
            if not isinstance(act["authorization_ref"], str) or not act["authorization_ref"].strip():
                raise OctodotError(ErrorCode.INVALID_INPUT, f"Action '{act_id}'.authorization_ref must be non-empty string")
            if not isinstance(act["payload"], dict):
                raise OctodotError(ErrorCode.INVALID_INPUT, f"Action '{act_id}'.payload must be an object")
            if "preconditions" in act and not isinstance(act["preconditions"], dict):
                raise OctodotError(ErrorCode.INVALID_INPUT, f"Action '{act_id}'.preconditions must be an object")

            # Reject dynamic references inside mutation target or payload
            _validate_no_dynamic_mutation_references(act["target"], f"action '{act_id}'.target")
            _validate_no_dynamic_mutation_references(act["payload"], f"action '{act_id}'.payload")
            if "preconditions" in act:
                _validate_no_dynamic_mutation_references(act["preconditions"], f"action '{act_id}'.preconditions")

        else:
            # Read / local / diagnostic action
            unknown_act = set(act.keys()) - READ_ACTION_KEYS
            if unknown_act:
                raise OctodotError(
                    ErrorCode.UNKNOWN_FIELD,
                    f"Unknown field in read action '{act_id}': {sorted(unknown_act)}",
                )

            if "params" in act:
                if not isinstance(act["params"], dict):
                    raise OctodotError(ErrorCode.INVALID_INPUT, f"Action '{act_id}'.params must be an object")
                _validate_read_action_references(
                    act["params"], act_id, seen_read_actions, action_ids, idx
                )

            seen_read_actions[act_id] = op

    # Check max_posts vs mutation action count
    if mode == "mutation" and mutation_count > 1 and limits["max_posts"] > 1:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            "max_posts cannot exceed 1 for mutations",
        )

    # Validate plan_hash
    expected_hash = compute_plan_hash(plan)
    if plan.get("plan_hash") != expected_hash:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"plan_hash mismatch: expected {expected_hash}, got {plan.get('plan_hash')}",
        )


# =====================================================================
# Execution Eligibility Checking
# =====================================================================

def _scan_for_placeholders(obj: Any) -> tuple[bool, str]:
    """Recursively search for placeholder tokens."""
    if isinstance(obj, str):
        if _PLACEHOLDER_PATTERN.search(obj):
            return True, obj
        if "REPLACE_ME" in obj or "TODO" in obj or "CHANGEME" in obj:
            return True, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            found, token = _scan_for_placeholders(k)
            if found:
                return True, token
            found, token = _scan_for_placeholders(v)
            if found:
                return True, token
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found, token = _scan_for_placeholders(item)
            if found:
                return True, token
    return False, ""


def check_execution_eligibility(plan: dict[str, Any]) -> None:
    """Verify plan execution eligibility before credential or network access.

    Raises:
    - OctodotError(ErrorCode.TEMPLATE_DISABLED): if any mutation action has enabled=False
    - OctodotError(ErrorCode.PLACEHOLDER_PRESENT): if placeholder tokens are detected
    """
    # Check disabled templates first
    for action in plan.get("actions", []):
        op = action.get("op", "")
        spec = OPERATION_INVENTORY.get(op)
        if spec and spec.classification == OperationClassification.MUTATION:
            if not action.get("enabled", False):
                raise OctodotError(
                    ErrorCode.TEMPLATE_DISABLED,
                    f"Template mutation action '{action.get('id')}' is disabled",
                )

    # Check for placeholder tokens
    has_placeholder, token = _scan_for_placeholders(plan)
    if has_placeholder:
        raise OctodotError(
            ErrorCode.PLACEHOLDER_PRESENT,
            f"Placeholder token found in plan: '{token}'",
        )


# =====================================================================
# Result Validation & Builder (jules-controller.result.v1)
# =====================================================================

RESULT_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "plan_id",
    "status",
    "exit_code",
    "action_results",
    "coverage",
    "omitted_attention_items",
    "resume_ref",
    "output_bytes",
})

COVERAGE_KEYS: frozenset[str] = frozenset({
    "complete",
    "snapshot_atomic",
    "pages",
    "items",
    "skipped_scope",
    "reasons",
    "resume_ref",
})


def validate_result(result: dict[str, Any]) -> None:
    """Validate result strictly against jules-controller.result.v1 specification."""
    if not isinstance(result, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "Result must be a JSON object")

    unknown_top = set(result.keys()) - RESULT_TOP_LEVEL_KEYS
    if unknown_top:
        raise OctodotError(ErrorCode.UNKNOWN_FIELD, f"Unknown field in result root: {sorted(unknown_top)}")

    missing_top = RESULT_TOP_LEVEL_KEYS - set(result.keys())
    if missing_top:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Missing required result fields: {sorted(missing_top)}")

    if result["schema_version"] != RESULT_SCHEMA_VERSION:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Invalid schema_version: '{result['schema_version']}'")

    if not isinstance(result["plan_id"], str) or not result["plan_id"].strip():
        raise OctodotError(ErrorCode.INVALID_INPUT, "plan_id must be a non-empty string")

    status_str = result["status"]
    try:
        ActionResultStatus(status_str)
    except ValueError as err:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Invalid status: '{status_str}'") from err

    exit_code = result["exit_code"]
    if exit_code not in ALL_EXIT_CODES:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Invalid exit_code: {exit_code}")

    action_results = result["action_results"]
    if not isinstance(action_results, list):
        raise OctodotError(ErrorCode.INVALID_INPUT, "action_results must be a list")

    action_exit_codes = []
    for ar in action_results:
        if not isinstance(ar, dict):
            raise OctodotError(ErrorCode.INVALID_INPUT, "Each action result must be an object")
        ar_code = ar.get("exit_code")
        if ar_code not in ALL_EXIT_CODES:
            raise OctodotError(ErrorCode.INVALID_INPUT, f"Invalid action result exit_code: {ar_code}")
        action_exit_codes.append(ar_code)

    expected_exit = combine_exit_codes(action_exit_codes)
    if exit_code != expected_exit:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Result exit_code {exit_code} does not match combined action exit codes {expected_exit}",
        )

    coverage = result["coverage"]
    if not isinstance(coverage, dict):
        raise OctodotError(ErrorCode.INVALID_INPUT, "coverage must be an object")
    unknown_cov = set(coverage.keys()) - COVERAGE_KEYS
    if unknown_cov:
        raise OctodotError(ErrorCode.UNKNOWN_FIELD, f"Unknown field in coverage: {sorted(unknown_cov)}")
    if not isinstance(coverage.get("complete"), bool):
        raise OctodotError(ErrorCode.INVALID_INPUT, "coverage.complete must be boolean")

    omitted = result["omitted_attention_items"]
    if not isinstance(omitted, list) or not all(isinstance(x, str) for x in omitted):
        raise OctodotError(ErrorCode.INVALID_INPUT, "omitted_attention_items must be a list of strings")

    resume_ref = result["resume_ref"]
    if resume_ref is not None and not isinstance(resume_ref, str):
        raise OctodotError(ErrorCode.INVALID_INPUT, "resume_ref must be string or null")

    output_bytes = result["output_bytes"]
    if not isinstance(output_bytes, int) or output_bytes < 0 or output_bytes > 65536:
        raise OctodotError(ErrorCode.OVERSIZED_RESPONSE, f"output_bytes exceeds 64 KiB cap: {output_bytes}")

    # Check serialized size
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 65536:
        raise OctodotError(ErrorCode.OVERSIZED_RESPONSE, f"Result serialized size {len(encoded)} exceeds 64 KiB cap")


class ResultBuilder:
    """Builder for jules-controller.result.v1 documents."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.action_results: list[ActionResult] = []
        self.coverage: Coverage = Coverage(complete=True)
        self.omitted_attention_items: list[str] = []
        self.resume_ref: str | None = None

    def add_action_result(self, result: ActionResult) -> ResultBuilder:
        self.action_results.append(result)
        return self

    def set_coverage(self, coverage: Coverage) -> ResultBuilder:
        self.coverage = coverage
        return self

    def add_omitted_attention(self, item: str) -> ResultBuilder:
        self.omitted_attention_items.append(item)
        return self

    def set_resume_ref(self, resume_ref: str | None) -> ResultBuilder:
        self.resume_ref = resume_ref
        return self

    def build(self) -> dict[str, Any]:
        exit_codes = [ar.exit_code for ar in self.action_results]
        combined_exit = combine_exit_codes(exit_codes)

        # Derive overall status
        statuses = [ar.status for ar in self.action_results]
        if ActionResultStatus.INTERRUPTED in statuses:
            overall_status = ActionResultStatus.INTERRUPTED.value
        elif ActionResultStatus.ERROR in statuses:
            overall_status = ActionResultStatus.ERROR.value
        elif ActionResultStatus.BLOCKED in statuses:
            overall_status = ActionResultStatus.BLOCKED.value
        elif ActionResultStatus.REJECTED in statuses:
            overall_status = ActionResultStatus.REJECTED.value
        elif ActionResultStatus.UNKNOWN in statuses:
            overall_status = ActionResultStatus.UNKNOWN.value
        elif ActionResultStatus.UNSUPPORTED in statuses:
            overall_status = ActionResultStatus.UNSUPPORTED.value
        elif ActionResultStatus.PARTIAL in statuses:
            overall_status = ActionResultStatus.PARTIAL.value
        elif ActionResultStatus.WAITING in statuses:
            overall_status = ActionResultStatus.WAITING.value
        elif all(s == ActionResultStatus.SKIPPED for s in statuses) and statuses:
            overall_status = ActionResultStatus.SKIPPED.value
        else:
            overall_status = ActionResultStatus.OK.value

        ar_dicts = []
        for ar in self.action_results:
            ar_dict: dict[str, Any] = {
                "action_id": ar.action_id,
                "op": ar.op,
                "status": ar.status.value,
                "exit_code": ar.exit_code,
                "error_code": ar.error_code.value if isinstance(ar.error_code, ErrorCode) else ar.error_code,
                "data": ar.data_dict,
            }
            if ar.coverage is not None:
                ar_dict["coverage"] = {
                    "complete": ar.coverage.complete,
                    "snapshot_atomic": ar.coverage.snapshot_atomic,
                    "pages": ar.coverage.pages,
                    "items": ar.coverage.items,
                    "skipped_scope": list(ar.coverage.skipped_scope),
                    "reasons": list(ar.coverage.reasons),
                    "resume_ref": ar.coverage.resume_ref,
                }
            ar_dicts.append(ar_dict)

        cov_dict = {
            "complete": self.coverage.complete,
            "snapshot_atomic": self.coverage.snapshot_atomic,
            "pages": self.coverage.pages,
            "items": self.coverage.items,
            "skipped_scope": list(self.coverage.skipped_scope),
            "reasons": list(self.coverage.reasons),
            "resume_ref": self.coverage.resume_ref,
        }

        result: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "status": overall_status,
            "exit_code": combined_exit,
            "action_results": ar_dicts,
            "coverage": cov_dict,
            "omitted_attention_items": list(self.omitted_attention_items),
            "resume_ref": self.resume_ref,
            "output_bytes": 0,
        }

        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result["output_bytes"] = len(encoded)

        if len(encoded) > 65536:
            raise OctodotError(
                ErrorCode.OVERSIZED_RESPONSE,
                f"Result serialized size {len(encoded)} exceeds 64 KiB cap",
            )

        return result


# =====================================================================
# Port Protocols (typing.Protocol)
# =====================================================================

class Clock(Protocol):
    """Fake-able clock protocol for deterministic time and sleep."""

    def now_utc(self) -> datetime:
        """Return current time in UTC."""
        ...

    def sleep(self, seconds: float) -> None:
        """Sleep for the specified number of seconds."""
        ...


class CredentialSource(Protocol):
    """Lazy credential source; access must be observable by a spy."""

    def get_credential(self, profile: str) -> str | None:
        """Retrieve credential for profile lazily."""
        ...

    def was_accessed(self) -> bool:
        """Return True if credentials have been accessed."""
        ...

    def access_count(self) -> int:
        """Return count of credential accesses."""
        ...


class Transport(Protocol):
    """Fixed-origin transport returning typed sanitized outcomes."""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> TransportOutcome:
        """Execute request against fixed allowlisted origin."""
        ...


class TicketAuthority(Protocol):
    """Authority protocol that validates and redeems journal-minted dispatch tickets.

    Atomically validates that a ticket was issued by the journal for the specified
    operation_id and request_hash, and marks it consumed. A second call with the same
    ticket must return False.
    """

    def redeem(self, ticket: DispatchTicket, request_hash: str) -> bool:
        """Atomically validate ticket authority and mark single use.

        Returns True if valid and consumed; False if ticket was already consumed,
        unrecognized, or request_hash does not match.
        """
        ...


class JulesReadAPI(Protocol):
    """Typed Jules API read wrapper and ticket-gated mutation endpoints.

    The JulesReadAPI implementation is constructed with a TicketAuthority supplied
    by the journal and must call redeem() with request_hash computed by
    contracts.request_hash over the exact outgoing request body/target before any
    transport call; a failed redeem means zero transport attempts.
    """

    def sources_list(
        self,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> tuple[tuple[SourceRecord, ...], str | None]:
        """List connected repository sources."""
        ...

    def sources_get(self, name: str) -> SourceRecord:
        """Get source details by resource name."""
        ...

    def sessions_list(
        self,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> tuple[tuple[SessionRecord, ...], str | None]:
        """List sessions paginated."""
        ...

    def sessions_get(self, name: str) -> SessionRecord:
        """Get session details by resource name."""
        ...

    def activities_list(
        self,
        session_name: str,
        page_token: str | None = None,
        page_size: int = 100,
        create_time_filter: str | None = None,
    ) -> tuple[tuple[ActivityRecord, ...], str | None]:
        """List activities for a session."""
        ...

    def activities_get(self, name: str) -> ActivityRecord:
        """Get activity by resource name."""
        ...

    def sessions_create(
        self,
        ticket: DispatchTicket,
        body: dict[str, Any],
    ) -> MutationResponse:
        """Internal mutation: create task session; requires DispatchTicket.

        Never retries; never raises for transport-level failures (returns outcome
        with uncertain_effect=True for timeout/disconnect/5xx/malformed-or-oversized
        success, and uncertain_effect=False for clear 4xx rejections). Raises only
        for local pre-dispatch validation failures (zero attempts).
        """
        ...

    def sessions_send_message(
        self,
        ticket: DispatchTicket,
        session_name: str,
        body: dict[str, Any],
    ) -> MutationResponse:
        """Internal mutation: send reply message; requires DispatchTicket.

        Never retries; never raises for transport-level failures (returns outcome
        with uncertain_effect=True for timeout/disconnect/5xx/malformed-or-oversized
        success, and uncertain_effect=False for clear 4xx rejections). Raises only
        for local pre-dispatch validation failures (zero attempts).
        """
        ...

    def sessions_approve_plan(
        self,
        ticket: DispatchTicket,
        session_name: str,
    ) -> MutationResponse:
        """Internal mutation: approve plan; requires DispatchTicket.

        Never retries; never raises for transport-level failures (returns outcome
        with uncertain_effect=True for timeout/disconnect/5xx/malformed-or-oversized
        success, and uncertain_effect=False for clear 4xx rejections). Raises only
        for local pre-dispatch validation failures (zero attempts).
        """
        ...


class Store(Protocol):
    """Durable state storage with short transactions and owner locking."""

    def acquire_lock(self, timeout: float = 0.0) -> bool:
        """Acquire exclusive process/workflow owner lock."""
        ...

    def release_lock(self) -> None:
        """Release workflow owner lock."""
        ...

    def get_profile_epoch(self, profile: str) -> int:
        """Get host-recorded epoch for profile."""
        ...

    def save_operation(self, record: OperationRecord) -> None:
        """Persist or update an operation record."""
        ...

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        """Retrieve operation record by ID."""
        ...

    def save_receipt(self, receipt: Receipt) -> None:
        """Persist a receiver/channel receipt."""
        ...

    def get_receipt(self, receipt_id: str) -> Receipt | None:
        """Retrieve receipt by ID."""
        ...

    def save_event(self, event: Event) -> None:
        """Persist an event."""
        ...

    def get_events(self, limit: int = 100) -> tuple[Event, ...]:
        """Retrieve events in order."""
        ...


class RecoveryFence(Protocol):
    """Host-controlled recovery fence outside worker-writable DB."""

    def get_current_epoch(self, profile: str) -> int:
        """Get current host-controlled credential-configuration epoch."""
        ...

    def is_fence_valid(self, profile: str, recorded_epoch: int) -> bool:
        """Check if recorded epoch matches current host epoch."""
        ...

    def get_journal_checkpoint(self, profile: str) -> int:
        """Get host-recorded monotonic journal checkpoint sequence for profile."""
        ...

    def advance_journal_checkpoint(self, profile: str, seq: int) -> None:
        """Advance host-recorded journal checkpoint to seq (monotonic, non-decreasing)."""
        ...


ProfileEpochSource = RecoveryFence


class ReadService(Protocol):
    """Bounded read service for inventory, inspection, and chat collection."""

    def collect(
        self,
        scope: dict[str, Any],
        limits: dict[str, Any],
    ) -> tuple[Any, Coverage]:
        """Collect source/session inventory for scope."""
        ...

    def inspect(self, binding: Binding) -> Any:
        """Inspect specific session binding."""
        ...

    def chats(self, selection: dict[str, Any]) -> Any:
        """Collect activities/messages for selection."""
        ...


@dataclass(frozen=True, slots=True)
class GrantBlocker:
    """Typed blocker returned when grant verification fails."""

    code: ErrorCode
    reason: str


class GrantVerifier(Protocol):
    """Trusted grant verification port binding one exact request."""

    def verify(
        self,
        reference: str,
        prepared_action: PreparedAction,
        current_profile_epoch: int,
    ) -> VerifiedGrant | GrantBlocker:
        """Verify external grant authority for a prepared mutation action."""
        ...


class MutationJournal(Protocol):
    """Single-attempt mutation journal managing dispatch tickets and recovery."""

    def prepare(
        self,
        action: PreparedAction,
        grant: VerifiedGrant,
    ) -> OperationRecord:
        """Record prepared mutation intent."""
        ...

    def begin_dispatch(
        self,
        operation_id: str,
        request_hash: str,
    ) -> DispatchTicket:
        """Commit dispatching state and issue single-use dispatch ticket."""
        ...

    def record_outcome(
        self,
        ticket: DispatchTicket,
        outcome: TransportOutcome,
        evidence: dict[str, Any] | None = None,
    ) -> OperationRecord:
        """Record transport outcome and update operation state."""
        ...

    def get_record(self, operation_id: str) -> OperationRecord | None:
        """Retrieve operation record."""
        ...


class ActionHandler(Protocol):
    """Handler for an individual named operation in a plan."""

    def can_handle(self, op: str) -> bool:
        """Return True if this handler handles the specified op."""
        ...

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
    ) -> ActionResult:
        """Execute the action and return ActionResult."""
        ...


class Receiver(Protocol):
    """Durable event receiver and receipt tracker."""

    def receive_event(self, event: Event) -> Receipt:
        """Accept event durably before ACK."""
        ...

    def ack_event(self, event_id: str) -> bool:
        """Acknowledge event delivery."""
        ...


class Provider(Protocol):
    """Optional external or UI provider (e.g. suggestions, patches)."""

    @property
    def name(self) -> str:
        """Provider name."""
        ...

    def inspect_capability(self) -> Capability:
        """Return capability metadata."""
        ...

    def provide(
        self,
        scope: dict[str, Any],
    ) -> tuple[Any, Coverage]:
        """Provide observations and coverage."""
        ...
