"""Authorization, trusted grant verification, and security boundaries.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from octodot.contracts import Clock, GrantBlocker, GrantVerifier
from octodot.errors import AuthorizationError, ErrorCode, OctodotError
from octodot.models import Binding, PreparedAction, VerifiedGrant


def parse_grant(data: Mapping[str, Any]) -> VerifiedGrant:
    """Parse and validate raw grant dictionary into a VerifiedGrant record.

    Raises OctodotError(ErrorCode.GRANT_INVALID) if required fields are missing
    or invalid types are supplied.
    """
    if not isinstance(data, Mapping):
        raise OctodotError(ErrorCode.GRANT_INVALID, "Grant data must be a dictionary")

    required_str_fields = (
        "action",
        "operation_id",
        "profile",
        "source",
        "repository",
        "branch",
        "payload_hash",
        "context_hash",
        "plan_hash",
        "publication_scope",
        "authorizing_source",
    )

    for field_name in required_str_fields:
        val = data.get(field_name)
        if not isinstance(val, str) or not val.strip():
            raise OctodotError(
                ErrorCode.GRANT_INVALID,
                f"Grant field '{field_name}' must be a non-empty string, got {val!r}",
            )

    epoch = data.get("profile_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise OctodotError(
            ErrorCode.GRANT_INVALID,
            f"Grant field 'profile_epoch' must be a non-negative integer, got {epoch!r}",
        )

    session = data.get("session")
    if session is not None and not isinstance(session, str):
        raise OctodotError(
            ErrorCode.GRANT_INVALID,
            f"Grant field 'session' must be a string or null, got {session!r}",
        )

    expiry = data.get("expiry")
    if expiry is not None and not isinstance(expiry, str):
        raise OctodotError(
            ErrorCode.GRANT_INVALID,
            f"Grant field 'expiry' must be a string or null, got {expiry!r}",
        )

    revocation_ref = data.get("revocation_ref")
    if revocation_ref is not None and not isinstance(revocation_ref, str):
        raise OctodotError(
            ErrorCode.GRANT_INVALID,
            f"Grant field 'revocation_ref' must be a string or null, got {revocation_ref!r}",
        )

    max_attempts = data.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
        raise OctodotError(
            ErrorCode.GRANT_INVALID,
            f"Grant field 'max_attempts' must be an integer, got {max_attempts!r}",
        )

    return VerifiedGrant(
        action=data["action"],
        operation_id=data["operation_id"],
        profile=data["profile"],
        profile_epoch=epoch,
        source=data["source"],
        repository=data["repository"],
        branch=data["branch"],
        payload_hash=data["payload_hash"],
        context_hash=data["context_hash"],
        plan_hash=data["plan_hash"],
        publication_scope=data["publication_scope"],
        authorizing_source=data["authorizing_source"],
        session=session,
        expiry=expiry,
        revocation_ref=revocation_ref,
        max_attempts=max_attempts,
    )


def verify_grant_binding(
    grant: VerifiedGrant,
    prepared_action: PreparedAction,
    current_profile_epoch: int,
    clock: Clock | None = None,
) -> VerifiedGrant | GrantBlocker:
    """Validate every binding field between grant and prepared action.

    Checks:
    - max_attempts == 1
    - Expiry against clock
    - Host-controlled profile epoch matches both grant and action binding
    - Profile name exact match
    - Target source, repository, branch (case-sensitive) and session match
    - Action ID exact match
    - Operation ID exact match
    - Exact payload hash
    - Context hash
    - Plan hash
    - Publication scope
    - Authorizing source non-empty
    """
    if grant.max_attempts != 1:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=f"Grant max_attempts must be exactly 1, got {grant.max_attempts}",
        )

    if grant.expiry is not None:
        try:
            expiry_str = grant.expiry.replace("Z", "+00:00")
            expiry_dt = datetime.fromisoformat(expiry_str)
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        except Exception as err:
            return GrantBlocker(
                code=ErrorCode.GRANT_INVALID,
                reason=f"Malformed expiry timestamp in grant: '{grant.expiry}': {err}",
            )

        now = clock.now_utc() if clock is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        if now >= expiry_dt:
            return GrantBlocker(
                code=ErrorCode.GRANT_EXPIRED,
                reason=f"Grant expired at {grant.expiry} (current time {now.isoformat()})",
            )

    # Check recovery fence / credential configuration epoch
    if grant.profile_epoch != current_profile_epoch:
        return GrantBlocker(
            code=ErrorCode.RECOVERY_FENCE_STALE,
            reason=(
                f"Grant profile epoch {grant.profile_epoch} does not match "
                f"current host epoch {current_profile_epoch}"
            ),
        )

    if prepared_action.binding.profile_epoch != current_profile_epoch:
        return GrantBlocker(
            code=ErrorCode.RECOVERY_FENCE_STALE,
            reason=(
                f"Action binding profile epoch {prepared_action.binding.profile_epoch} "
                f"does not match current host epoch {current_profile_epoch}"
            ),
        )

    # Check profile name
    if grant.profile != prepared_action.binding.profile:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant profile '{grant.profile}' does not match "
                f"action profile '{prepared_action.binding.profile}'"
            ),
        )

    # Check target binding fields
    if grant.source != prepared_action.binding.source:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant source '{grant.source}' does not match "
                f"action source '{prepared_action.binding.source}'"
            ),
        )

    if grant.repository != prepared_action.binding.repository:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant repository '{grant.repository}' does not match "
                f"action repository '{prepared_action.binding.repository}'"
            ),
        )

    # Exact case-sensitive starting branch
    if grant.branch != prepared_action.binding.starting_branch:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant branch '{grant.branch}' does not match "
                f"action branch '{prepared_action.binding.starting_branch}'"
            ),
        )

    # Session match
    if grant.session != prepared_action.binding.session:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant session '{grant.session}' does not match "
                f"action session '{prepared_action.binding.session}'"
            ),
        )

    # Action ID match
    if grant.action != prepared_action.action:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant action '{grant.action}' does not match "
                f"action '{prepared_action.action}'"
            ),
        )

    # Operation ID match
    if grant.operation_id != prepared_action.operation_id:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant operation_id '{grant.operation_id}' does not match "
                f"action operation_id '{prepared_action.operation_id}'"
            ),
        )

    # Payload hash match (exact Unicode, exact newlines)
    if grant.payload_hash != prepared_action.payload_hash:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant payload_hash '{grant.payload_hash}' does not match "
                f"action payload_hash '{prepared_action.payload_hash}'"
            ),
        )

    # Context hash match
    if grant.context_hash != prepared_action.context_hash:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant context_hash '{grant.context_hash}' does not match "
                f"action context_hash '{prepared_action.context_hash}'"
            ),
        )

    # Plan hash match
    if grant.plan_hash != prepared_action.plan_hash:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant plan_hash '{grant.plan_hash}' does not match "
                f"action plan_hash '{prepared_action.plan_hash}'"
            ),
        )

    # Publication scope match
    if grant.publication_scope != prepared_action.publication_scope:
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=(
                f"Grant publication_scope '{grant.publication_scope}' does not match "
                f"action publication_scope '{prepared_action.publication_scope}'"
            ),
        )

    # Authorizing source non-empty
    if not grant.authorizing_source or not grant.authorizing_source.strip():
        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason="Grant authorizing_source is empty or missing",
        )

    return grant


class DisabledGrantVerifier:
    """Default grant verifier implementation that always fails closed.

    Without a coordinator-injected trusted verifier adapter, automated writes
    remain strictly disabled.
    """

    def verify(
        self,
        reference: str,
        prepared_action: PreparedAction,
        current_profile_epoch: int,
    ) -> GrantBlocker:
        return GrantBlocker(
            code=ErrorCode.VERIFIER_UNAVAILABLE,
            reason=(
                "No trusted grant verifier adapter configured; automated writes disabled"
            ),
        )


HostVerifierCallable = Callable[[str, PreparedAction, int], VerifiedGrant | GrantBlocker]


class HostGrantVerifierAdapter:
    """Adapter interface for a host-supplied trusted grant verifier.

    Delegates verification to an externally injected trusted callable or port.
    The runner never constructs one from local files.
    """

    def __init__(self, verifier: HostVerifierCallable | GrantVerifier) -> None:
        if hasattr(verifier, "verify") and callable(getattr(verifier, "verify")):
            self._verifier = verifier.verify
        elif callable(verifier):
            self._verifier = verifier
        else:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                "Host verifier must be callable or implement verify()",
            )

    def verify(
        self,
        reference: str,
        prepared_action: PreparedAction,
        current_profile_epoch: int,
    ) -> VerifiedGrant | GrantBlocker:
        try:
            result = self._verifier(reference, prepared_action, current_profile_epoch)
        except Exception as exc:
            return GrantBlocker(
                code=ErrorCode.VERIFIER_UNAVAILABLE,
                reason=f"Host grant verifier invocation failed: {exc}",
            )

        if isinstance(result, (VerifiedGrant, GrantBlocker)):
            return result

        return GrantBlocker(
            code=ErrorCode.GRANT_INVALID,
            reason=f"Host verifier returned unexpected result type: {type(result).__name__}",
        )


def require_verifier_allowed(verifier: Any, *, live: bool) -> None:
    """Verify that a grant verifier is permitted for the given execution mode.

    Raises OctodotError(ErrorCode.AUTH_DENIED) if live=True and:
    - verifier is marked FIXTURE_ONLY (e.g. FakeGrantVerifier)
    - verifier is neither a HostGrantVerifierAdapter nor DisabledGrantVerifier
    """
    if not live:
        return

    if getattr(verifier, "FIXTURE_ONLY", False):
        raise OctodotError(
            ErrorCode.AUTH_DENIED,
            f"Fixture-only verifier '{type(verifier).__name__}' is barred from live mode",
        )

    if isinstance(verifier, DisabledGrantVerifier):
        return

    if isinstance(verifier, HostGrantVerifierAdapter):
        return

    raise OctodotError(
        ErrorCode.AUTH_DENIED,
        f"Verifier of type '{type(verifier).__name__}' is not permitted in live mode; "
        "only HostGrantVerifierAdapter or DisabledGrantVerifier are allowed",
    )


class FakeGrantVerifier:
    """In-memory grant verifier for offline tests.

    Marked fixture-only via an immutable FIXTURE_ONLY class marker.
    Never exports approvals or touches real credentials.
    """

    FIXTURE_ONLY: bool = True

    def __init__(
        self,
        grants: Mapping[str, VerifiedGrant | Mapping[str, Any]] | None = None,
        clock: Clock | None = None,
        revoked_refs: Sequence[str] | set[str] | None = None,
        single_use: bool = True,
        auto_consume: bool = False,
    ) -> None:
        self.clock = clock
        self.single_use = single_use
        self.auto_consume = auto_consume
        self._grants: dict[str, VerifiedGrant | Mapping[str, Any]] = dict(grants or {})
        self._revoked_refs: set[str] = set(revoked_refs or ())
        self._consumed_refs: set[str] = set()

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("FIXTURE_ONLY", "fixture_only"):
            raise AttributeError(f"Cannot reassign immutable marker '{name}'")
        super().__setattr__(name, value)

    def register_grant(
        self, reference: str, grant: VerifiedGrant | Mapping[str, Any]
    ) -> None:
        """Register an in-memory grant for testing."""
        self._grants[reference] = grant

    def revoke_grant(self, reference_or_revocation_ref: str) -> None:
        """Mark a reference or revocation_ref as revoked."""
        self._revoked_refs.add(reference_or_revocation_ref)

    def mark_consumed(self, reference: str) -> None:
        """Record that a grant has been claimed / consumed for single-attempt dispatch."""
        self._consumed_refs.add(reference)

    def is_consumed(self, reference: str) -> bool:
        """Return True if grant has already been consumed."""
        return reference in self._consumed_refs

    def verify(
        self,
        reference: str,
        prepared_action: PreparedAction,
        current_profile_epoch: int,
    ) -> VerifiedGrant | GrantBlocker:
        """Verify external grant authority for prepared action."""
        # Missing grant check
        if reference not in self._grants:
            return GrantBlocker(
                code=ErrorCode.GRANT_MISSING,
                reason=f"Grant reference '{reference}' not found in authority",
            )

        # Revocation by reference
        if reference in self._revoked_refs:
            return GrantBlocker(
                code=ErrorCode.GRANT_REVOKED,
                reason=f"Grant reference '{reference}' has been revoked",
            )

        # Replay / single-attempt consumption check
        if self.single_use and reference in self._consumed_refs:
            return GrantBlocker(
                code=ErrorCode.GRANT_REVOKED,
                reason=(
                    f"Grant reference '{reference}' has already been consumed "
                    f"(single-attempt limit reached)"
                ),
            )

        raw_grant = self._grants[reference]

        # Parse grant if supplied as dictionary
        if isinstance(raw_grant, VerifiedGrant):
            grant = raw_grant
        elif isinstance(raw_grant, Mapping):
            try:
                grant = parse_grant(raw_grant)
            except OctodotError as err:
                return GrantBlocker(code=err.code, reason=f"Malformed grant: {err.message}")
        else:
            return GrantBlocker(
                code=ErrorCode.GRANT_INVALID,
                reason=f"Malformed grant of type: {type(raw_grant).__name__}",
            )

        # Revocation by revocation_ref
        if grant.revocation_ref is not None and grant.revocation_ref in self._revoked_refs:
            return GrantBlocker(
                code=ErrorCode.GRANT_REVOKED,
                reason=(
                    f"Grant reference '{reference}' revocation_ref "
                    f"'{grant.revocation_ref}' has been revoked"
                ),
            )

        # Validate binding fields and hashes
        verification_result = verify_grant_binding(
            grant=grant,
            prepared_action=prepared_action,
            current_profile_epoch=current_profile_epoch,
            clock=self.clock,
        )

        if isinstance(verification_result, VerifiedGrant) and self.auto_consume:
            self._consumed_refs.add(reference)

        return verification_result
