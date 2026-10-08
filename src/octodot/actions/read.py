"""Action handlers for read and diagnostic operations in octodot plans.

Standard library only. Compatible with Python 3.10+.
Provides ActionHandlers for:
- inventory.collect
- session.inspect
- chats.collect
- capabilities.inspect
- healthcheck
- suggestions.collect (API mode => status unsupported, code unsupported_public_api, coverage.complete=false)

Produces bounded ActionResults respecting max_output_bytes caps and operation selection filters.
Zero POST calls on all read paths.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from octodot.contracts import (
    ActionHandler,
    CapabilityClassification,
    LIVE_INVOCATION_DEFAULTS,
    OPERATION_INVENTORY,
    canonical_bytes,
)
from octodot.errors import (
    EXIT_OK,
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
    OctodotError,
)
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    Binding,
    CandidateBundle,
    Capability,
    Coverage,
    LifecycleBucket,
    SessionRecord,
    SourceRecord,
)
from octodot.reads import ReadService, ResolvedScope, resolve_effective_scope


def _serialize_for_result(obj: Any) -> Any:
    """Recursively convert records and domain objects into JSON-safe dictionaries/tuples."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, SourceRecord):
        from octodot.reads import source_to_dict
        return source_to_dict(obj)
    if isinstance(obj, SessionRecord):
        from octodot.reads import session_to_dict
        return session_to_dict(obj)
    if isinstance(obj, Binding):
        return {
            "profile": obj.profile,
            "profile_epoch": obj.profile_epoch,
            "source": obj.source,
            "repository": obj.repository,
            "starting_branch": obj.starting_branch,
            "session": obj.session,
        }
    if isinstance(obj, Coverage):
        return {
            "complete": obj.complete,
            "snapshot_atomic": obj.snapshot_atomic,
            "pages": obj.pages,
            "items": obj.items,
            "skipped_scope": list(obj.skipped_scope),
            "reasons": list(obj.reasons),
            "resume_ref": obj.resume_ref,
        }
    if isinstance(obj, CandidateBundle):
        return {
            "messages": list(obj.messages),
            "activities": list(obj.activities),
            "has_ambiguity": obj.has_ambiguity,
            "ambiguity_reasons": list(obj.ambiguity_reasons),
            "selected_activity_id": obj.selected_activity_id,
            "last_message_text": obj.last_message_text,
        }
    if isinstance(obj, Capability):
        return {
            "name": obj.name,
            "documented": obj.documented,
            "enabled": obj.enabled,
            "live_tested": obj.live_tested,
            "source": obj.source,
            "evidence_time": obj.evidence_time,
        }
    if isinstance(obj, Mapping):
        return {str(k): _serialize_for_result(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_result(item) for item in obj]
    return str(obj)


def _check_and_bound_data(
    data: dict[str, Any],
    max_output_bytes: int,
) -> tuple[dict[str, Any], Coverage | None]:
    """Check serialized data size against max_output_bytes cap.

    Returns bounded data dict and updated partial coverage if cap exceeded.
    """
    serialized = canonical_bytes(data)
    if len(serialized) <= max_output_bytes:
        return data, None

    # Cap exceeded: bound data and return partial coverage
    bounded_data: dict[str, Any] = {
        "truncated": True,
        "byte_count": len(serialized),
        "max_output_bytes": max_output_bytes,
    }
    for k, v in data.items():
        if isinstance(v, (list, tuple)):
            bounded_data[k] = list(v)[:3]  # summarize to first 3 items
            bounded_data[f"{k}_total_count"] = len(v)
        else:
            bounded_data[k] = v
        # Re-check size
        if len(canonical_bytes(bounded_data)) > max_output_bytes:
            bounded_data.pop(k, None)
            break

    cov = Coverage(
        complete=False,
        snapshot_atomic=False,
        reasons=("output_cap_reached",),
        skipped_scope=("output_payload",),
    )
    return bounded_data, cov


# =====================================================================
# Specific Action Handlers
# =====================================================================


class InventoryCollectHandler:
    """ActionHandler for 'inventory.collect'."""

    def can_handle(self, op: str) -> bool:
        return op == "inventory.collect"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id", "act-inventory-collect"))
        op = "inventory.collect"
        params = action.get("params") or {}
        limits = context.get("limits") or {}
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

        envelope_scope = context.get("scope")
        if envelope_scope is None and isinstance(context.get("plan"), Mapping):
            envelope_scope = context["plan"].get("scope")

        try:
            effective_scope = resolve_effective_scope(envelope_scope, action=action, params=params)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": str(err)},
            )

        try:
            collection, coverage = read_service.collect(scope=effective_scope, limits=limits)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": str(err)},
            )

        raw_sources = [_serialize_for_result(s) for s in collection.sources]
        raw_sessions = [_serialize_for_result(s) for s in collection.sessions]

        data: dict[str, Any] = {}
        selection = params.get("selection")
        if selection and isinstance(selection, (list, tuple)):
            sel_set = set(selection)
            if "sources" in sel_set:
                data["sources"] = raw_sources
            if "sessions" in sel_set:
                data["sessions"] = raw_sessions
            if "source_names" in sel_set:
                data["source_names"] = [s.name for s in collection.sources]
            if "session_names" in sel_set:
                data["session_names"] = [s.name for s in collection.sessions]
            if "active_session" in sel_set:
                # Find newest open session
                active = None
                for s in collection.sessions:
                    if s.state.upper() in ("ACTIVE", "RUNNING", "IN_PROGRESS", "AWAITING_USER_FEEDBACK"):
                        active = s.name
                        break
                data["active_session"] = active
        else:
            data["sources"] = raw_sources
            data["sessions"] = raw_sessions

        max_output = limits.get("max_output_bytes", LIVE_INVOCATION_DEFAULTS["max_output_bytes"])
        bounded_data, cap_cov = _check_and_bound_data(data, max_output)

        final_coverage = cap_cov or coverage
        if final_coverage.complete:
            status = ActionResultStatus.OK
            exit_code = EXIT_OK
            err_code = None
        else:
            status = ActionResultStatus.PARTIAL
            exit_code = EXIT_PARTIAL_OR_UNSUPPORTED
            err_code = ErrorCode.PARTIAL_COVERAGE

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=status,
            exit_code=exit_code,
            error_code=err_code,
            coverage=final_coverage,
            data=bounded_data,
        )


class SessionInspectHandler:
    """ActionHandler for 'session.inspect'."""

    def can_handle(self, op: str) -> bool:
        return op == "session.inspect"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id", "act-session-inspect"))
        op = "session.inspect"
        params = action.get("params") or {}
        limits = context.get("limits") or {}
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

        session_target = params.get("session") or action.get("target")
        if not session_target:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "session.inspect requires a session target"},
            )

        envelope_scope = context.get("scope")
        if envelope_scope is None and isinstance(context.get("plan"), Mapping):
            envelope_scope = context["plan"].get("scope")

        try:
            effective_scope = resolve_effective_scope(
                envelope_scope, action=action, params=params, target_session=session_target
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": str(err)},
            )

        binding = Binding(
            profile=context.get("profile", "default"),
            profile_epoch=context.get("profile_epoch", 0),
            source=params.get("source", ""),
            repository=effective_scope.repository or "",
            starting_branch=effective_scope.branch,
            session=session_target,
        )

        try:
            insp = read_service.inspect(binding=binding, fresh=True, scope=effective_scope)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": str(err)},
            )

        full_data: dict[str, Any] = {
            "session": _serialize_for_result(insp.session),
            "binding": _serialize_for_result(insp.binding),
            "state": insp.state,
            "title": insp.title,
            "latest_plan": insp.latest_plan,
            "latest_plan_id": insp.latest_plan_id,
            "latest_plan_hash": insp.latest_plan_hash,
            "feedback_bundle": _serialize_for_result(insp.feedback_bundle),
        }

        selection = params.get("selection")
        if selection and isinstance(selection, (list, tuple)):
            sel_set = set(selection)
            data = {k: v for k, v in full_data.items() if k in sel_set}
        else:
            data = full_data

        max_output = limits.get("max_output_bytes", LIVE_INVOCATION_DEFAULTS["max_output_bytes"])
        bounded_data, cap_cov = _check_and_bound_data(data, max_output)

        coverage = cap_cov or insp.coverage or Coverage(complete=True)
        status = ActionResultStatus.OK if coverage.complete else ActionResultStatus.PARTIAL
        exit_code = EXIT_OK if coverage.complete else EXIT_PARTIAL_OR_UNSUPPORTED
        err_code = None if coverage.complete else ErrorCode.PARTIAL_COVERAGE

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=status,
            exit_code=exit_code,
            error_code=err_code,
            coverage=coverage,
            data=bounded_data,
        )


class ChatsCollectHandler:
    """ActionHandler for 'chats.collect'."""

    def can_handle(self, op: str) -> bool:
        return op == "chats.collect"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id", "act-chats-collect"))
        op = "chats.collect"
        params = action.get("params") or {}
        limits = context.get("limits") or {}
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

        session_target = params.get("session") or action.get("target")
        if not session_target:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "chats.collect requires a session target"},
            )

        envelope_scope = context.get("scope")
        if envelope_scope is None and isinstance(context.get("plan"), Mapping):
            envelope_scope = context["plan"].get("scope")

        try:
            effective_scope = resolve_effective_scope(
                envelope_scope, action=action, params=params, target_session=session_target
            )
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": str(err)},
            )

        chat_params = {
            "session": session_target,
            "repository": effective_scope.repository,
            "branch": effective_scope.branch,
            **params,
        }
        try:
            coll = read_service.chats(chat_params, fresh=True, scope=effective_scope)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": str(err)},
            )

        full_data: dict[str, Any] = {
            "activities": [_serialize_for_result(a) for a in coll.activities],
            "messages": list(coll.messages),
            "candidate_bundle": _serialize_for_result(coll.candidate_bundle),
            "feedback_bundle": _serialize_for_result(coll.feedback_bundle),
            "latest_activity_id": coll.latest_activity_id,
            "last_message": coll.last_message_text,
        }

        selection = params.get("selection")
        if selection and isinstance(selection, (list, tuple)):
            sel_set = set(selection)
            data = {k: v for k, v in full_data.items() if k in sel_set}
        else:
            data = {
                "activities": full_data["activities"],
                "messages": full_data["messages"],
                "candidate_bundle": full_data["candidate_bundle"],
            }

        max_output = limits.get("max_output_bytes", LIVE_INVOCATION_DEFAULTS["max_output_bytes"])
        bounded_data, cap_cov = _check_and_bound_data(data, max_output)

        coverage = cap_cov or coll.coverage
        status = ActionResultStatus.OK if coverage.complete else ActionResultStatus.PARTIAL
        exit_code = EXIT_OK if coverage.complete else EXIT_PARTIAL_OR_UNSUPPORTED
        err_code = None if coverage.complete else ErrorCode.PARTIAL_COVERAGE

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=status,
            exit_code=exit_code,
            error_code=err_code,
            coverage=coverage,
            data=bounded_data,
        )


class CapabilitiesInspectHandler:
    """ActionHandler for 'capabilities.inspect'."""

    def can_handle(self, op: str) -> bool:
        return op == "capabilities.inspect"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id", "act-capabilities-inspect"))
        op = "capabilities.inspect"

        caps: list[dict[str, Any]] = []
        for op_name, spec in OPERATION_INVENTORY.items():
            is_unsupported = spec.capability == CapabilityClassification.UNSUPPORTED_PUBLIC_API
            is_mutation = spec.classification.value == "mutation"
            enabled = not is_unsupported and not is_mutation
            caps.append({
                "name": op_name,
                "documented": True,
                "enabled": enabled,
                "live_tested": False,
                "source": "octodot.core" if not is_unsupported else "jules.public_api",
            })

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=ActionResultStatus.OK,
            exit_code=EXIT_OK,
            coverage=Coverage(complete=True),
            data={"capabilities": caps},
        )


class HealthcheckHandler:
    """ActionHandler for 'healthcheck'."""

    def can_handle(self, op: str) -> bool:
        return op == "healthcheck"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id", "act-healthcheck"))
        op = "healthcheck"
        store = context.get("store")
        read_service = context.get("read_service")

        details: dict[str, Any] = {
            "store_available": store is not None,
            "read_service_available": read_service is not None,
            "profile": context.get("profile", "default"),
        }

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=ActionResultStatus.OK,
            exit_code=EXIT_OK,
            coverage=Coverage(complete=True),
            data={"healthy": True, "status": "ok", "details": details},
        )


class SuggestionsCollectHandler:
    """ActionHandler for 'suggestions.collect' (unsupported in public API mode)."""

    def can_handle(self, op: str) -> bool:
        return op == "suggestions.collect"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id", "act-suggestions-collect"))
        op = "suggestions.collect"

        # Public API mode returns unsupported without any remote mutation/call
        coverage = Coverage(
            complete=False,
            snapshot_atomic=False,
            reasons=("unsupported_public_api",),
            skipped_scope=("suggestions",),
        )

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=ActionResultStatus.UNSUPPORTED,
            exit_code=EXIT_PARTIAL_OR_UNSUPPORTED,
            error_code=ErrorCode.UNSUPPORTED_PUBLIC_API,
            coverage=coverage,
            data={"suggestions": (), "unsupported": True},
        )


# =====================================================================
# Aggregate Read Action Handler Router
# =====================================================================


class ReadActionHandler:
    """Unified ActionHandler handling all read and diagnostic operations."""

    def __init__(self) -> None:
        self._handlers: tuple[ActionHandler, ...] = (
            InventoryCollectHandler(),
            SessionInspectHandler(),
            ChatsCollectHandler(),
            CapabilitiesInspectHandler(),
            HealthcheckHandler(),
            SuggestionsCollectHandler(),
        )

    def can_handle(self, op: str) -> bool:
        return any(h.can_handle(op) for h in self._handlers)

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        op = str(action.get("op", ""))
        for h in self._handlers:
            if h.can_handle(op):
                return h.execute(action, context)

        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"ReadActionHandler cannot handle operation '{op}'",
        )
