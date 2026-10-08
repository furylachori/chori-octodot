"""Durable events, stable identities, and event extraction for octodot.

Standard library only. Compatible with Python 3.10+.
Provides:
- Stable event ID derivation from resource identity and durable transitions.
- Event generation from session inspections, state transitions, and activities.
- Invariant event identity across projection migrations.
- Extraction where unchanged polling emits zero new events.
- Reading and acknowledging durable unacknowledged events in SQLite store.
- ActionHandlers for 'events.read' and 'events.ack'.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
from typing import Any

from octodot.contracts import (
    ActionHandler,
    Clock,
    EventsAckArgs,
    EventsAckResult,
    EventsReadArgs,
    EventsReadResult,
    Store,
    canonical_hash,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_OK,
    ErrorCode,
    OctodotError,
)
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ActivityRecord,
    Binding,
    Event,
    LifecycleBucket,
    SessionRecord,
)
from octodot.reads import SessionInspection
from octodot.transport import SystemClock

# Event type constants
EVENT_TYPE_ACTIVITY_CREATED = "activity.created"
EVENT_TYPE_SESSION_TRANSITION = "session.transition"
EVENT_TYPE_SESSION_DISCOVERED = "session.discovered"
EVENT_TYPE_ATTENTION_REQUESTED = "attention.requested"
EVENT_TYPE_TERMINAL_REACHED = "terminal.reached"
EVENT_TYPE_OPERATION_OBSERVED = "operation.observed"


def _normalize_session_key(session_id: str) -> str:
    """Normalize session resource name or ID to stable key."""
    if session_id.startswith("sessions/"):
        return session_id.removeprefix("sessions/")
    return session_id


def _normalize_activity_key(activity_id: str) -> str:
    """Normalize activity resource name or ID to stable key."""
    if "/" in activity_id:
        return activity_id.split("/")[-1]
    return activity_id


def derive_activity_event_id(session_id: str, activity_id: str) -> str:
    """Derive stable event ID from activity resource identity.

    Stable across projection changes: depends only on session and activity identifiers.
    """
    sess = _normalize_session_key(session_id)
    act = _normalize_activity_key(activity_id)
    return f"act:{sess}:{act}"


def count_session_transitions(store: Store, session_id: str) -> int:
    """Count prior stored transition events for this session."""
    events = store.get_events(limit=1000)
    sess_norm = _normalize_session_key(session_id)
    count = 0
    for ev in events:
        if ev.event_type == EVENT_TYPE_SESSION_TRANSITION:
            ev_sess = _normalize_session_key(ev.session_id or "")
            if ev_sess == sess_norm:
                count += 1
    return count


def derive_transition_event_id(
    session_id: str,
    old_state: str,
    new_state: str,
    ordinal: int = 0,
    activity_id: str | None = None,
) -> str:
    """Derive stable event ID from durable session state transition.

    Stable across projection changes: depends only on session, state delta,
    and a durable per-session transition ordinal (or evidencing activity ID).
    Crucial: Unrelated update_time changes NEVER alter this ID.
    """
    sess = _normalize_session_key(session_id)
    if activity_id:
        act_key = _normalize_activity_key(activity_id)
        return f"trans:{sess}:{old_state}->{new_state}:{act_key}"
    return f"trans:{sess}:{old_state}->{new_state}:{ordinal}"


def derive_session_discovered_event_id(session_id: str) -> str:
    """Derive stable event ID for session discovery."""
    sess = _normalize_session_key(session_id)
    return f"disc:{sess}"


def derive_operation_event_id(operation_id: str, state: str) -> str:
    """Derive stable event ID for observed operation state."""
    return f"op:{operation_id}:{state}"


def create_activity_event(
    session_id: str,
    activity: ActivityRecord,
) -> Event:
    """Create an Event for a Jules activity."""
    act_id = activity.id or activity.name
    event_id = derive_activity_event_id(session_id, act_id)
    payload: dict[str, Any] = {
        "activity_name": activity.name,
        "activity_type": activity.activity_type,
    }
    if activity.originator is not None:
        payload["originator"] = activity.originator
    for k, v in activity.unknown_fields:
        payload[k] = v

    return Event.create(
        event_id=event_id,
        event_type=EVENT_TYPE_ACTIVITY_CREATED,
        resource_id=activity.name,
        payload=payload,
        created_at=activity.create_time,
        session_id=session_id,
    )


def create_transition_event(
    session_id: str,
    old_state: str,
    new_state: str,
    timestamp: str | None = None,
    ordinal: int | None = None,
    store: Store | None = None,
    activity_id: str | None = None,
) -> Event:
    """Create an Event for a session lifecycle state transition."""
    if ordinal is None:
        if store is not None:
            ordinal = count_session_transitions(store, session_id)
        else:
            ordinal = 0

    event_id = derive_transition_event_id(
        session_id=session_id,
        old_state=old_state,
        new_state=new_state,
        ordinal=ordinal,
        activity_id=activity_id,
    )
    return Event.create(
        event_id=event_id,
        event_type=EVENT_TYPE_SESSION_TRANSITION,
        resource_id=session_id,
        payload={"old_state": old_state, "new_state": new_state, "ordinal": ordinal},
        created_at=timestamp,
        session_id=session_id,
        transition_id=f"{old_state}->{new_state}:{ordinal}",
    )


def create_attention_event(
    session_id: str,
    reason: str,
    timestamp: str | None = None,
) -> Event:
    """Create an Event for session attention requested."""
    sess = _normalize_session_key(session_id)
    event_id = f"attn:{sess}:{reason}"
    return Event.create(
        event_id=event_id,
        event_type=EVENT_TYPE_ATTENTION_REQUESTED,
        resource_id=session_id,
        payload={"reason": reason},
        created_at=timestamp,
        session_id=session_id,
    )


def create_terminal_event(
    session_id: str,
    terminal_state: str,
    timestamp: str | None = None,
) -> Event:
    """Create an Event when a session reaches terminal state."""
    sess = _normalize_session_key(session_id)
    event_id = f"term:{sess}:{terminal_state}"
    return Event.create(
        event_id=event_id,
        event_type=EVENT_TYPE_TERMINAL_REACHED,
        resource_id=session_id,
        payload={"terminal_state": terminal_state},
        created_at=timestamp,
        session_id=session_id,
    )


def create_operation_event(
    operation_id: str,
    state: str,
    session_id: str | None = None,
    timestamp: str | None = None,
) -> Event:
    """Create an Event when an operation reaches an observed state."""
    event_id = derive_operation_event_id(operation_id, state)
    return Event.create(
        event_id=event_id,
        event_type=EVENT_TYPE_OPERATION_OBSERVED,
        resource_id=operation_id,
        payload={"state": state},
        created_at=timestamp,
        session_id=session_id,
    )


def extract_events_from_inspection(
    inspection: SessionInspection,
    previous_state: str | None = None,
    known_activity_ids: set[str] | None = None,
    store: Store | None = None,
    transition_ordinal: int | None = None,
) -> tuple[Event, ...]:
    """Extract new durable events from a session inspection.

    Crucial rule: If state has not transitioned and all activities are already known,
    returns empty tuple () (unchanged polling emits zero new events).
    """
    events: list[Event] = []
    sess_id = inspection.session.id or inspection.session.name

    # 1. State transition event
    curr_state = inspection.session.state
    if previous_state is not None and previous_state != curr_state:
        events.append(
            create_transition_event(
                session_id=sess_id,
                old_state=previous_state,
                new_state=curr_state,
                timestamp=inspection.session.update_time or inspection.session.create_time,
                ordinal=transition_ordinal,
                store=store,
            )
        )
        if inspection.lifecycle.bucket in (LifecycleBucket.COMPLETED, LifecycleBucket.FAILED):
            events.append(
                create_terminal_event(
                    session_id=sess_id,
                    terminal_state=curr_state,
                    timestamp=inspection.session.update_time or inspection.session.create_time,
                )
            )

    # 2. Activity events for newly observed activities
    known = known_activity_ids or set()
    for act in inspection.activities:
        act_id = act.id or act.name
        if act_id not in known:
            events.append(create_activity_event(session_id=sess_id, activity=act))

    # 3. Attention requested event
    from octodot.projections import project_attention, project_lifecycle
    lifecycle = inspection.lifecycle or project_lifecycle(inspection.session)
    attention = project_attention(
        lifecycle=lifecycle,
        candidate_bundle=inspection.candidate_bundle,
        session=inspection.session,
    )
    if attention.needs_attention and (previous_state is None or previous_state != curr_state):
        reason_str = ",".join(r.value for r in attention.reasons) or lifecycle.bucket.value
        events.append(
            create_attention_event(
                session_id=sess_id,
                reason=reason_str,
                timestamp=inspection.session.update_time or inspection.session.create_time,
            )
        )

    return tuple(events)


def detect_new_events(
    activities: Sequence[ActivityRecord],
    session: SessionRecord,
    previous_state: str | None = None,
    known_activity_ids: set[str] | None = None,
    store: Store | None = None,
    transition_ordinal: int | None = None,
) -> tuple[Event, ...]:
    """Detect new events directly from activities and session state."""
    events: list[Event] = []
    sess_id = session.id or session.name

    if previous_state is not None and previous_state != session.state:
        events.append(
            create_transition_event(
                session_id=sess_id,
                old_state=previous_state,
                new_state=session.state,
                timestamp=session.update_time or session.create_time,
                ordinal=transition_ordinal,
                store=store,
            )
        )

    known = known_activity_ids or set()
    for act in activities:
        act_id = act.id or act.name
        if act_id not in known:
            events.append(create_activity_event(session_id=sess_id, activity=act))

    return tuple(events)


def save_events(store: Store, events: Sequence[Event]) -> int:
    """Save events to durable SQLite store (idempotent ON CONFLICT DO NOTHING).

    Returns the count of events passed to save.
    """
    for ev in events:
        store.save_event(ev)
    return len(events)


def get_unacked_events(
    store: Store,
    limit: int = 100,
    since_id: str | None = None,
) -> tuple[Event, ...]:
    """Retrieve unacknowledged events from the store, optionally after since_id."""
    all_unacked = store.get_events(limit=limit, unacked_only=True)
    if since_id is None:
        return all_unacked

    # Filter events strictly after since_id if since_id is present
    found_since = False
    filtered: list[Event] = []
    for ev in all_unacked:
        if found_since:
            filtered.append(ev)
        elif ev.event_id == since_id:
            found_since = True
    return tuple(filtered) if found_since else all_unacked


def ack_events(store: Store, event_ids: Sequence[str]) -> tuple[str, ...]:
    """Acknowledge a sequence of events in the durable store.

    Returns the tuple of event IDs that were successfully acknowledged.
    """
    acked: list[str] = []
    for eid in event_ids:
        if store.ack_event(eid):
            acked.append(eid)
    return tuple(acked)


# =============================================================================
# Action Handlers for 'events.read' and 'events.ack'
# =============================================================================


class EventsReadHandler:
    """ActionHandler for 'events.read' operation."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def handle(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("action_id", "events_read"))
        args = action.get("args", {})
        limit = int(args.get("limit", 100))
        since_id = args.get("since_id")
        if since_id is not None:
            since_id = str(since_id)

        try:
            unacked = get_unacked_events(self.store, limit=limit, since_id=since_id)
            serialized_events = [
                {
                    "event_id": ev.event_id,
                    "event_type": ev.event_type,
                    "resource_id": ev.resource_id,
                    "payload": dict(ev.payload),
                    "created_at": ev.created_at,
                    "session_id": ev.session_id,
                    "transition_id": ev.transition_id,
                }
                for ev in unacked
            ]
            data = {"events": serialized_events}
            return ActionResult(
                action_id=action_id,
                op="events.read",
                status=ActionResultStatus.OK,
                exit_code=EXIT_OK,
                data=tuple(data.items()),
            )
        except Exception as e:
            code = getattr(e, "code", ErrorCode.INTERNAL_ERROR)
            return ActionResult(
                action_id=action_id,
                op="events.read",
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=code if isinstance(code, ErrorCode) else ErrorCode.INTERNAL_ERROR,
                data=(("error", str(e)),),
            )


class EventsAckHandler:
    """ActionHandler for 'events.ack' operation."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def handle(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("action_id", "events_ack"))
        args = action.get("args", {})
        event_ids_raw = args.get("event_ids", ())
        if isinstance(event_ids_raw, (list, tuple)):
            event_ids = tuple(str(x) for x in event_ids_raw)
        else:
            event_ids = (str(event_ids_raw),)

        try:
            acked = ack_events(self.store, event_ids)
            data = {
                "acked_event_ids": list(acked),
                "status": "acknowledged",
                "success": len(acked) == len(event_ids),
            }
            return ActionResult(
                action_id=action_id,
                op="events.ack",
                status=ActionResultStatus.OK,
                exit_code=EXIT_OK,
                data=tuple(data.items()),
            )
        except Exception as e:
            code = getattr(e, "code", ErrorCode.INTERNAL_ERROR)
            return ActionResult(
                action_id=action_id,
                op="events.ack",
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=code if isinstance(code, ErrorCode) else ErrorCode.INTERNAL_ERROR,
                data=(("error", str(e)),),
            )
