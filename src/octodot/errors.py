"""Error codes, exceptions, and exit code precedence for octodot.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

import enum
from typing import Iterable


class ErrorCode(str, enum.Enum):
    """Closed error code enumeration for protocol and runtime contracts."""

    # Validation & Syntax
    INVALID_INPUT = "invalid_input"
    DUPLICATE_KEY = "duplicate_key"
    NONFINITE_NUMBER = "nonfinite_number"
    UNKNOWN_FIELD = "unknown_field"
    OVERSIZED_INPUT = "oversized_input"
    INVALID_REFERENCE = "invalid_reference"
    DYNAMIC_MUTATION_TARGET = "dynamic_mutation_target"
    PLACEHOLDER_PRESENT = "placeholder_present"
    TEMPLATE_DISABLED = "template_disabled"
    SCHEMA_TOO_NEW = "schema_too_new"

    # Unsupported features / limitations
    UNSUPPORTED_PUBLIC_API = "unsupported_public_api"
    UNSUPPORTED_EXACT_COMMIT = "unsupported_exact_commit"
    UNSUPPORTED_ATOMIC_PLAN_APPROVAL = "unsupported_atomic_plan_approval"

    # Authorization & Credentials
    AUTH_DENIED = "auth_denied"
    GRANT_MISSING = "grant_missing"
    GRANT_INVALID = "grant_invalid"
    GRANT_EXPIRED = "grant_expired"
    GRANT_REVOKED = "grant_revoked"
    VERIFIER_UNAVAILABLE = "verifier_unavailable"
    RECOVERY_FENCE_STALE = "recovery_fence_stale"

    # Transport & Network
    RATE_LIMITED = "rate_limited"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    UNCERTAIN_EFFECT = "uncertain_effect"
    MALFORMED_RESPONSE = "malformed_response"
    OVERSIZED_RESPONSE = "oversized_response"
    BUDGET_EXHAUSTED = "budget_exhausted"

    # Identity, Scope & Coverage
    PARTIAL_COVERAGE = "partial_coverage"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    BINDING_MISMATCH = "binding_mismatch"
    BRANCH_UNVERIFIED = "branch_unverified"
    UNKNOWN_STATE = "unknown_state"
    ACCEPTED_IDENTITY_UNVERIFIED = "accepted_identity_unverified"

    # State & Journal
    OPERATION_CONFLICT = "operation_conflict"
    UNRESOLVED_INTENT = "unresolved_intent"
    STATE_LOCKED = "state_locked"
    STATE_CORRUPT = "state_corrupt"
    UNSAFE_STATE_DIR = "unsafe_state_dir"
    LOCK_UNSUPPORTED = "lock_unsupported"

    # Lifecycle & System
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class OctodotError(Exception):
    """Base exception for all octodot errors.

    Carries a structured ErrorCode and a sanitized error message.
    Never includes raw response bodies, bearer tokens, or secrets.
    """

    def __init__(self, code: ErrorCode | str, message: str) -> None:
        if isinstance(code, str) and not isinstance(code, ErrorCode):
            try:
                self.code = ErrorCode(code)
            except ValueError:
                self.code = code  # type: ignore[assignment]
        else:
            self.code = code
        self.message = message
        super().__init__(f"[{self.code}] {self.message}")


class ValidationError(OctodotError):
    """Raised when plan, result, or input validation fails."""


class PlanValidationError(ValidationError):
    """Raised when a jules-controller plan fails validation."""


class ResultValidationError(ValidationError):
    """Raised when a jules-controller result fails validation."""


class JsonContractError(ValidationError):
    """Raised when JSON parsing violates strict constraints."""


class ReferenceResolutionError(ValidationError):
    """Raised when an action selection reference is invalid or unsupported."""


class ExecutionEligibilityError(OctodotError):
    """Raised when a plan template is disabled or contains placeholders."""


class AuthorizationError(OctodotError):
    """Raised when grant or recovery fence verification fails."""


class StateStoreError(OctodotError):
    """Raised when durable state operations fail."""


class TransportFailureError(OctodotError):
    """Raised on sanitized transport errors."""


# Exit code constants
EXIT_OK = 0                     # Complete
EXIT_WAITING = 2                # Waiting / yielded
EXIT_FATAL_READ_OR_LOCAL = 3    # Invalid input or fatal read / local failure
EXIT_MUTATION_BLOCKED = 4       # Blocked / rejected / unknown mutation
EXIT_PARTIAL_OR_UNSUPPORTED = 5 # Partial coverage or unsupported operation
EXIT_INTERRUPTED = 130          # Interrupted (SIGINT/SIGTERM)

ALL_EXIT_CODES: frozenset[int] = frozenset({
    EXIT_OK,
    EXIT_WAITING,
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_MUTATION_BLOCKED,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    EXIT_INTERRUPTED,
})

# Precedence order: 130 > 4 > 3 > 5 > 2 > 0
_EXIT_CODE_PRECEDENCE: tuple[int, ...] = (
    EXIT_INTERRUPTED,            # 130
    EXIT_MUTATION_BLOCKED,       # 4
    EXIT_FATAL_READ_OR_LOCAL,    # 3
    EXIT_PARTIAL_OR_UNSUPPORTED, # 5
    EXIT_WAITING,                # 2
    EXIT_OK,                     # 0
)


def combine_exit_codes(codes: Iterable[int]) -> int:
    """Combine exit codes according to precedence: 130 > 4 > 3 > 5 > 2 > 0.

    If codes is empty, returns EXIT_OK (0).
    """
    code_set = set(codes)
    if not code_set:
        return EXIT_OK
    for code in _EXIT_CODE_PRECEDENCE:
        if code in code_set:
            return code
    return max(code_set)
