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
    request_hash,
)
from octodot.errors import ErrorCode, OctodotError, StateStoreError
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
        self.fence = fence
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
    ) -> OperationRecord:
        """Record prepared mutation intent.

        - Same operation_id + same request_hash returns recorded state.
        - Same operation_id + different request_hash raises OPERATION_CONFLICT.
        - New operation_id cannot bypass unresolved same-session or logical-task effect.
        - If GrantVerifier is DisabledGrantVerifier or rejects grant, records as BLOCKED_BEFORE_DISPATCH.
        - Validates predecessor_operation_id linkage if provided.
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

        if isinstance(self.verifier, DisabledGrantVerifier):
            target_state = OperationState.BLOCKED_BEFORE_DISPATCH
            error_code = ErrorCode.VERIFIER_UNAVAILABLE
        elif self.verifier is not None:
            if grant is None:
                target_state = OperationState.BLOCKED_BEFORE_DISPATCH
                error_code = ErrorCode.GRANT_INVALID
            else:
                epoch = action.binding.profile_epoch if action.binding else 0
                res = self.verifier.verify(
                    grant.authorizing_source or "grant",
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
        if hasattr(self.store, "check_mutation_eligibility"):
            try:
                self.store.check_mutation_eligibility(profile, fence=self.fence)
            except StateStoreError as err:
                raise OctodotError(err.code, err.message) from err
        elif self.fence is not None:
            epoch = op.binding.profile_epoch if op.binding else 0
            if not self.fence.is_fence_valid(profile, epoch):
                raise OctodotError(
                    ErrorCode.RECOVERY_FENCE_STALE,
                    f"Recovery fence invalid for profile '{profile}'",
                )

        # 3. Grant verifier check
        if isinstance(self.verifier, DisabledGrantVerifier):
            self.store.transition_operation_state(
                operation_id,
                OperationState.BLOCKED_BEFORE_DISPATCH,
                error_code=ErrorCode.VERIFIER_UNAVAILABLE,
                fence=self.fence,
            )
            raise OctodotError(
                ErrorCode.VERIFIER_UNAVAILABLE,
                "Automated writes disabled by DisabledGrantVerifier",
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

        if raw_outcome.uncertain_effect:
            to_state = OperationState.UNKNOWN
            error_code = raw_outcome.sanitized_error_code or ErrorCode.TRANSPORT_ERROR
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

                # Check operation state in operations table: must still be DISPATCHING
                cursor.execute(
                    "SELECT state FROM operations WHERE operation_id = ?",
                    (ticket.operation_id,),
                )
                op_row = cursor.fetchone()
                if op_row is None:
                    return False
                if op_row["state"] != OperationState.DISPATCHING.value:
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
