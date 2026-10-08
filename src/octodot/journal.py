"""Single-attempt mutation journal managing dispatch tickets and recovery.

Standard library only. Compatible with Python 3.10+.
Implements MutationJournal and TicketAuthority protocols on top of SQLiteStore.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
from typing import Any, Mapping, Sequence
import uuid

from octodot.authorization import DisabledGrantVerifier
from octodot.contracts import (
    Clock,
    GrantBlocker,
    GrantVerifier,
    RecoveryFence,
    TicketAuthority,
    canonical_hash,
    request_hash,
)
from octodot.errors import ErrorCode, OctodotError, StateStoreError
from octodot.preparation import compute_mutation_request_hash
from octodot.models import (
    Binding,
    DispatchTicket,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    TransportOutcome,
    VerifiedGrant,
    is_legal_operation_transition,
)
from octodot.transport import SystemClock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_logical_task_marker(payload: Mapping[str, Any] | None) -> str | None:
    """Extract creation logical-task marker from payload dictionary if present."""
    if not isinstance(payload, Mapping):
        return None
    for key in ("logical_task_marker", "task_marker", "marker", "logical_task"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


class Journal(TicketAuthority):
    """Single-attempt mutation journal implementing MutationJournal and TicketAuthority.

    Guarantees:
    - Unique operation ID plus canonical request hash.
    - Same operation ID + same request hash returns recorded outcome.
    - Same operation ID + different request hash raises OPERATION_CONFLICT.
    - New operation IDs cannot bypass unresolved same-session or creation logical-task intent.
    - Commit DISPATCHING state before issuing a single-use DispatchTicket.
    - Recovery on open / restart transitions any in-flight DISPATCHING operations to UNKNOWN.
    - No path from UNKNOWN back to dispatchable; no lease-expiry reset; no retry override.
    - TicketAuthority.redeem(ticket, request_hash) is single-use and durable across restart.
    - Maps TransportOutcome/MutationResponse: uncertain_effect -> UNKNOWN, clear 4xx -> REJECTED,
      2xx -> ACCEPTED; persistence failure leaves operation in UNKNOWN on restart.
    """

    def __init__(
        self,
        store: Any,
        verifier: GrantVerifier | None = None,
        fence: RecoveryFence | None = None,
        clock: Clock | None = None,
        auto_recover: bool = True,
        fault_hook: Any = None,
    ) -> None:
        self.store = store
        self.verifier = verifier
        self.fence = fence or getattr(store, "fence", None) or getattr(store, "_fence", None)
        self.clock = clock or SystemClock()
        self._fault_hook = fault_hook
        self._in_memory_tickets: dict[str, dict[str, Any]] = {}

        if hasattr(self.store, "_conn") and self.store._conn is not None:
            self._init_ticket_table()

        if auto_recover:
            self.recover()

    def set_fault_hook(self, hook: Any) -> None:
        """Set deterministic fault injection hook for testing boundaries."""
        self._fault_hook = hook

    def _init_ticket_table(self) -> None:
        """Ensure durable dispatch tickets table exists in SQLite database."""
        with self.store.transaction():
            self.store._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dispatch_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.store._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dispatch_tickets_op ON dispatch_tickets (operation_id)"
            )

    def recover(self) -> int:
        """Scan for abandoned DISPATCHING operations and transition them to UNKNOWN.

        Returns number of operations transitioned.
        """
        count = 0
        if hasattr(self.store, "_conn") and self.store._conn is not None:
            now = _utc_now_iso()
            cursor = self.store._conn.cursor()
            cursor.execute(
                "SELECT operation_id FROM operations WHERE state = ?",
                (OperationState.DISPATCHING.value,),
            )
            rows = cursor.fetchall()
            for row in rows:
                op_id = row["operation_id"]
                try:
                    self.store.transition_operation_state(
                        op_id,
                        OperationState.UNKNOWN,
                        fence=self.fence,
                    )
                    count += 1
                except Exception:
                    pass

            # Invalidate any unconsumed tickets for recovered operations
            with self.store.transaction():
                self.store._conn.execute(
                    """
                    UPDATE dispatch_tickets
                    SET consumed = 1, consumed_at = ?
                    WHERE consumed = 0
                      AND operation_id IN (
                          SELECT operation_id FROM operations WHERE state = ?
                      )
                    """,
                    (now, OperationState.UNKNOWN.value),
                )
        return count

    # =========================================================================
    # Gating and Conflict Detection
    # =========================================================================

    def _is_operation_desired_state_resolved(self, operation_id: str) -> bool:
        """Return True if operation has a desired_state_resolution evidence record."""
        if not hasattr(self.store, "_conn") or self.store._conn is None:
            return False
        cursor = self.store._conn.cursor()
        cursor.execute(
            """
            SELECT evidence_id FROM operation_evidence
            WHERE operation_id = ? AND key = 'desired_state_resolution'
            LIMIT 1
            """,
            (operation_id,),
        )
        return cursor.fetchone() is not None

    def _get_operation_predecessor(self, operation_id: str) -> str | None:
        """Retrieve predecessor_operation_id from evidence if recorded."""
        if not hasattr(self.store, "_conn") or self.store._conn is None:
            return None
        cursor = self.store._conn.cursor()
        cursor.execute(
            """
            SELECT value_json FROM operation_evidence
            WHERE operation_id = ? AND key = 'predecessor_operation_id'
            ORDER BY evidence_id ASC LIMIT 1
            """,
            (operation_id,),
        )
        row = cursor.fetchone()
        if row:
            try:
                val = json.loads(row["value_json"])
                if isinstance(val, str):
                    return val
            except Exception:
                pass
        return None

    def _check_unresolved_conflicts(
        self,
        current_op_id: str,
        session: str | None,
        marker: str | None,
        predecessor_operation_id: str | None = None,
    ) -> None:
        """Check for unresolved same-session intent or logical-task marker.

        Raises OctodotError(ErrorCode.OPERATION_CONFLICT) if a conflict exists.
        An UNKNOWN operation is treated as unresolved UNLESS it has a desired_state_resolution record.
        A resolved UNKNOWN operation still blocks a new ID UNLESS the new operation carries
        predecessor_operation_id pointing at the resolved operation.
        """
        if not hasattr(self.store, "_conn") or self.store._conn is None:
            return

        cursor = self.store._conn.cursor()

        # 1. Unresolved same-session intent
        if session:
            clean_session = session.strip()
            cursor.execute(
                """
                SELECT operation_id, state, effect_observed
                FROM operations
                WHERE session = ? AND operation_id != ?
                """,
                (clean_session, current_op_id),
            )
            for row in cursor.fetchall():
                other_id = row["operation_id"]
                st = OperationState(row["state"])
                effect_obs = bool(row["effect_observed"])

                if st in (OperationState.PREPARED, OperationState.DISPATCHING) or (
                    st == OperationState.ACCEPTED and not effect_obs
                ):
                    raise OctodotError(
                        ErrorCode.OPERATION_CONFLICT,
                        f"Unresolved same-session operation '{other_id}' "
                        f"(state={st.value}) exists for session '{clean_session}'",
                    )
                if st == OperationState.UNKNOWN:
                    is_resolved = self._is_operation_desired_state_resolved(other_id)
                    if not is_resolved:
                        raise OctodotError(
                            ErrorCode.OPERATION_CONFLICT,
                            f"Unresolved same-session operation '{other_id}' "
                            f"(state={st.value}) exists for session '{clean_session}'",
                        )
                    if predecessor_operation_id != other_id:
                        raise OctodotError(
                            ErrorCode.OPERATION_CONFLICT,
                            f"Resolved operation '{other_id}' for session '{clean_session}' "
                            f"requires predecessor_operation_id linkage on new operations",
                        )

        # 2. Unresolved creation logical-task marker
        if marker:
            clean_marker = marker.strip()
            marker_json = json.dumps(clean_marker)
            cursor.execute(
                """
                SELECT o.operation_id, o.state, o.effect_observed
                FROM operations o
                JOIN operation_evidence e ON o.operation_id = e.operation_id
                WHERE e.key IN ('logical_task_marker', 'marker')
                  AND e.value_json = ?
                  AND o.operation_id != ?
                """,
                (marker_json, current_op_id),
            )
            for row in cursor.fetchall():
                other_id = row["operation_id"]
                st = OperationState(row["state"])
                effect_obs = bool(row["effect_observed"])
                if st in (OperationState.PREPARED, OperationState.DISPATCHING) or (
                    st == OperationState.ACCEPTED and not effect_obs
                ):
                    raise OctodotError(
                        ErrorCode.OPERATION_CONFLICT,
                        f"Unresolved creation logical-task marker '{clean_marker}' "
                        f"exists under operation '{other_id}' (state={st.value})",
                    )
                if st == OperationState.UNKNOWN:
                    is_resolved = self._is_operation_desired_state_resolved(other_id)
                    if not is_resolved:
                        raise OctodotError(
                            ErrorCode.OPERATION_CONFLICT,
                            f"Unresolved creation logical-task marker '{clean_marker}' "
                            f"exists under operation '{other_id}' (state={st.value})",
                        )
                    if predecessor_operation_id != other_id:
                        raise OctodotError(
                            ErrorCode.OPERATION_CONFLICT,
                            f"Resolved creation operation '{other_id}' for marker '{clean_marker}' "
                            f"requires predecessor_operation_id linkage on new operations",
                        )

    def _get_operation_marker(self, operation_id: str) -> str | None:
        """Retrieve logical task marker from operation evidence if recorded."""
        if not hasattr(self.store, "_conn") or self.store._conn is None:
            return None
        cursor = self.store._conn.cursor()
        cursor.execute(
            """
            SELECT value_json FROM operation_evidence
            WHERE operation_id = ? AND key IN ('logical_task_marker', 'marker')
            ORDER BY evidence_id ASC LIMIT 1
            """,
            (operation_id,),
        )
        row = cursor.fetchone()
        if row:
            try:
                val = json.loads(row["value_json"])
                if isinstance(val, str):
                    return val
            except Exception:
                pass
        return None

    # =========================================================================
    # MutationJournal Protocol Implementation
    # =========================================================================

    def prepare(
        self,
        action: PreparedAction,
        grant: VerifiedGrant | None = None,
        *,
        predecessor_operation_id: str | None = None,
        authorization_ref: str | None = None,
    ) -> OperationRecord:
        """Record prepared mutation intent.

        - Same operation_id + same request_hash returns recorded state.
        - Same operation_id + different request_hash raises OPERATION_CONFLICT.
        - New operation_id cannot bypass unresolved same-session or logical-task effect.
        - If GrantVerifier is DisabledGrantVerifier or rejects grant, records as BLOCKED_BEFORE_DISPATCH.
        - Validates predecessor_operation_id linkage if provided.
        - Preserves authorization_ref and PreparedAction context for dispatch re-validation.
        """
        # 1. Check existing record
        existing = self.store.get_operation(action.operation_id)
        if existing is not None:
            if existing.request_hash == action.request_hash:
                return existing
            raise OctodotError(
                ErrorCode.OPERATION_CONFLICT,
                f"Operation '{action.operation_id}' already exists with different request hash "
                f"'{existing.request_hash}' != '{action.request_hash}'",
            )

        # 2. Validate predecessor_operation_id if provided
        if predecessor_operation_id is not None:
            pred_record = self.store.get_operation(predecessor_operation_id)
            if pred_record is None:
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    f"Predecessor operation '{predecessor_operation_id}' not found",
                )
            if not self._is_operation_desired_state_resolved(predecessor_operation_id):
                raise OctodotError(
                    ErrorCode.OPERATION_CONFLICT,
                    f"Predecessor operation '{predecessor_operation_id}' is not desired-state resolved",
                )
            # Must match the same session / logical task
            pred_session = pred_record.binding.session if pred_record.binding else None
            action_session = action.binding.session if action.binding else None
            pred_marker = self._get_operation_marker(predecessor_operation_id)
            action_marker = extract_logical_task_marker(action.payload)
            if action_session:
                if pred_session != action_session:
                    raise OctodotError(
                        ErrorCode.BINDING_MISMATCH,
                        f"Predecessor operation '{predecessor_operation_id}' session '{pred_session}' "
                        f"does not match action session '{action_session}'",
                    )
            elif action_marker:
                if pred_marker != action_marker:
                    raise OctodotError(
                        ErrorCode.BINDING_MISMATCH,
                        f"Predecessor operation '{predecessor_operation_id}' marker '{pred_marker}' "
                        f"does not match action marker '{action_marker}'",
                    )

        # 3. Check gating for unresolved conflicts (same-session / logical-task)
        session = action.binding.session if action.binding else None
        marker = extract_logical_task_marker(action.payload)

        self._check_unresolved_conflicts(
            action.operation_id,
            session,
            marker,
            predecessor_operation_id=predecessor_operation_id,
        )

        # 4. Check grant verifier if configured
        target_state = OperationState.PREPARED
        error_code: ErrorCode | None = None
        evidence_list: list[tuple[str, Any]] = []

        if marker:
            evidence_list.append(("logical_task_marker", marker))
        if predecessor_operation_id:
            evidence_list.append(("predecessor_operation_id", predecessor_operation_id))

        # Determine target if possible
        target: str | None = None
        if action.action == "tasks.create":
            target = "/v1alpha/sessions"
        elif action.binding and action.binding.session:
            sess = action.binding.session
            clean_sess = sess if sess.startswith("sessions/") else f"sessions/{sess}"
            if action.action == "chats.reply":
                target = f"/v1alpha/{clean_sess}:sendMessage"
            elif action.action == "plans.approve":
                target = f"/v1alpha/{clean_sess}:approvePlan"

        # Store full action context for dispatch boundary re-validation
        prepared_action_dict = {
            "action": action.action,
            "operation_id": action.operation_id,
            "target": target,
            "binding": {
                "profile": action.binding.profile,
                "profile_epoch": action.binding.profile_epoch,
                "source": action.binding.source,
                "repository": action.binding.repository,
                "starting_branch": action.binding.starting_branch,
                "session": action.binding.session,
            } if action.binding else None,
            "payload": dict(action.payload) if action.payload is not None else {},
            "payload_hash": action.payload_hash,
            "context_hash": action.context_hash,
            "request_hash": action.request_hash,
            "publication_scope": action.publication_scope,
            "plan_hash": action.plan_hash,
        }
        evidence_list.append(("prepared_action", prepared_action_dict))

        # Requirement 1: verifier=None or DisabledGrantVerifier fails closed
        if self.verifier is None or isinstance(self.verifier, DisabledGrantVerifier):
            target_state = OperationState.BLOCKED_BEFORE_DISPATCH
            error_code = ErrorCode.VERIFIER_UNAVAILABLE
        else:
            # Active verifier: Requirement 2: Remove authorizing_source fallback.
            # Missing or empty authorization_ref gives BLOCKED_BEFORE_DISPATCH with GRANT_MISSING
            clean_auth_ref = authorization_ref.strip() if isinstance(authorization_ref, str) else None
            if not clean_auth_ref:
                target_state = OperationState.BLOCKED_BEFORE_DISPATCH
                error_code = ErrorCode.GRANT_MISSING
            elif grant is None:
                target_state = OperationState.BLOCKED_BEFORE_DISPATCH
                error_code = ErrorCode.GRANT_INVALID
            else:
                evidence_list.append(("authorization_ref", clean_auth_ref))
                epoch = action.binding.profile_epoch if action.binding else 0
                res = self.verifier.verify(
                    clean_auth_ref,
                    action,
                    epoch,
                )
                if isinstance(res, GrantBlocker):
                    target_state = OperationState.BLOCKED_BEFORE_DISPATCH
                    error_code = res.code

        # Store action prompt/body in evidence for reconciliation matching
        if action.payload:
            evidence_list.append(("payload", dict(action.payload)))
            if "prompt" in action.payload:
                evidence_list.append(("prompt", str(action.payload["prompt"])))
            elif "text" in action.payload:
                evidence_list.append(("prompt", str(action.payload["text"])))

        now = _utc_now_iso()
        record = OperationRecord(
            operation_id=action.operation_id,
            state=target_state,
            request_hash=action.request_hash,
            binding=action.binding,
            error_code=error_code,
            created_at=now,
            updated_at=now,
            evidence=tuple(evidence_list),
        )

        # Persist authorization record if provided
        if grant is not None and hasattr(self.store, "save_authorization_record"):
            try:
                self.store.save_authorization_record(grant)
            except Exception:
                pass

        # Persist operation record (insert-only)
        if self._fault_hook:
            self._fault_hook("before_intent_commit")
        self.store.save_operation(record, fence=self.fence)
        if self._fault_hook:
            self._fault_hook("after_intent_commit")
        saved = self.store.get_operation(action.operation_id)
        return saved or record

    def begin_dispatch(
        self,
        operation_id: str,
        request_hash: str,
    ) -> DispatchTicket:
        """Commit dispatching state and issue single-use dispatch ticket.

        Gating rules:
        - Recovery fence / profile epoch must be valid.
        - Verifier must authorize dispatch (DisabledGrantVerifier blocks).
        - No unresolved same-session or creation logical-task intent.
        - Must be in PREPARED state with matching request_hash.
        - Commits DISPATCHING state before returning ticket.
        """
        # 1. Retrieve operation
        op = self.store.get_operation(operation_id)
        if op is None:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Operation '{operation_id}' not found for dispatch",
            )

        if op.request_hash != request_hash:
            raise OctodotError(
                ErrorCode.OPERATION_CONFLICT,
                f"Operation '{operation_id}' request hash mismatch: '{op.request_hash}' != '{request_hash}'",
            )

        if op.state != OperationState.PREPARED:
            if op.state in (OperationState.UNKNOWN, OperationState.DISPATCHING):
                raise OctodotError(
                    ErrorCode.UNRESOLVED_INTENT,
                    f"Operation '{operation_id}' is in unresolved state '{op.state.value}'",
                )
            if op.state == OperationState.BLOCKED_BEFORE_DISPATCH:
                err = op.error_code or ErrorCode.AUTH_DENIED
                raise OctodotError(
                    err,
                    f"Operation '{operation_id}' is in terminal state '{op.state.value}': {err}",
                )
            # Terminal states: ACCEPTED, EFFECT_OBSERVED, REJECTED, CANCELLED_BEFORE_DISPATCH
            err_code = op.error_code or op.state.value
            raise OctodotError(
                err_code,
                f"Operation '{operation_id}' is in terminal state '{op.state.value}'",
            )

        # 2. Recovery fence and profile epoch validity check
        profile = op.binding.profile if op.binding else "default"
        op_epoch = op.binding.profile_epoch if op.binding else 0
        if self.fence is not None:
            current_fence_epoch = self.fence.get_current_epoch(profile)
            if op_epoch != current_fence_epoch or not self.fence.is_fence_valid(profile, op_epoch):
                self.store.transition_operation_state(
                    operation_id,
                    OperationState.BLOCKED_BEFORE_DISPATCH,
                    error_code=ErrorCode.RECOVERY_FENCE_STALE,
                    fence=self.fence,
                )
                raise OctodotError(
                    ErrorCode.RECOVERY_FENCE_STALE,
                    f"Recovery fence epoch mismatch for profile '{profile}': operation epoch {op_epoch} != current fence epoch {current_fence_epoch}",
                )
        else:
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.RECOVERY_FENCE_STALE,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.RECOVERY_FENCE_STALE,
                f"No trusted recovery fence configured for profile '{profile}'",
            )

        if hasattr(self.store, "check_mutation_eligibility"):
            try:
                self.store.check_mutation_eligibility(profile, fence=self.fence)
            except StateStoreError as err:
                self.store.transition_operation_state(
                    operation_id,
                    OperationState.BLOCKED_BEFORE_DISPATCH,
                    error_code=err.code,
                    fence=self.fence,
                )
                raise OctodotError(err.code, err.message) from err

        # 3. Grant verifier check & dispatch boundary re-validation
        # Requirement 1: verifier=None or DisabledGrantVerifier fails closed
        if self.verifier is None or isinstance(self.verifier, DisabledGrantVerifier):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.VERIFIER_UNAVAILABLE,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.VERIFIER_UNAVAILABLE,
                "Automated writes disabled: no trusted grant verifier configured",
            )

        auth_ref: str | None = None
        action_dict: dict[str, Any] | None = None
        for k, v in op.evidence:
            if k == "authorization_ref" and isinstance(v, str):
                auth_ref = v
            elif k == "prepared_action" and isinstance(v, dict):
                action_dict = v

        # Fail closed if authorization context cannot be recovered
        if not auth_ref:
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_MISSING,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_MISSING,
                f"Missing authorization_ref evidence for operation '{operation_id}' at dispatch boundary",
            )

        if action_dict is None or not isinstance(action_dict, dict):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_MISSING,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_MISSING,
                f"Missing prepared_action evidence for operation '{operation_id}' at dispatch boundary",
            )

        # Requirement 3: Dispatch-boundary rebuild: no .get(default) for required fields
        required_action_str_fields = (
            "action",
            "operation_id",
            "payload_hash",
            "context_hash",
            "request_hash",
            "publication_scope",
            "plan_hash",
        )
        for field_name in required_action_str_fields:
            if field_name not in action_dict:
                self.store.transition_operation_state(
                    operation_id,
                    OperationState.BLOCKED_BEFORE_DISPATCH,
                    error_code=ErrorCode.GRANT_MISSING,
                    fence=self.fence,
                )
                raise OctodotError(
                    ErrorCode.GRANT_MISSING,
                    f"Missing required field '{field_name}' in prepared_action evidence for '{operation_id}'",
                )
            val = action_dict[field_name]
            if not isinstance(val, str) or (field_name != "publication_scope" and not val):
                self.store.transition_operation_state(
                    operation_id,
                    OperationState.BLOCKED_BEFORE_DISPATCH,
                    error_code=ErrorCode.GRANT_INVALID,
                    fence=self.fence,
                )
                raise OctodotError(
                    ErrorCode.GRANT_INVALID,
                    f"Invalid type or empty value for field '{field_name}' in prepared_action evidence",
                )

        if "payload" not in action_dict:
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_MISSING,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_MISSING,
                f"Missing 'payload' in prepared_action evidence for '{operation_id}'",
            )
        if not isinstance(action_dict["payload"], (dict, Mapping)):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_INVALID,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_INVALID,
                f"Invalid 'payload' type in prepared_action evidence for '{operation_id}'",
            )

        if "binding" not in action_dict:
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_MISSING,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_MISSING,
                f"Missing 'binding' in prepared_action evidence for '{operation_id}'",
            )
        b_data = action_dict["binding"]
        if not isinstance(b_data, dict):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_INVALID,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_INVALID,
                f"Invalid 'binding' type in prepared_action evidence for '{operation_id}'",
            )

        required_binding_fields = (
            "profile",
            "profile_epoch",
            "source",
            "repository",
            "starting_branch",
            "session",
        )
        for b_field in required_binding_fields:
            if b_field not in b_data:
                self.store.transition_operation_state(
                    operation_id,
                    OperationState.BLOCKED_BEFORE_DISPATCH,
                    error_code=ErrorCode.GRANT_MISSING,
                    fence=self.fence,
                )
                raise OctodotError(
                    ErrorCode.GRANT_MISSING,
                    f"Missing binding field '{b_field}' in prepared_action evidence for '{operation_id}'",
                )

        if not isinstance(b_data["profile"], str) or not b_data["profile"]:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, "Invalid profile in binding evidence")
        if not isinstance(b_data["profile_epoch"], int) or isinstance(b_data["profile_epoch"], bool) or b_data["profile_epoch"] < 0:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, "Invalid profile_epoch in binding evidence")
        if not isinstance(b_data["source"], str) or not b_data["source"]:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, "Invalid source in binding evidence")
        if not isinstance(b_data["repository"], str) or not b_data["repository"]:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, "Invalid repository in binding evidence")
        if b_data["starting_branch"] is not None and not isinstance(b_data["starting_branch"], str):
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, "Invalid starting_branch in binding evidence")
        if b_data["session"] is not None and not isinstance(b_data["session"], str):
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, "Invalid session in binding evidence")

        reconstructed_binding = Binding(
            profile=b_data["profile"],
            profile_epoch=b_data["profile_epoch"],
            source=b_data["source"],
            repository=b_data["repository"],
            starting_branch=b_data["starting_branch"],
            session=b_data["session"],
        )
        reconstructed_action = PreparedAction(
            action=action_dict["action"],
            operation_id=action_dict["operation_id"],
            binding=reconstructed_binding,
            payload=dict(action_dict["payload"]),
            payload_hash=action_dict["payload_hash"],
            context_hash=action_dict["context_hash"],
            request_hash=action_dict["request_hash"],
            publication_scope=action_dict["publication_scope"],
            plan_hash=action_dict["plan_hash"],
        )

        # Cross-checks against op record
        if reconstructed_action.operation_id != op.operation_id:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, f"Evidence operation_id '{reconstructed_action.operation_id}' != '{op.operation_id}'")

        if reconstructed_action.request_hash != op.request_hash:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, f"Evidence request_hash '{reconstructed_action.request_hash}' != '{op.request_hash}'")

        if op.binding is None:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.BINDING_MISMATCH, fence=self.fence)
            raise OctodotError(ErrorCode.BINDING_MISMATCH, f"Operation '{operation_id}' lacks store binding record")

        # Field-by-field binding comparison
        if (
            reconstructed_binding.profile != op.binding.profile
            or reconstructed_binding.profile_epoch != op.binding.profile_epoch
            or reconstructed_binding.source != op.binding.source
            or reconstructed_binding.repository != op.binding.repository
            or reconstructed_binding.starting_branch != op.binding.starting_branch
            or reconstructed_binding.session != op.binding.session
        ):
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.BINDING_MISMATCH, fence=self.fence)
            raise OctodotError(ErrorCode.BINDING_MISMATCH, f"Reconstructed binding does not match store binding for '{operation_id}'")

        # Recompute payload_hash
        recomputed_payload_hash = canonical_hash(reconstructed_action.payload)
        if recomputed_payload_hash != reconstructed_action.payload_hash:
            self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
            raise OctodotError(ErrorCode.GRANT_INVALID, f"Payload hash mismatch: recomputed '{recomputed_payload_hash}' != stored '{reconstructed_action.payload_hash}'")

        # Recompute request_hash
        target = action_dict.get("target")
        if not target:
            if reconstructed_action.action == "tasks.create":
                target = "/v1alpha/sessions"
            elif reconstructed_action.binding.session:
                clean_sess = reconstructed_action.binding.session if reconstructed_action.binding.session.startswith("sessions/") else f"sessions/{reconstructed_action.binding.session}"
                if reconstructed_action.action == "chats.reply":
                    target = f"/v1alpha/{clean_sess}:sendMessage"
                elif reconstructed_action.action == "plans.approve":
                    target = f"/v1alpha/{clean_sess}:approvePlan"
        if target:
            recomputed_req_hash = compute_mutation_request_hash(target, reconstructed_action.action, reconstructed_action.payload)
            if recomputed_req_hash != reconstructed_action.request_hash:
                self.store.transition_operation_state(operation_id, OperationState.BLOCKED_BEFORE_DISPATCH, error_code=ErrorCode.GRANT_INVALID, fence=self.fence)
                raise OctodotError(ErrorCode.GRANT_INVALID, f"Request hash mismatch: recomputed '{recomputed_req_hash}' != stored '{reconstructed_action.request_hash}'")

        current_epoch = self.fence.get_current_epoch(profile)
        recheck_res = self.verifier.verify(auth_ref, reconstructed_action, current_epoch)
        if isinstance(recheck_res, GrantBlocker):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=recheck_res.code,
                fence=self.fence,
            )
            raise OctodotError(
                recheck_res.code,
                f"Grant verification failed at dispatch boundary: {recheck_res.reason}",
            )
        if not isinstance(recheck_res, VerifiedGrant):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.GRANT_INVALID,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.GRANT_INVALID,
                "Grant verifier did not return VerifiedGrant at dispatch boundary",
            )

        # 4. Check unresolved conflicts
        session = op.binding.session if op.binding else None
        marker = self._get_operation_marker(operation_id)
        predecessor_op_id = self._get_operation_predecessor(operation_id)
        self._check_unresolved_conflicts(
            operation_id,
            session,
            marker,
            predecessor_operation_id=predecessor_op_id,
        )

        # 5. Mint ticket
        ticket_id = f"ticket-{uuid.uuid4().hex[:12]}"
        nonce = secrets.token_hex(16)
        now = _utc_now_iso()

        ticket = DispatchTicket(
            ticket_id=ticket_id,
            operation_id=operation_id,
            request_hash=request_hash,
            nonce=nonce,
            created_at=now,
        )

        # 6. Commit DISPATCHING state and persist ticket BEFORE returning ticket
        if self._fault_hook:
            self._fault_hook("before_dispatching_commit")

        if hasattr(self.store, "_conn") and self.store._conn is not None:
            # First insert ticket record into SQLite
            with self.store.transaction():
                self.store._conn.execute(
                    """
                    INSERT INTO dispatch_tickets (
                        ticket_id, operation_id, request_hash, nonce, consumed, created_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    """,
                    (ticket_id, operation_id, request_hash, nonce, now),
                )
        else:
            self._in_memory_tickets[ticket_id] = {
                "operation_id": operation_id,
                "request_hash": request_hash,
                "nonce": nonce,
                "consumed": False,
                "created_at": now,
            }

        # Transition operation to DISPATCHING with ticket_id
        self.store.transition_operation_state(
            operation_id,
            OperationState.DISPATCHING,
            ticket_id=ticket_id,
            fence=self.fence,
        )

        if self._fault_hook:
            self._fault_hook("after_dispatching_commit")

        return ticket

    def record_outcome(
        self,
        ticket: DispatchTicket,
        outcome: TransportOutcome | MutationResponse,
        evidence: dict[str, Any] | None = None,
    ) -> OperationRecord:
        """Record transport outcome and update operation state.

        - uncertain_effect => UNKNOWN
        - clear 4xx rejection (not uncertain) => REJECTED (no automatic retry)
        - 2xx success (not uncertain) => ACCEPTED
        - persistence failure leaves operation in UNKNOWN on restart.
        """
        if isinstance(outcome, MutationResponse):
            raw_outcome = outcome.outcome
            session_record = outcome.session
        elif isinstance(outcome, TransportOutcome):
            raw_outcome = outcome
            session_record = None
        else:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Invalid outcome type: {type(outcome).__name__}",
            )

        # Check if this is a create mutation
        is_create_mutation = False
        if evidence and (
            evidence.get("mutation_kind") == "tasks.create"
            or ("repository" in evidence and "starting_branch" in evidence)
        ):
            is_create_mutation = True
        elif hasattr(self.store, "get_operation"):
            op_rec = self.store.get_operation(ticket.operation_id)
            if op_rec and op_rec.binding and op_rec.binding.session is None:
                is_create_mutation = True

        if raw_outcome.uncertain_effect:
            to_state = OperationState.UNKNOWN
            error_code = raw_outcome.sanitized_error_code or ErrorCode.TRANSPORT_ERROR
            api_accepted = False
        elif (
            raw_outcome.status in (200, 201)
            and is_create_mutation
            and (session_record is None or not getattr(session_record, "name", None))
        ):
            to_state = OperationState.UNKNOWN
            error_code = ErrorCode.MALFORMED_RESPONSE
            api_accepted = False
        elif 400 <= raw_outcome.status < 500:
            to_state = OperationState.REJECTED
            error_code = raw_outcome.sanitized_error_code or ErrorCode.INVALID_INPUT
            api_accepted = False
        elif raw_outcome.status in (200, 201):
            to_state = OperationState.ACCEPTED
            error_code = None
            api_accepted = True
        else:
            to_state = OperationState.UNKNOWN
            error_code = raw_outcome.sanitized_error_code or ErrorCode.TRANSPORT_ERROR
            api_accepted = False

        evidence_entry = None
        if session_record is not None:
            sess_dict = (
                session_record.to_dict()
                if hasattr(session_record, "to_dict")
                else {"name": getattr(session_record, "name", "")}
            )
            evidence_entry = ("session_record", sess_dict)

        if self._fault_hook:
            self._fault_hook("during_acceptance_persistence")

        rec = self.store.transition_operation_state(
            ticket.operation_id,
            to_state,
            api_accepted=api_accepted,
            error_code=error_code,
            evidence_entry=evidence_entry,
            fence=self.fence,
        )

        if evidence:
            for k, v in evidence.items():
                self.store.append_operation_evidence(ticket.operation_id, k, v)

        if self._fault_hook:
            self._fault_hook("after_acceptance_persistence")

        updated = self.store.get_operation(ticket.operation_id)
        return updated or rec

    def get_record(self, operation_id: str) -> OperationRecord | None:
        """Retrieve operation record."""
        return self.store.get_operation(operation_id)

    # =========================================================================
    # TicketAuthority Protocol Implementation
    # =========================================================================

    def redeem(self, ticket: DispatchTicket, request_hash: str) -> bool:
        """Atomically validate ticket authority and mark single use.

        Returns True if valid and consumed; False if ticket was already consumed,
        unrecognized, or request_hash does not match.
        Ticket is durable across restart.
        """
        if not isinstance(ticket, DispatchTicket):
            return False

        if ticket.request_hash != request_hash:
            return False

        now = _utc_now_iso()

        if hasattr(self.store, "_conn") and self.store._conn is not None:
            with self.store.transaction():
                cursor = self.store._conn.cursor()
                cursor.execute(
                    """
                    SELECT operation_id, request_hash, nonce, consumed
                    FROM dispatch_tickets
                    WHERE ticket_id = ?
                    """,
                    (ticket.ticket_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                if row["consumed"] == 1:
                    return False
                if (
                    row["operation_id"] != ticket.operation_id
                    or row["request_hash"] != ticket.request_hash
                    or row["nonce"] != ticket.nonce
                ):
                    return False

                # Check operation state in operations table: must still be DISPATCHING and ticket_id must match
                cursor.execute(
                    "SELECT state, ticket_id FROM operations WHERE operation_id = ?",
                    (ticket.operation_id,),
                )
                op_row = cursor.fetchone()
                if op_row is None:
                    return False
                if op_row["state"] != OperationState.DISPATCHING.value:
                    return False
                if not op_row["ticket_id"] or op_row["ticket_id"] != ticket.ticket_id:
                    return False

                # Atomically mark ticket consumed
                cursor.execute(
                    """
                    UPDATE dispatch_tickets
                    SET consumed = 1, consumed_at = ?
                    WHERE ticket_id = ? AND consumed = 0
                    """,
                    (now, ticket.ticket_id),
                )
                if cursor.rowcount != 1:
                    return False
                return True
        else:
            entry = self._in_memory_tickets.get(ticket.ticket_id)
            if entry is None:
                return False
            if entry["consumed"]:
                return False
            if (
                entry["operation_id"] != ticket.operation_id
                or entry["request_hash"] != ticket.request_hash
                or entry["nonce"] != ticket.nonce
            ):
                return False

            op = self.store.get_operation(ticket.operation_id)
            if op is None or op.state != OperationState.DISPATCHING:
                return False
            if not getattr(op, "ticket_id", None) or op.ticket_id != ticket.ticket_id:
                return False

            entry["consumed"] = True
            entry["consumed_at"] = now
            return True

    # =========================================================================
    # Pre-dispatch cancellation helpers
    # =========================================================================

    def cancel_before_dispatch(
        self, operation_id: str, reason: str = ""
    ) -> OperationRecord:
        """Cancel prepared operation before dispatch."""
        now = _utc_now_iso()
        entry = ("cancellation", {"reason": reason, "timestamp": now}) if reason else None
        return self.store.transition_operation_state(
            operation_id,
            OperationState.CANCELLED_BEFORE_DISPATCH,
            evidence_entry=entry,
            fence=self.fence,
        )

    def block_before_dispatch(
        self, operation_id: str, error_code: ErrorCode, reason: str = ""
    ) -> OperationRecord:
        """Block prepared operation before dispatch."""
        now = _utc_now_iso()
        entry = ("blocker", {"reason": reason, "timestamp": now}) if reason else None
        return self.store.transition_operation_state(
            operation_id,
            OperationState.BLOCKED_BEFORE_DISPATCH,
            error_code=error_code,
            evidence_entry=entry,
            fence=self.fence,
        )
