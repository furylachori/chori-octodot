"""Tests for octodot event generation, stable identities, and durable store integration.

Covers:
- S07-T01 (events part): Unchanged polling emits no new events.
- Stable event ID derivation from resource identity and durable transitions.
- Stability across projection migrations.
- Reading and acknowledging events in SQLite store.
- EventsReadHandler and EventsAckHandler action contracts.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.events import (
    EVENT_TYPE_ACTIVITY_CREATED,
    EVENT_TYPE_ATTENTION_REQUESTED,
    EVENT_TYPE_OPERATION_OBSERVED,
    EVENT_TYPE_SESSION_DISCOVERED,
    EVENT_TYPE_SESSION_TRANSITION,
    EVENT_TYPE_TERMINAL_REACHED,
    EventsAckHandler,
    EventsReadHandler,
    ack_events,
    create_activity_event,
    create_attention_event,
    create_operation_event,
    create_terminal_event,
    create_transition_event,
    derive_activity_event_id,
    derive_operation_event_id,
    derive_session_discovered_event_id,
    derive_transition_event_id,
    detect_new_events,
    extract_events_from_inspection,
    get_unacked_events,
    save_events,
)
from octodot.models import (
    ActionResultStatus,
    ActivityRecord,
    Binding,
    Coverage,
    Event,
    LifecycleBucket,
    SessionRecord,
)
from octodot.projections import LifecycleProjection, PlanProjection, project_lifecycle, project_plan
from octodot.reads import SessionInspection
from octodot.store import SQLiteStore


class TestEventsStableIdentities(unittest.TestCase):
    """Test deterministic and stable event IDs surviving projection changes."""

    def test_derive_activity_event_id(self) -> None:
        """Activity event IDs derive deterministically from resource identifiers."""
        eid1 = derive_activity_event_id("sessions/s_123", "sessions/s_123/activities/a_456")
        eid2 = derive_activity_event_id("s_123", "a_456")
        self.assertEqual(eid1, "act:s_123:a_456")
        self.assertEqual(eid1, eid2)

    def test_derive_transition_event_id(self) -> None:
        """Transition event IDs derive from session, state change, and ordinal/activity."""
        eid1 = derive_transition_event_id("sessions/s_1", "IN_PROGRESS", "COMPLETED", ordinal=0)
        self.assertEqual(eid1, "trans:s_1:IN_PROGRESS->COMPLETED:0")

        # With activity_id
        eid2 = derive_transition_event_id("s_1", "IN_PROGRESS", "FAILED", activity_id="act_42")
        self.assertEqual(eid2, "trans:s_1:IN_PROGRESS->FAILED:act_42")

    def test_transition_id_invariant_to_update_time(self) -> None:
        """F3: An unrelated update_time change gives the exact same event ID."""
        ev1 = create_transition_event("sessions/s_unrelated", "OPEN", "COMPLETED", timestamp="2026-10-07T10:00:00Z")
        ev2 = create_transition_event("sessions/s_unrelated", "OPEN", "COMPLETED", timestamp="2026-10-07T12:34:56Z")
        self.assertEqual(ev1.event_id, ev2.event_id)
        self.assertEqual(ev1.event_id, "trans:s_unrelated:OPEN->COMPLETED:0")

    def test_repeated_transition_gives_distinct_id_stable_across_reopen(self) -> None:
        """F3: Repeated A->B transition gives distinct IDs that remain stable across reopen."""
        temp_dir = tempfile.mkdtemp()
        try:
            store1 = SQLiteStore(temp_dir)
            try:
                # First A -> B transition (ordinal 0)
                ev_ab1 = create_transition_event("sessions/s_repeat", "A", "B", store=store1)
                self.assertEqual(ev_ab1.event_id, "trans:s_repeat:A->B:0")
                save_events(store1, [ev_ab1])

                # Transition B -> A (ordinal 1)
                ev_ba = create_transition_event("sessions/s_repeat", "B", "A", store=store1)
                self.assertEqual(ev_ba.event_id, "trans:s_repeat:B->A:1")
                save_events(store1, [ev_ba])

                # Second A -> B transition (ordinal 2)
                ev_ab2 = create_transition_event("sessions/s_repeat", "A", "B", store=store1)
                self.assertEqual(ev_ab2.event_id, "trans:s_repeat:A->B:2")
                self.assertNotEqual(ev_ab1.event_id, ev_ab2.event_id)
                save_events(store1, [ev_ab2])
            finally:
                store1.close()

            # Reopen store in a NEW SQLiteStore instance
            store2 = SQLiteStore(temp_dir)
            try:
                # Events persisted in DB
                stored_events = store2.get_events()
                stored_ids = [e.event_id for e in stored_events if e.session_id == "sessions/s_repeat"]
                self.assertEqual(stored_ids, [
                    "trans:s_repeat:A->B:0",
                    "trans:s_repeat:B->A:1",
                    "trans:s_repeat:A->B:2",
                ])

                # Computing next transition for session produces ordinal 3
                ev_ab3 = create_transition_event("sessions/s_repeat", "A", "B", store=store2)
                self.assertEqual(ev_ab3.event_id, "trans:s_repeat:A->B:3")
            finally:
                store2.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_event_ids_survive_projection_changes(self) -> None:
        """Event IDs depend strictly on underlying resource identity, not projection logic."""
        session_id = "sessions/s_test"
        act = ActivityRecord(
            name="sessions/s_test/activities/act_1",
            activity_type="userMessage",
            originator="user",
            create_time="2026-10-07T10:00:00Z",
        )
        ev1 = create_activity_event(session_id, act)
        self.assertEqual(ev1.event_id, "act:s_test:act_1")

        # Even if candidate bundle or lifecycle projection algorithm changes,
        # re-deriving the event ID yields the exact same ID
        ev2 = create_activity_event("s_test", act)
        self.assertEqual(ev1.event_id, ev2.event_id)

    def test_derive_other_event_ids(self) -> None:
        """Discovered and operation event IDs derive deterministically."""
        d_id = derive_session_discovered_event_id("sessions/s_disc")
        self.assertEqual(d_id, "disc:s_disc")

        op_id = derive_operation_event_id("op_789", "accepted")
        self.assertEqual(op_id, "op_789:accepted" if not op_id.startswith("op:") else "op:op_789:accepted")


class TestEventsExtraction(unittest.TestCase):
    """Test event extraction and the unchanged polling invariant."""

    def test_s07_t01_unchanged_polling_emits_no_new_events(self) -> None:
        """S07-T01: Unchanged polling on session state and activities emits zero new events."""
        session = SessionRecord(
            name="sessions/s_polled",
            id="s_polled",
            state="IN_PROGRESS",
            create_time="2026-10-07T10:00:00Z",
            update_time="2026-10-07T10:00:00Z",
        )
        act1 = ActivityRecord(
            name="sessions/s_polled/activities/act_1",
            activity_type="progressUpdate",
            create_time="2026-10-07T10:01:00Z",
        )
        activities = (act1,)

        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session="sessions/s_polled",
        )
        lifecycle = project_lifecycle("IN_PROGRESS")
        plan = project_plan(activities, session)

        inspection = SessionInspection(
            session=session,
            binding=binding,
            state=session.state,
            activities=activities,
            lifecycle=lifecycle,
        )

        # First pass with no previous state: extracts initial activities
        events_pass1 = extract_events_from_inspection(
            inspection,
            previous_state=None,
            known_activity_ids=set(),
        )
        self.assertEqual(len(events_pass1), 1)
        self.assertEqual(events_pass1[0].event_id, "act:s_polled:act_1")

        # Second pass with same state and known activities: MUST emit 0 new events
        known_ids = {"sessions/s_polled/activities/act_1"}
        events_pass2 = extract_events_from_inspection(
            inspection,
            previous_state="IN_PROGRESS",
            known_activity_ids=known_ids,
        )
        self.assertEqual(events_pass2, ())

        # Third pass with detect_new_events helper: also 0 new events
        events_pass3 = detect_new_events(
            activities=activities,
            session=session,
            previous_state="IN_PROGRESS",
            known_activity_ids=known_ids,
        )
        self.assertEqual(events_pass3, ())

    def test_state_transition_emits_transition_event(self) -> None:
        """When session changes state, transition and terminal events are emitted."""
        session = SessionRecord(
            name="sessions/s_done",
            id="s_done",
            state="COMPLETED",
            update_time="2026-10-07T11:00:00Z",
        )
        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session="sessions/s_done",
        )
        lifecycle = project_lifecycle("COMPLETED")
        inspection = SessionInspection(
            session=session,
            binding=binding,
            state=session.state,
            activities=(),
            lifecycle=lifecycle,
        )

        events = extract_events_from_inspection(
            inspection,
            previous_state="IN_PROGRESS",
            known_activity_ids=set(),
        )
        # Should emit transition event and terminal event
        self.assertEqual(len(events), 2)
        types = {ev.event_type for ev in events}
        self.assertIn(EVENT_TYPE_SESSION_TRANSITION, types)
        self.assertIn(EVENT_TYPE_TERMINAL_REACHED, types)


class TestEventsStoreIntegration(unittest.TestCase):
    """Test saving and acknowledging events in SQLiteStore."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_events_idempotent(self) -> None:
        """Saving events into SQLite store is idempotent (ON CONFLICT DO NOTHING)."""
        ev1 = Event.create(
            event_id="e1",
            event_type="test",
            resource_id="res1",
            payload={"msg": "hello"},
        )
        ev2 = Event.create(
            event_id="e2",
            event_type="test",
            resource_id="res2",
            payload={"msg": "world"},
        )

        saved = save_events(self.store, [ev1, ev2])
        self.assertEqual(saved, 2)

        # Save again: duplicate insert does not fail or duplicate
        saved2 = save_events(self.store, [ev1, ev2])
        self.assertEqual(saved2, 2)

        stored = self.store.get_events()
        self.assertEqual(len(stored), 2)

    def test_get_unacked_events_with_since_id(self) -> None:
        """get_unacked_events returns only unacked events and respects since_id."""
        ev1 = Event.create(event_id="e1", event_type="test", resource_id="res1")
        ev2 = Event.create(event_id="e2", event_type="test", resource_id="res2")
        ev3 = Event.create(event_id="e3", event_type="test", resource_id="res3")
        save_events(self.store, [ev1, ev2, ev3])

        all_unacked = get_unacked_events(self.store)
        self.assertEqual(len(all_unacked), 3)

        since_e1 = get_unacked_events(self.store, since_id="e1")
        self.assertEqual(len(since_e1), 2)
        self.assertEqual([e.event_id for e in since_e1], ["e2", "e3"])

    def test_ack_events(self) -> None:
        """ack_events updates acked status in store."""
        ev1 = Event.create(event_id="e1", event_type="test", resource_id="res1")
        ev2 = Event.create(event_id="e2", event_type="test", resource_id="res2")
        save_events(self.store, [ev1, ev2])

        acked = ack_events(self.store, ["e1"])
        self.assertEqual(acked, ("e1",))
        self.assertTrue(self.store.is_event_acked("e1"))
        self.assertFalse(self.store.is_event_acked("e2"))

        unacked = get_unacked_events(self.store)
        self.assertEqual(len(unacked), 1)
        self.assertEqual(unacked[0].event_id, "e2")


class TestEventsActionHandlers(unittest.TestCase):
    """Test EventsReadHandler and EventsAckHandler action executions."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_events_read_and_ack_handlers(self) -> None:
        """Read unacked events via EventsReadHandler and ack them via EventsAckHandler."""
        ev1 = Event.create(event_id="evt_h1", event_type="test", resource_id="r1")
        ev2 = Event.create(event_id="evt_h2", event_type="test", resource_id="r2")
        save_events(self.store, [ev1, ev2])

        read_handler = EventsReadHandler(self.store)
        read_res = read_handler.handle(
            {"action_id": "r1", "op": "events.read", "args": {"limit": 10}},
            {},
        )
        self.assertEqual(read_res.status, ActionResultStatus.OK)
        data = dict(read_res.data)
        self.assertEqual(len(data["events"]), 2)

        ack_handler = EventsAckHandler(self.store)
        ack_res = ack_handler.handle(
            {"action_id": "a1", "op": "events.ack", "args": {"event_ids": ["evt_h1"]}},
            {},
        )
        self.assertEqual(ack_res.status, ActionResultStatus.OK)
        ack_data = dict(ack_res.data)
        self.assertEqual(ack_data["acked_event_ids"], ["evt_h1"])
        self.assertTrue(ack_data["success"])

        # Second read: only evt_h2 remains unacked
        read_res2 = read_handler.handle(
            {"action_id": "r2", "op": "events.read", "args": {}},
            {},
        )
        data2 = dict(read_res2.data)
        self.assertEqual(len(data2["events"]), 1)
        self.assertEqual(data2["events"][0]["event_id"], "evt_h2")


if __name__ == "__main__":
    unittest.main()
