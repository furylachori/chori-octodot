"""Tests for S07-T04: Durable event receiver, staged receipts, and crash recovery.

Covers:
- S07-T04: Crash after receiver acceptance/before ACK produces deduplicated redelivery;
  crash around channel send preserves ambiguous delivery without blind resend.
- Distinct receipt stages: receiver_accepted, channel_send_accepted, delivery_unknown.
- Channel idempotent lookup resolution without blind resend.
- Refusal to ACK without verified durable receiver acceptance.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.errors import ErrorCode, OctodotError, TransportFailureError
from octodot.models import Event, Receipt
from octodot.receiver import Channel, DurableReceiver
from octodot.store import SQLiteStore
from octodot.transport import FakeClock


class FakeTestCrash(Exception):
    """Simulated crash exception for fault hooks."""
    pass


class ScriptedChannel:
    """Scripted outbound channel tracking sends and supporting idempotent lookup."""

    def __init__(
        self,
        name: str = "test_channel",
        can_lookup: bool = False,
        lookup_result: bool | None = None,
        fail_on_send: Exception | None = None,
    ) -> None:
        self._name = name
        self._can_lookup = can_lookup
        self._lookup_result = lookup_result
        self.fail_on_send = fail_on_send
        self.sent_events: list[Event] = []
        self.lookup_calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: Event, receipt: Receipt) -> None:
        if self.fail_on_send is not None:
            raise self.fail_on_send
        self.sent_events.append(event)

    def has_idempotent_lookup(self) -> bool:
        return self._can_lookup

    def lookup_send_status(self, event_id: str, receipt_id: str) -> bool | None:
        self.lookup_calls.append((event_id, receipt_id))
        return self._lookup_result


class TestReceiverS07T04(unittest.TestCase):
    """S07-T04 test suite for durable receiver, crash recovery, and ambiguous sends."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s07_t04_crash_after_acceptance_before_ack_deduplication(self) -> None:
        """S07-T04: Crash after receiver acceptance/before ACK produces deduplicated redelivery."""
        channel = ScriptedChannel(name="webhook")

        def crash_hook(point: str) -> None:
            if point == "after_receiver_acceptance":
                raise FakeTestCrash("Process crashed immediately after receiver acceptance!")

        receiver = DurableReceiver(
            store=self.store,
            channel=channel,
            clock=self.clock,
            fault_hook=crash_hook,
        )

        event = Event.create(
            event_id="evt_crash_01",
            event_type="userMessage",
            resource_id="sessions/s1/activities/a1",
            payload={"text": "Hello world"},
        )
        # Seed event into store as unacknowledged
        self.store.save_event(event)

        # 1. First delivery attempt: crashes after receiver acceptance
        with self.assertRaises(FakeTestCrash):
            receiver.receive_event(event)

        # Verify durable state in store: acceptance was written BEFORE the crash
        saved_receipt = self.store.get_receipt("rcpt_evt_crash_01")
        self.assertIsNotNone(saved_receipt)
        assert saved_receipt is not None
        self.assertTrue(saved_receipt.receiver_accepted)
        self.assertFalse(saved_receipt.channel_send_accepted)
        self.assertFalse(saved_receipt.delivery_unknown)
        # Event was not yet sent to channel before crash
        self.assertEqual(len(channel.sent_events), 0)

        # 2. Crash recovery / restart: new receiver on NEW store
        self.store.close()
        self.store = SQLiteStore(self.test_dir)
        recovery_channel = ScriptedChannel(name="webhook")
        recovery_receiver = DurableReceiver(
            store=self.store,
            channel=recovery_channel,
            clock=self.clock,
        )

        # Redelivery of the exact same event
        receipt2 = recovery_receiver.receive_event(event)
        self.assertTrue(receipt2.receiver_accepted)
        self.assertTrue(receipt2.channel_send_accepted)
        self.assertEqual(len(recovery_channel.sent_events), 1)

        # 3. Third delivery attempt: already completed -> deduplicated, zero extra sends
        receipt3 = recovery_receiver.receive_event(event)
        self.assertTrue(receipt3.receiver_accepted)
        self.assertTrue(receipt3.channel_send_accepted)
        self.assertEqual(len(recovery_channel.sent_events), 1)  # No extra send!

        # 4. ACK after receiver acceptance succeeds
        ack_success = recovery_receiver.ack_event(event.event_id)
        self.assertTrue(ack_success)
        self.assertTrue(self.store.is_event_acked(event.event_id))

    def test_s07_t04_crash_around_channel_send_preserves_ambiguous_delivery_without_blind_resend(self) -> None:
        """S07-T04: Crash around channel send preserves ambiguous delivery without blind resend."""
        # Channel fails with network timeout (ambiguous delivery)
        channel = ScriptedChannel(
            name="slack",
            can_lookup=False,
            fail_on_send=TransportFailureError(ErrorCode.TIMEOUT, "Gateway timeout on channel send"),
        )
        receiver = DurableReceiver(
            store=self.store,
            channel=channel,
            clock=self.clock,
        )

        event = Event.create(
            event_id="evt_ambig_01",
            event_type="alert",
            resource_id="sessions/s2",
        )
        self.store.save_event(event)

        # 1. Delivery attempt: channel send fails ambiguously
        receipt = receiver.receive_event(event)
        self.assertTrue(receipt.receiver_accepted)
        self.assertFalse(receipt.channel_send_accepted)
        self.assertTrue(receipt.delivery_unknown)

        # Verify persisted state
        stored_rcpt = self.store.get_receipt("rcpt_evt_ambig_01")
        assert stored_rcpt is not None
        self.assertTrue(stored_rcpt.receiver_accepted)
        self.assertFalse(stored_rcpt.channel_send_accepted)
        self.assertTrue(stored_rcpt.delivery_unknown)

        # 2. Restart / redelivery with fresh receiver and channel without lookup on NEW store
        self.store.close()
        self.store = SQLiteStore(self.test_dir)
        recovery_channel = ScriptedChannel(name="slack", can_lookup=False)
        recovery_receiver = DurableReceiver(
            store=self.store,
            channel=recovery_channel,
            clock=self.clock,
        )

        # Redelivery MUST NOT blindly resend
        redelivered_receipt = recovery_receiver.receive_event(event)
        self.assertTrue(redelivered_receipt.delivery_unknown)
        self.assertFalse(redelivered_receipt.channel_send_accepted)
        # Crucial check: zero sends were attempted on the recovery channel
        self.assertEqual(len(recovery_channel.sent_events), 0)

    def test_s07_t04_crash_during_channel_send_intent_preserves_unknown_no_resend(self) -> None:
        """S07-T04: Crash during channel send (after send executed) preserves delivery_unknown; never blindly resent."""
        channel = ScriptedChannel(name="outbox", can_lookup=False)

        def crash_hook(point: str) -> None:
            if point == "during_channel_send":
                raise FakeTestCrash("Process terminated immediately after channel.send executed")

        receiver = DurableReceiver(
            store=self.store,
            channel=channel,
            clock=self.clock,
            fault_hook=crash_hook,
        )

        event = Event.create(
            event_id="evt_send_crash_01",
            event_type="alert",
            resource_id="sessions/s9",
        )
        self.store.save_event(event)

        # 1. Attempt send: channel.send is invoked, then during_channel_send hook kills the process
        with self.assertRaises(FakeTestCrash):
            receiver.receive_event(event)

        # channel.send was called exactly once before process killed
        self.assertEqual(len(channel.sent_events), 1)

        # 2. Process restart: close old store and open NEW SQLiteStore instance
        self.store.close()
        self.store = SQLiteStore(self.test_dir)

        # Persisted receipt in store has delivery_unknown=True and send_started=True
        saved_receipt = self.store.get_receipt("rcpt_evt_send_crash_01")
        self.assertIsNotNone(saved_receipt)
        assert saved_receipt is not None
        self.assertTrue(saved_receipt.receiver_accepted)
        self.assertFalse(saved_receipt.channel_send_accepted)
        self.assertTrue(saved_receipt.delivery_unknown)
        self.assertTrue(dict(saved_receipt.metadata).get("send_started"))

        # 3. Redelivery on restart with channel without idempotent lookup
        recovery_receiver = DurableReceiver(
            store=self.store,
            channel=channel,
            clock=self.clock,
        )

        receipt = recovery_receiver.receive_event(event)

        # Must NOT blindly resend: call count stays 1!
        self.assertEqual(len(channel.sent_events), 1)
        self.assertTrue(receipt.delivery_unknown)
        self.assertFalse(receipt.channel_send_accepted)

    def test_s07_t04_channel_idempotent_lookup_resolves_ambiguity_without_send(self) -> None:
        """S07-T04: Ambiguous delivery with idempotent lookup resolves affirmative delivery without send."""
        # Initial delivery fails ambiguously
        channel1 = ScriptedChannel(
            name="webhook",
            can_lookup=True,
            lookup_result=True,  # Lookup confirms the message did arrive
            fail_on_send=TransportFailureError(ErrorCode.TRANSPORT_ERROR, "Socket reset"),
        )
        receiver1 = DurableReceiver(self.store, channel=channel1, clock=self.clock)
        event = Event.create(event_id="evt_lookup_01", event_type="notification", resource_id="res_01")
        self.store.save_event(event)

        receipt1 = receiver1.receive_event(event)
        self.assertTrue(receipt1.delivery_unknown)

        # Redelivery on NEW store with idempotent lookup returning True
        self.store.close()
        self.store = SQLiteStore(self.test_dir)
        channel2 = ScriptedChannel(name="webhook", can_lookup=True, lookup_result=True)
        receiver2 = DurableReceiver(self.store, channel=channel2, clock=self.clock)

        resolved_receipt = receiver2.receive_event(event)
        self.assertTrue(resolved_receipt.channel_send_accepted)
        self.assertFalse(resolved_receipt.delivery_unknown)
        # Idempotent lookup was queried, and channel send was NOT called
        self.assertEqual(len(channel2.lookup_calls), 1)
        self.assertEqual(len(channel2.sent_events), 0)

    def test_s07_t04_channel_lookup_inconclusive_stays_unknown(self) -> None:
        """S07-T04: Inconclusive idempotent lookup leaves status delivery_unknown without blind send."""
        channel1 = ScriptedChannel(
            name="webhook",
            can_lookup=True,
            fail_on_send=TransportFailureError(ErrorCode.TRANSPORT_ERROR, "Socket reset"),
        )
        receiver1 = DurableReceiver(self.store, channel=channel1, clock=self.clock)
        event = Event.create(event_id="evt_inconclusive", event_type="notification", resource_id="res_02")
        self.store.save_event(event)
        receiver1.receive_event(event)

        # Redelivery on NEW store with lookup returning None (inconclusive)
        self.store.close()
        self.store = SQLiteStore(self.test_dir)
        channel2 = ScriptedChannel(name="webhook", can_lookup=True, lookup_result=None)
        receiver2 = DurableReceiver(self.store, channel=channel2, clock=self.clock)

        receipt = receiver2.receive_event(event)
        self.assertTrue(receipt.delivery_unknown)
        self.assertEqual(len(channel2.sent_events), 0)

    def test_s07_t04_channel_lookup_negative_safely_resends(self) -> None:
        """S07-T04: Ambiguous send where lookup explicitly confirms absence safely resends."""
        channel1 = ScriptedChannel(
            name="webhook",
            can_lookup=True,
            fail_on_send=TransportFailureError(ErrorCode.TRANSPORT_ERROR, "Socket reset"),
        )
        receiver1 = DurableReceiver(self.store, channel=channel1, clock=self.clock)
        event = Event.create(event_id="evt_neg_lookup", event_type="notification", resource_id="res_03")
        self.store.save_event(event)
        receiver1.receive_event(event)

        # Recovery on NEW store with channel confirming absence (lookup_result=False)
        self.store.close()
        self.store = SQLiteStore(self.test_dir)
        channel2 = ScriptedChannel(name="webhook", can_lookup=True, lookup_result=False)
        receiver2 = DurableReceiver(self.store, channel=channel2, clock=self.clock)

        receipt = receiver2.receive_event(event)
        self.assertTrue(receipt.channel_send_accepted)
        self.assertFalse(receipt.delivery_unknown)
        self.assertEqual(len(channel2.sent_events), 1)

    def test_s07_t04_ack_requires_receiver_acceptance(self) -> None:
        """S07-T04: Event cannot be acknowledged unless durable acceptance is verified."""
        receiver = DurableReceiver(self.store, clock=self.clock)

        # An event that was never received/accepted cannot be acked
        result = receiver.ack_event("evt_never_accepted")
        self.assertFalse(result)

        # Receive it properly
        event = Event.create(event_id="evt_accept_then_ack", event_type="test", resource_id="r1")
        self.store.save_event(event)
        receiver.receive_event(event)

        # Now ACK succeeds
        result2 = receiver.ack_event(event.event_id)
        self.assertTrue(result2)


if __name__ == "__main__":
    unittest.main()
