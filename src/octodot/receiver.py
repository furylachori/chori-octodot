"""Durable receiver and outbox/channel delivery tracking for octodot.

Standard library only. Compatible with Python 3.10+.
Provides:
- Receiver protocol implementation with durable acceptance into SQLite BEFORE ACK.
- Segregated receipt stages: receiver_accepted, channel_send_accepted, delivery_unknown.
- Crash recovery: redelivery of an accepted event is deduplicated.
- Ambiguous channel send handling: ambiguous sends without idempotent lookup stay
  delivery_unknown and are never blindly resent.
- Fault hooks for crash injection and deterministic test verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from typing import Any, Callable, Protocol

from octodot.contracts import Clock, Receiver as ReceiverProtocol, Store
from octodot.errors import (
    ErrorCode,
    OctodotError,
    StateStoreError,
    TransportFailureError,
)
from octodot.models import Event, Receipt
from octodot.transport import SystemClock


class Channel(Protocol):
    """Outbound communication or notification channel protocol."""

    @property
    def name(self) -> str:
        """Channel identifier or name."""
        ...

    def send(self, event: Event, receipt: Receipt) -> None:
        """Send the event to the outbound channel.

        Raises:
            Exception / TransportFailureError if send fails or is ambiguous.
        """
        ...

    def has_idempotent_lookup(self) -> bool:
        """Return True if channel supports checking whether a past send was delivered."""
        ...

    def lookup_send_status(self, event_id: str, receipt_id: str) -> bool | None:
        """Check status of previous send attempt.

        Returns:
            True: Confirmed delivered / accepted by channel.
            False: Confirmed not delivered / not present.
            None: Ambiguous / unknown status.
        """
        ...


def _utc_now_iso(clock: Clock) -> str:
    """Return formatted UTC ISO8601 string from clock."""
    dt = clock.now_utc()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class DurableReceiver:
    """Durable event receiver accepting events into SQLite before ACK.

    Maintains distinct receipt flags:
    - receiver_accepted: event was successfully persisted to receiver_receipts.
    - channel_send_accepted: event was successfully delivered to the outbound channel.
    - delivery_unknown: channel delivery attempt outcome is ambiguous.

    Crucial guarantees:
    - Acceptance is persisted durably before ACK.
    - If channel send is ambiguous and channel lacks idempotent lookup,
      status stays delivery_unknown and is NEVER blindly resent.
    - Redelivery of an already-accepted event returns the existing receipt (deduplicated).
    - An event cannot be acknowledged unless durable acceptance is verified.
    """

    def __init__(
        self,
        store: Store,
        channel: Channel | None = None,
        clock: Clock | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.channel = channel
        self.clock = clock or SystemClock()
        self.fault_hook = fault_hook

    def set_fault_hook(self, hook: Callable[[str], None] | None) -> None:
        """Inject or clear crash/fault hook."""
        self.fault_hook = hook

    def receive_event(self, event: Event, receipt_id: str | None = None) -> Receipt:
        """Accept an event durably into store and attempt channel delivery.

        Idempotent by receipt_id (defaults to 'rcpt_{event.event_id}').
        """
        rcpt_id = receipt_id or f"rcpt_{event.event_id}"
        now_str = _utc_now_iso(self.clock)
        chan_name = self.channel.name if self.channel is not None else None

        # 1. Check existing receipt for redelivery / deduplication
        existing = self.store.get_receipt(rcpt_id)
        if existing is not None:
            # Case A: Already accepted and channel send accepted
            if existing.channel_send_accepted:
                return existing

            existing_meta = dict(existing.metadata)
            send_started = existing.delivery_unknown or bool(existing_meta.get("send_started", False))

            # Case B: Send intent was persisted previously (delivery ambiguous or in-flight)
            if send_started:
                if self.channel is not None and self.channel.has_idempotent_lookup():
                    lookup = self.channel.lookup_send_status(event.event_id, rcpt_id)
                    if lookup is True:
                        # Confirmed delivered!
                        resolved = Receipt(
                            receipt_id=rcpt_id,
                            event_id=event.event_id,
                            receiver_accepted=True,
                            channel_send_accepted=True,
                            delivery_unknown=False,
                            channel=existing.channel or chan_name,
                            timestamp=now_str,
                            metadata=existing.metadata,
                        )
                        self.store.save_receipt(resolved)
                        return resolved
                    elif lookup is False:
                        # Confirmed not delivered, can safely attempt send now
                        receipt = existing
                    else:
                        # Inconclusive lookup: must remain delivery_unknown, never blindly resent!
                        return existing
                else:
                    # No idempotent lookup: must remain delivery_unknown, never blindly resent!
                    return existing
            else:
                # Case C: Already accepted, but send intent had not started (e.g. crash right after step 1)
                # Proceed to channel send below using existing receipt
                receipt = existing
        else:
            # 2. Stage 1: Durable acceptance BEFORE ACK and BEFORE channel send
            receipt = Receipt(
                receipt_id=rcpt_id,
                event_id=event.event_id,
                receiver_accepted=True,
                channel_send_accepted=False,
                delivery_unknown=False,
                channel=chan_name,
                timestamp=now_str,
            )
            self.store.save_receipt(receipt)

            # Fault hook point: crash after receiver acceptance, before ACK or channel send
            if self.fault_hook:
                self.fault_hook("after_receiver_acceptance")

        # 3. Stage 2: Outbound channel send (if configured)
        if self.channel is not None and not receipt.channel_send_accepted:
            if self.fault_hook:
                self.fault_hook("before_channel_send")

            # Persist send intent before attempting channel send
            intent_receipt = Receipt(
                receipt_id=rcpt_id,
                event_id=event.event_id,
                receiver_accepted=True,
                channel_send_accepted=False,
                delivery_unknown=True,
                channel=chan_name,
                timestamp=now_str,
                metadata=(("send_started", True),),
            )
            self.store.save_receipt(intent_receipt)
            receipt = intent_receipt

            try:
                self.channel.send(event, receipt)
            except Exception as e:
                # Ambiguous delivery or network failure during send
                err_meta = (("send_started", True), ("error", str(e)), ("error_type", type(e).__name__))
                receipt = Receipt(
                    receipt_id=rcpt_id,
                    event_id=event.event_id,
                    receiver_accepted=True,
                    channel_send_accepted=False,
                    delivery_unknown=True,
                    channel=chan_name,
                    timestamp=now_str,
                    metadata=err_meta,
                )
                self.store.save_receipt(receipt)
                if isinstance(e, OctodotError) and e.code == ErrorCode.CANCELLED:
                    raise
                return receipt

            if self.fault_hook:
                self.fault_hook("during_channel_send")
            if self.fault_hook:
                self.fault_hook("after_channel_send")

            # Channel send succeeded
            receipt = Receipt(
                receipt_id=rcpt_id,
                event_id=event.event_id,
                receiver_accepted=True,
                channel_send_accepted=True,
                delivery_unknown=False,
                channel=chan_name,
                timestamp=now_str,
            )
            self.store.save_receipt(receipt)

        return receipt

    def ack_event(self, event_id: str) -> bool:
        """Acknowledge event delivery.

        Enforces: Durable acceptance MUST exist before ACK can succeed.
        """
        rcpt_id = f"rcpt_{event_id}"
        rcpt = self.store.get_receipt(rcpt_id)
        if rcpt is None or not rcpt.receiver_accepted:
            # Untrusted / absent receiver receipt blocks ACK
            return False

        now_str = _utc_now_iso(self.clock)
        return self.store.ack_event(event_id, acked_at=now_str)
