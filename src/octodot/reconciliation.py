"""Read-only uncertain-effect reconciliation and desired-state resolution.

Standard library only. Compatible with Python 3.10+.
Provides read-only effect verification via JulesReadAPI, honest attribution
under ambiguity (manual messages, duplicate text, multiple creation markers),
and blocker retirement without resend authorization.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping, Sequence

from octodot.contracts import (
    Clock,
    JulesReadAPI,
    OperationsReconcileArgs,
    OperationsReconcileResult,
    RecoveryFence,
)
from octodot.errors import ErrorCode, OctodotError
from octodot.models import (
    ActivityRecord,
    OperationRecord,
    OperationState,
    SessionRecord,
)
from octodot.transport import SystemClock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Reconciler:
    """Read-only reconciliation service for operations in UNKNOWN or ACCEPTED state.

    Guarantees:
    - Never issues mutations (zero POST calls).
    - Matches new activities/sessions as effect evidence with honest attribution:
      - Duplicate text => attribution uncertain.
      - Exact manual matching message => attribution uncertain.
      - Multiple creation-marker matches => attribution uncertain.
    - Evidence separates api_accepted, effect_observed, attribution, and ui_verified.
    - Absence after 0, 1, or 3 complete scans remains UNKNOWN; absence never authorizes retry.
    - Desired-state resolution can retire a blocker but never authorizes resend.
    """

    def __init__(
        self,
        store: Any,
        read_api: JulesReadAPI | Any | None = None,
        clock: Clock | None = None,
        fence: RecoveryFence | None = None,
    ) -> None:
        self.store = store
        self.read_api = read_api
        self.clock = clock or SystemClock()
        self.fence = fence

    def reconcile(
        self,
        operation_id: str,
        *,
        read_api: JulesReadAPI | Any | None = None,
        scans: int = 1,
    ) -> OperationsReconcileResult:
        """Perform read-only reconciliation for a single operation."""
        rec = self.store.get_operation(operation_id)
        if rec is None:
            raise OctodotError(
                ErrorCode.NOT_FOUND, f"Operation '{operation_id}' not found"
            )

        # Terminal operations don't need reconciliation
        if rec.state in (
            OperationState.EFFECT_OBSERVED,
            OperationState.REJECTED,
            OperationState.BLOCKED_BEFORE_DISPATCH,
            OperationState.CANCELLED_BEFORE_DISPATCH,
        ):
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=rec,
            )

        api = read_api or self.read_api
        if api is None or scans <= 0:
            # 0 scans or no read API: operation remains in current state
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=rec,
            )

        # Determine operation type
        is_session_scoped = bool(rec.binding and rec.binding.session)
        if is_session_scoped:
            return self._reconcile_session_operation(rec, api, scans)
        else:
            return self._reconcile_creation_operation(rec, api, scans)

    # =========================================================================
    # Internal reconciliation handlers
    # =========================================================================

    def _get_expected_text(self, rec: OperationRecord) -> str | None:
        for k, v in rec.evidence:
            if k == "prompt" and isinstance(v, str):
                return v
            if k == "payload" and isinstance(v, dict):
                p = v.get("prompt") or v.get("text")
                if isinstance(p, str):
                    return p
        return None

    def _get_expected_marker(self, rec: OperationRecord) -> str | None:
        for k, v in rec.evidence:
            if k in ("logical_task_marker", "marker") and isinstance(v, str):
                return v
            if k == "payload" and isinstance(v, dict):
                m = (
                    v.get("logical_task_marker")
                    or v.get("task_marker")
                    or v.get("marker")
                    or v.get("logical_task")
                )
                if isinstance(m, str):
                    return m
        return None

    def _extract_activity_text(self, act: Any) -> str | None:
        if isinstance(act, ActivityRecord):
            for k, v in act.unknown_fields:
                if k in ("prompt", "text", "message") and isinstance(v, str):
                    return v
            if hasattr(act, "data") and isinstance(getattr(act, "data"), dict):
                d = getattr(act, "data")
                return d.get("prompt") or d.get("text") or d.get("message")
        if isinstance(act, dict):
            for k in ("prompt", "text", "message"):
                if k in act:
                    return str(act[k])
            data = act.get("data")
            if isinstance(data, dict):
                return data.get("prompt") or data.get("text") or data.get("message")
        if hasattr(act, "prompt"):
            return getattr(act, "prompt")
        return None

    def _is_manual_activity(self, act: Any) -> bool:
        originator = getattr(act, "originator", None)
        if originator is None and isinstance(act, dict):
            originator = act.get("originator")
        if isinstance(originator, str) and originator.upper() in (
            "USER",
            "HUMAN",
            "MANUAL",
        ):
            return True
        if getattr(act, "manual", False) or (
            isinstance(act, dict) and act.get("manual")
        ):
            return True
        return False

    def _reconcile_session_operation(
        self,
        rec: OperationRecord,
        api: Any,
        scans: int,
    ) -> OperationsReconcileResult:
        assert rec.binding is not None
        session_name = rec.binding.session or ""
        expected_text = self._get_expected_text(rec)

        all_matching_activities: list[Any] = []

        for scan_idx in range(1, scans + 1):
            try:
                resp = api.activities_list(session_name)
            except Exception as exc:
                self.store.append_operation_evidence(
                    rec.operation_id,
                    "reconciliation_error",
                    {"scan": scan_idx, "error": str(exc)},
                )
                continue

            activities: Sequence[Any] = ()
            if isinstance(resp, (list, tuple)):
                activities = resp
            elif hasattr(resp, "activities"):
                activities = getattr(resp, "activities") or ()
            elif isinstance(resp, dict) and "activities" in resp:
                activities = resp["activities"] or ()

            for act in activities:
                act_text = self._extract_activity_text(act)
                if expected_text and act_text and act_text == expected_text:
                    if act not in all_matching_activities:
                        all_matching_activities.append(act)

        # Record scan completion evidence
        self.store.append_operation_evidence(
            rec.operation_id,
            "reconciliation_scan",
            {
                "scans_completed": scans,
                "matches_found": len(all_matching_activities),
                "timestamp": _utc_now_iso(),
            },
        )

        # Evaluate matches
        if len(all_matching_activities) == 0:
            # Absence after 0, 1, or 3 complete scans remains UNKNOWN; never authorizes retry
            refreshed = self.store.get_operation(rec.operation_id)
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=refreshed,
            )

        if len(all_matching_activities) > 1:
            # Duplicate text: honest attribution uncertainty
            self.store.update_operation_evidence_flags(
                rec.operation_id,
                effect_observed=True,
                attribution="uncertain:duplicate_text",
                fence=self.fence,
            )
            self.store.append_operation_evidence(
                rec.operation_id,
                "attribution_uncertainty",
                {
                    "reason": "duplicate_text",
                    "matching_count": len(all_matching_activities),
                },
            )
            refreshed = self.store.get_operation(rec.operation_id)
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=refreshed,
            )

        # Exactly 1 matching activity
        act = all_matching_activities[0]
        if self._is_manual_activity(act):
            # Exact manual matching message: honest attribution uncertainty
            self.store.update_operation_evidence_flags(
                rec.operation_id,
                effect_observed=True,
                attribution="uncertain:manual_match",
                fence=self.fence,
            )
            self.store.append_operation_evidence(
                rec.operation_id,
                "attribution_uncertainty",
                {
                    "reason": "manual_match",
                    "activity": str(getattr(act, "name", act)),
                },
            )
            refreshed = self.store.get_operation(rec.operation_id)
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=refreshed,
            )

        # Unique automated match: inferred attribution
        updated = self.store.transition_operation_state(
            rec.operation_id,
            OperationState.EFFECT_OBSERVED,
            effect_observed=True,
            attribution="inferred:unique_text_match",
            fence=self.fence,
        )
        return OperationsReconcileResult(
            reconciled_state=OperationState.EFFECT_OBSERVED.value,
            record=updated,
        )

    def _reconcile_creation_operation(
        self,
        rec: OperationRecord,
        api: Any,
        scans: int,
    ) -> OperationsReconcileResult:
        marker = self._get_expected_marker(rec)
        all_matching_sessions: list[Any] = []

        for scan_idx in range(1, scans + 1):
            try:
                resp = api.sessions_list()
            except Exception as exc:
                self.store.append_operation_evidence(
                    rec.operation_id,
                    "reconciliation_error",
                    {"scan": scan_idx, "error": str(exc)},
                )
                continue

            sessions: Sequence[Any] = ()
            if isinstance(resp, (list, tuple)):
                sessions = resp
            elif hasattr(resp, "sessions"):
                sessions = getattr(resp, "sessions") or ()
            elif isinstance(resp, dict) and "sessions" in resp:
                sessions = resp["sessions"] or ()

            for sess in sessions:
                if self._session_matches_marker(sess, marker):
                    if sess not in all_matching_sessions:
                        all_matching_sessions.append(sess)

        self.store.append_operation_evidence(
            rec.operation_id,
            "reconciliation_scan",
            {
                "scans_completed": scans,
                "matches_found": len(all_matching_sessions),
                "timestamp": _utc_now_iso(),
            },
        )

        if len(all_matching_sessions) == 0:
            # Absence stays UNKNOWN
            refreshed = self.store.get_operation(rec.operation_id)
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=refreshed,
            )

        if len(all_matching_sessions) > 1:
            # Multiple creation-marker matches: honest attribution uncertainty
            self.store.update_operation_evidence_flags(
                rec.operation_id,
                effect_observed=True,
                attribution="uncertain:multiple_matches",
                fence=self.fence,
            )
            self.store.append_operation_evidence(
                rec.operation_id,
                "attribution_uncertainty",
                {
                    "reason": "multiple_creation_marker_matches",
                    "matching_count": len(all_matching_sessions),
                },
            )
            refreshed = self.store.get_operation(rec.operation_id)
            return OperationsReconcileResult(
                reconciled_state=rec.state.value,
                record=refreshed,
            )

        # Exactly 1 matching session
        updated = self.store.transition_operation_state(
            rec.operation_id,
            OperationState.EFFECT_OBSERVED,
            effect_observed=True,
            attribution="inferred:unique_marker_match",
            fence=self.fence,
        )
        return OperationsReconcileResult(
            reconciled_state=OperationState.EFFECT_OBSERVED.value,
            record=updated,
        )

    def _session_matches_marker(self, sess: Any, marker: str | None) -> bool:
        if not marker:
            return False
        clean_marker = marker.strip()
        title = getattr(sess, "title", None) or (
            sess.get("title") if isinstance(sess, dict) else ""
        )
        prompt = getattr(sess, "prompt", None) or (
            sess.get("prompt") if isinstance(sess, dict) else ""
        )
        name = getattr(sess, "name", None) or (
            sess.get("name") if isinstance(sess, dict) else ""
        )
        if clean_marker in str(title or ""):
            return True
        if clean_marker in str(prompt or ""):
            return True
        if clean_marker == str(name or ""):
            return True
        if isinstance(sess, SessionRecord):
            for k, v in sess.unknown_fields:
                if clean_marker in str(v):
                    return True
        return False

    # =========================================================================
    # Desired-State Resolution
    # =========================================================================

    def resolve_desired_state(
        self,
        operation_id: str,
        decided_by: str,
        reason: str = "",
        resolution: str = "desired_state_resolved",
    ) -> OperationRecord:
        """Retire a blocker via desired-state resolution.

        Guarantees:
        - The operation state stays UNKNOWN (remote truth stays unknown) and its evidence flags are untouched.
        - Appends an immutable resolution evidence record via store evidence API:
          {"resolution": "desired_state_resolved", "decided_by": ..., "reason": ..., "at": ...}.
        - Requires a non-empty, non-placeholder decision reference.
        - NEVER authorizes resend or issues new dispatch tickets.
        """
        rec = self.store.get_operation(operation_id)
        if rec is None:
            raise OctodotError(
                ErrorCode.NOT_FOUND, f"Operation '{operation_id}' not found"
            )

        if not decided_by or not isinstance(decided_by, str) or not decided_by.strip():
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                "decided_by must be a non-empty decision reference",
            )
        clean_decided = decided_by.strip()
        lower_decided = clean_decided.lower()
        if (
            lower_decided
            in (
                "placeholder",
                "todo",
                "none",
                "null",
                "unknown",
                "n/a",
                "na",
                "undefined",
                "tbd",
            )
            or lower_decided.startswith(("placeholder", "todo"))
        ):
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"decided_by cannot be empty or a placeholder: '{decided_by}'",
            )

        now = _utc_now_iso()
        self.store.append_operation_evidence(
            operation_id,
            "desired_state_resolution",
            {
                "resolution": resolution,
                "decided_by": clean_decided,
                "reason": reason.strip() if reason else "",
                "at": now,
            },
        )

        if rec.state == OperationState.PREPARED:
            return self.store.transition_operation_state(
                operation_id,
                OperationState.CANCELLED_BEFORE_DISPATCH,
                fence=self.fence,
            )

        # For UNKNOWN: state stays UNKNOWN, evidence flags are untouched
        refreshed = self.store.get_operation(operation_id)
        return refreshed or rec


def reconcile_operation(
    store: Any,
    operation_id: str,
    read_api: JulesReadAPI | Any | None = None,
    scans: int = 1,
    fence: RecoveryFence | None = None,
) -> OperationsReconcileResult:
    """Convenience function to reconcile an operation."""
    reconciler = Reconciler(store=store, read_api=read_api, fence=fence)
    return reconciler.reconcile(operation_id, scans=scans)


def resolve_desired_state(
    store: Any,
    operation_id: str,
    decided_by: str,
    reason: str = "",
    fence: RecoveryFence | None = None,
) -> OperationRecord:
    """Convenience function to resolve desired state."""
    reconciler = Reconciler(store=store, fence=fence)
    return reconciler.resolve_desired_state(
        operation_id, decided_by=decided_by, reason=reason
    )
