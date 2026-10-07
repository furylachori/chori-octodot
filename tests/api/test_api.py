"""Unit tests for typed Jules API read wrapper, pagination, and single-attempt mutations.

Covers:
- S02-T02: Pagination for 0, 1, 100, 101 records; empty page continuation, cycles, duplicates, caps.
- S02-T03: Empty success for message/approval, valid session for create, malformed/oversized/truncated responses.
- S02-T06: Mutation single transport attempt on all failure paths; GET backoff bounded with Retry-After.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import uuid

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.api import compute_mutation_request_hash, JulesClient
from octodot.contracts import DispatchTicket, TicketAuthority
from octodot.errors import ErrorCode, OctodotError
from octodot.models import (
    ActivityRecord,
    Coverage,
    MutationResponse,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
)
from octodot.transport import FakeClock, FixtureTransport


class InMemoryTicketAuthority:
    """In-memory ticket authority for testing single-use redemption."""

    def __init__(self) -> None:
        self.minted_tickets: dict[str, tuple[str, str, str]] = {}  # id -> (op_id, req_hash, nonce)
        self.consumed_tickets: set[str] = set()

    def mint(self, operation_id: str, request_hash: str, nonce: str = "nonce-1") -> DispatchTicket:
        t_id = f"ticket-{uuid.uuid4().hex[:8]}"
        self.minted_tickets[t_id] = (operation_id, request_hash, nonce)
        return DispatchTicket(
            ticket_id=t_id,
            operation_id=operation_id,
            request_hash=request_hash,
            nonce=nonce,
        )

    def redeem(self, ticket: DispatchTicket, request_hash: str) -> bool:
        if ticket.ticket_id in self.consumed_tickets:
            return False
        expected = self.minted_tickets.get(ticket.ticket_id)
        if expected is None:
            return False
        exp_op_id, exp_req_hash, exp_nonce = expected
        if (
            ticket.operation_id != exp_op_id
            or ticket.request_hash != exp_req_hash
            or ticket.nonce != exp_nonce
            or request_hash != exp_req_hash
        ):
            return False
        self.consumed_tickets.add(ticket.ticket_id)
        return True


class TestS02T02PaginationCyclesAndDuplicates(unittest.TestCase):
    """S02-T02: Pagination 0, 1, 100, 101 records, continuation, cycles, duplicates, caps."""

    def test_s02_t02_paginate_0_records(self) -> None:
        """S02-T02: Paginating an empty response returns 0 records and complete Coverage."""
        fixture = FixtureTransport(
            responses={
                ("GET", "/v1alpha/sources"): TransportOutcome(
                    status=200,
                    body=b'{"sources": []}',
                )
            }
        )
        client = JulesClient(transport=fixture)
        records, coverage = client.paginate_sources()
        self.assertEqual(len(records), 0)
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.pages, 1)
        self.assertEqual(coverage.items, 0)

    def test_s02_t02_paginate_1_record(self) -> None:
        """S02-T02: Paginating a single record returns 1 item and complete Coverage."""
        fixture = FixtureTransport(
            responses={
                ("GET", "/v1alpha/sessions"): TransportOutcome(
                    status=200,
                    body=b'{"sessions": [{"name": "sessions/sess-1", "state": "ACTIVE"}]}',
                )
            }
        )
        client = JulesClient(transport=fixture)
        records, coverage = client.paginate_sessions()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "sessions/sess-1")
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.pages, 1)
        self.assertEqual(coverage.items, 1)

    def test_s02_t02_paginate_100_records(self) -> None:
        """S02-T02: Paginating exactly 100 records returns all 100 items with complete Coverage."""
        items = [{"name": f"sessions/sess-{i}", "state": "ACTIVE"} for i in range(100)]
        body = json.dumps({"sessions": items}).encode("utf-8")
        fixture = FixtureTransport(
            responses={("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=body)}
        )
        client = JulesClient(transport=fixture)
        records, coverage = client.paginate_sessions(page_size=100)
        self.assertEqual(len(records), 100)
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.items, 100)

    def test_s02_t02_paginate_101_records_across_pages(self) -> None:
        """S02-T02: Paginating 101 records across 2 pages returns all 101 items with complete Coverage."""
        page1_items = [{"name": f"sessions/sess-{i}", "state": "ACTIVE"} for i in range(100)]
        page1_body = json.dumps({"sessions": page1_items, "nextPageToken": "page-2-token"}).encode("utf-8")
        page2_items = [{"name": "sessions/sess-100", "state": "ACTIVE"}]
        page2_body = json.dumps({"sessions": page2_items}).encode("utf-8")

        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions",
            [
                TransportOutcome(status=200, body=page1_body),
                TransportOutcome(status=200, body=page2_body),
            ],
        )
        client = JulesClient(transport=fixture)
        records, coverage = client.paginate_sessions(page_size=100)
        self.assertEqual(len(records), 101)
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.pages, 2)
        self.assertEqual(coverage.items, 101)

    def test_s02_t02_follow_empty_pages_with_continuation(self) -> None:
        """S02-T02: An empty page with a nextPageToken continues until terminal page."""
        page1_body = b'{"sessions": [], "nextPageToken": "token-after-empty"}'
        page2_body = b'{"sessions": [{"name": "sessions/sess-found", "state": "ACTIVE"}]}'

        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions",
            [
                TransportOutcome(status=200, body=page1_body),
                TransportOutcome(status=200, body=page2_body),
            ],
        )
        client = JulesClient(transport=fixture)
        records, coverage = client.paginate_sessions()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "sessions/sess-found")
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.pages, 2)

    def test_s02_t02_reject_token_cycles(self) -> None:
        """S02-T02: Cycle in nextPageToken raises OctodotError(ErrorCode.MALFORMED_RESPONSE)."""
        page1_body = b'{"sessions": [{"name": "sessions/s1", "state": "ACTIVE"}], "nextPageToken": "cycle-tok"}'
        page2_body = b'{"sessions": [{"name": "sessions/s2", "state": "ACTIVE"}], "nextPageToken": "cycle-tok"}'

        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions",
            [
                TransportOutcome(status=200, body=page1_body),
                TransportOutcome(status=200, body=page2_body),
            ],
        )
        client = JulesClient(transport=fixture)
        with self.assertRaises(OctodotError) as ctx:
            client.paginate_sessions()
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_RESPONSE)

    def test_s02_t02_reject_malformed_page_response(self) -> None:
        """S02-T02: Malformed JSON or non-list items field raises MALFORMED_RESPONSE."""
        fixture_bad_json = FixtureTransport(
            responses={("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=b"broken-json")}
        )
        client = JulesClient(transport=fixture_bad_json)
        with self.assertRaises(OctodotError) as ctx:
            client.paginate_sessions()
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_RESPONSE)

        fixture_bad_field = FixtureTransport(
            responses={("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=b'{"sessions": 12345}')}
        )
        client2 = JulesClient(transport=fixture_bad_field)
        with self.assertRaises(OctodotError) as ctx2:
            client2.paginate_sessions()
        self.assertEqual(ctx2.exception.code, ErrorCode.MALFORMED_RESPONSE)

    def test_s02_t02_reject_conflicting_duplicate_identities(self) -> None:
        """S02-T02: Duplicate resource name with conflicting attributes raises IDENTITY_AMBIGUOUS."""
        page1_body = b'{"sessions": [{"name": "sessions/s1", "state": "ACTIVE"}], "nextPageToken": "tok2"}'
        page2_body = b'{"sessions": [{"name": "sessions/s1", "state": "COMPLETED"}]}'

        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions",
            [
                TransportOutcome(status=200, body=page1_body),
                TransportOutcome(status=200, body=page2_body),
            ],
        )
        client = JulesClient(transport=fixture)
        with self.assertRaises(OctodotError) as ctx:
            client.paginate_sessions()
        self.assertEqual(ctx.exception.code, ErrorCode.IDENTITY_AMBIGUOUS)

    def test_s02_t02_respect_page_caps_returns_partial_coverage(self) -> None:
        """S02-T02: Hitting max_pages returns partial Coverage with resume info without raising."""
        page1_body = b'{"sessions": [{"name": "sessions/s1", "state": "ACTIVE"}], "nextPageToken": "tok2"}'
        page2_body = b'{"sessions": [{"name": "sessions/s2", "state": "ACTIVE"}], "nextPageToken": "tok3"}'

        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions",
            [
                TransportOutcome(status=200, body=page1_body),
                TransportOutcome(status=200, body=page2_body),
            ],
        )
        client = JulesClient(transport=fixture)
        # Cap at 2 pages while more exist
        records, coverage = client.paginate_sessions(max_pages=2)
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.pages, 2)
        self.assertEqual(coverage.items, 2)
        self.assertEqual(coverage.resume_ref, "tok3")
        self.assertIn("page_cap_reached", coverage.reasons)

    def test_s02_t02_reject_duplicate_key_body_get(self) -> None:
        """S02-T02: GET response with duplicate dictionary keys raises MALFORMED_RESPONSE."""
        dup_key_body = b'{"sessions": [], "sessions": []}'
        fixture = FixtureTransport(
            responses={("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=dup_key_body)}
        )
        client = JulesClient(transport=fixture)
        with self.assertRaises(OctodotError) as ctx:
            client.sessions_list()
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_RESPONSE)

    def test_s02_t02_reject_nan_body_get(self) -> None:
        """S02-T02: GET response with NaN floating point constant raises MALFORMED_RESPONSE."""
        nan_body = b'{"sessions": [], "metric": NaN}'
        fixture = FixtureTransport(
            responses={("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=nan_body)}
        )
        client = JulesClient(transport=fixture)
        with self.assertRaises(OctodotError) as ctx:
            client.sessions_list()
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_RESPONSE)

    def test_s02_t02_unknown_fields_tolerated_get(self) -> None:
        """S02-T02: Unknown response fields in GET are tolerated and preserved in unknown_fields."""
        body = b'{"name": "sessions/sess-1", "state": "ACTIVE", "futureField": "futureVal"}'
        fixture = FixtureTransport(
            responses={("GET", "/v1alpha/sessions/sess-1"): TransportOutcome(status=200, body=body)}
        )
        client = JulesClient(transport=fixture)
        rec = client.sessions_get("sess-1")
        self.assertEqual(rec.name, "sessions/sess-1")
        self.assertEqual(rec.state, "ACTIVE")
        self.assertIn(("futureField", "futureVal"), rec.unknown_fields)


class TestS02T03MutationSuccessAndMalformedHandling(unittest.TestCase):
    """S02-T03: Empty success for message/approval, valid session for create, malformed responses."""

    def setUp(self) -> None:
        self.authority = InMemoryTicketAuthority()

    def test_s02_t03_empty_success_send_message(self) -> None:
        """S02-T03: sendMessage returns empty success with uncertain_effect=False."""
        target = "/v1alpha/sessions/sess-1:sendMessage"
        body = {"prompt": "Please review this pull request."}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-send-1", request_hash=req_hash)

        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_send_message(ticket, "sessions/sess-1", body)
        self.assertEqual(resp.outcome.status, 200)
        self.assertFalse(resp.outcome.uncertain_effect)
        self.assertIsNone(resp.session)
        self.assertEqual(len(fixture.calls), 1)

    def test_s02_t03_empty_success_approve_plan(self) -> None:
        """S02-T03: approvePlan returns empty success with uncertain_effect=False."""
        target = "/v1alpha/sessions/sess-1:approvePlan"
        body: dict[str, str] = {}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-app-1", request_hash=req_hash)

        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=b"{}")}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_approve_plan(ticket, "sessions/sess-1")
        self.assertEqual(resp.outcome.status, 200)
        self.assertFalse(resp.outcome.uncertain_effect)
        self.assertIsNone(resp.session)
        self.assertEqual(len(fixture.calls), 1)

    def test_s02_t03_valid_session_create(self) -> None:
        """S02-T03: create returns parsed SessionRecord with uncertain_effect=False."""
        target = "/v1alpha/sessions"
        body = {"title": "New Task", "prompt": "Investigate bug"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-create-1", request_hash=req_hash)

        created_data = {
            "name": "sessions/sess-new-123",
            "state": "ACTIVE",
            "title": "New Task",
            "requirePlanApproval": True,
        }
        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=json.dumps(created_data).encode("utf-8"))}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertEqual(resp.outcome.status, 200)
        self.assertFalse(resp.outcome.uncertain_effect)
        self.assertIsNotNone(resp.session)
        self.assertEqual(resp.session.name, "sessions/sess-new-123")
        self.assertEqual(resp.session.state, "ACTIVE")
        self.assertEqual(resp.session.title, "New Task")

    def test_s02_t03_malformed_response_handling_create(self) -> None:
        """S02-T03: Malformed JSON body in create response marks uncertain_effect=True."""
        target = "/v1alpha/sessions"
        body = {"title": "Task"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-c-mal", request_hash=req_hash)

        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=b"broken-non-json")}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.MALFORMED_RESPONSE)
        self.assertIsNone(resp.session)

    def test_s02_t03_oversized_response_handling(self) -> None:
        """S02-T03: Oversized response from transport marks uncertain_effect=True."""
        target = "/v1alpha/sessions"
        body = {"title": "Task"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-c-over", request_hash=req_hash)

        fixture = FixtureTransport(
            responses={
                ("POST", target): TransportOutcome(
                    status=200,
                    body=None,
                    byte_count=9000000,
                    uncertain_effect=True,
                    sanitized_error_code=ErrorCode.OVERSIZED_RESPONSE,
                )
            }
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.OVERSIZED_RESPONSE)
        self.assertIsNone(resp.session)

    def test_s02_t03_truncated_response_handling(self) -> None:
        """S02-T03: Truncated response from transport marks uncertain_effect=True."""
        target = "/v1alpha/sessions"
        body = {"title": "Task"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-c-trunc", request_hash=req_hash)

        fixture = FixtureTransport(
            responses={
                ("POST", target): TransportOutcome(
                    status=0,
                    body=None,
                    byte_count=50,
                    uncertain_effect=True,
                    sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                )
            }
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertIsNone(resp.session)

    def test_s02_t03_mutation_create_duplicate_key_body_marks_uncertain(self) -> None:
        """S02-T03: POST create response with duplicate keys marks uncertain_effect=True (unknown)."""
        target = "/v1alpha/sessions"
        body = {"title": "Task"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-dup-create", request_hash=req_hash)

        dup_body = b'{"name": "sessions/s1", "name": "sessions/s2"}'
        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=dup_body)}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.MALFORMED_RESPONSE)
        self.assertIsNone(resp.session)

    def test_s02_t03_mutation_create_nan_body_marks_uncertain(self) -> None:
        """S02-T03: POST create response with NaN marks uncertain_effect=True (unknown)."""
        target = "/v1alpha/sessions"
        body = {"title": "Task"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-nan-create", request_hash=req_hash)

        nan_body = b'{"name": "sessions/s1", "state": "ACTIVE", "score": NaN}'
        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=nan_body)}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.MALFORMED_RESPONSE)
        self.assertIsNone(resp.session)

    def test_s02_t03_mutation_send_message_duplicate_key_body_marks_uncertain(self) -> None:
        """S02-T03: POST sendMessage response with duplicate keys marks uncertain_effect=True."""
        target = "/v1alpha/sessions/sess-1:sendMessage"
        body = {"prompt": "Hello"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-dup-msg", request_hash=req_hash)

        dup_body = b'{"ok": true, "ok": false}'
        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=dup_body)}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_send_message(ticket, "sessions/sess-1", body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.MALFORMED_RESPONSE)

    def test_s02_t03_mutation_send_message_nan_body_marks_uncertain(self) -> None:
        """S02-T03: POST sendMessage response with NaN marks uncertain_effect=True."""
        target = "/v1alpha/sessions/sess-1:sendMessage"
        body = {"prompt": "Hello"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-nan-msg", request_hash=req_hash)

        nan_body = b'{"metric": NaN}'
        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=nan_body)}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_send_message(ticket, "sessions/sess-1", body)
        self.assertTrue(resp.outcome.uncertain_effect)
        self.assertEqual(resp.outcome.sanitized_error_code, ErrorCode.MALFORMED_RESPONSE)

    def test_s02_t03_unknown_fields_tolerated_create(self) -> None:
        """S02-T03: Unknown response fields in POST create are tolerated and preserved."""
        target = "/v1alpha/sessions"
        body = {"title": "Task"}
        req_hash = compute_mutation_request_hash(target, body)
        ticket = self.authority.mint(operation_id="op-unk-create", request_hash=req_hash)

        data = {
            "name": "sessions/sess-created",
            "state": "ACTIVE",
            "futureField": "futureVal",
        }
        fixture = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=200, body=json.dumps(data).encode("utf-8"))}
        )
        client = JulesClient(transport=fixture, ticket_authority=self.authority)

        resp = client.sessions_create(ticket, body)
        self.assertEqual(resp.outcome.status, 200)
        self.assertFalse(resp.outcome.uncertain_effect)
        self.assertIsNotNone(resp.session)
        self.assertEqual(resp.session.name, "sessions/sess-created")
        self.assertIn(("futureField", "futureVal"), resp.session.unknown_fields)


class TestS02T06MutationSingleAttemptAndGetBackoff(unittest.TestCase):
    """S02-T06: Exactly one transport attempt for mutations; bounded GET backoff honoring Retry-After."""

    def setUp(self) -> None:
        self.authority = InMemoryTicketAuthority()

    def test_s02_t06_mutation_failure_paths_record_at_most_one_attempt(self) -> None:
        """S02-T06: Every mutation failure path records at most 1 transport attempt."""
        target = "/v1alpha/sessions/sess-1:sendMessage"
        body = {"prompt": "Ping"}
        req_hash = compute_mutation_request_hash(target, body)

        # 1. Redeem failure: zero attempts
        bad_ticket = DispatchTicket("bad-id", "op-bad", "sha256:wrong", "nonce")
        fixture_0 = FixtureTransport()
        client_0 = JulesClient(transport=fixture_0, ticket_authority=self.authority)
        with self.assertRaises(OctodotError) as ctx:
            client_0.sessions_send_message(bad_ticket, "sess-1", body)
        self.assertEqual(ctx.exception.code, ErrorCode.GRANT_INVALID)
        self.assertEqual(len(fixture_0.calls), 0)

        # 2. 400 Bad Request
        ticket_400 = self.authority.mint("op-400", req_hash)
        fixture_400 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=400, body=b"Bad")}
        )
        client_400 = JulesClient(transport=fixture_400, ticket_authority=self.authority)
        resp_400 = client_400.sessions_send_message(ticket_400, "sess-1", body)
        self.assertEqual(len(fixture_400.calls), 1)
        self.assertFalse(resp_400.outcome.uncertain_effect)

        # 3. 401 Unauthorized
        ticket_401 = self.authority.mint("op-401", req_hash)
        fixture_401 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=401, sanitized_error_code=ErrorCode.AUTH_DENIED)}
        )
        client_401 = JulesClient(transport=fixture_401, ticket_authority=self.authority)
        resp_401 = client_401.sessions_send_message(ticket_401, "sess-1", body)
        self.assertEqual(len(fixture_401.calls), 1)
        self.assertFalse(resp_401.outcome.uncertain_effect)

        # 4. 403 Forbidden
        ticket_403 = self.authority.mint("op-403", req_hash)
        fixture_403 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=403, sanitized_error_code=ErrorCode.AUTH_DENIED)}
        )
        client_403 = JulesClient(transport=fixture_403, ticket_authority=self.authority)
        resp_403 = client_403.sessions_send_message(ticket_403, "sess-1", body)
        self.assertEqual(len(fixture_403.calls), 1)
        self.assertFalse(resp_403.outcome.uncertain_effect)

        # 5. 429 Rate Limited (NEVER retried for mutation!)
        ticket_429 = self.authority.mint("op-429", req_hash)
        fixture_429 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=429, retry_after=5.0)}
        )
        client_429 = JulesClient(transport=fixture_429, ticket_authority=self.authority)
        resp_429 = client_429.sessions_send_message(ticket_429, "sess-1", body)
        self.assertEqual(len(fixture_429.calls), 1)
        self.assertFalse(resp_429.outcome.uncertain_effect)

        # 6. 500 Internal Server Error (NEVER retried for mutation!)
        ticket_500 = self.authority.mint("op-500", req_hash)
        fixture_500 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=500, uncertain_effect=True)}
        )
        client_500 = JulesClient(transport=fixture_500, ticket_authority=self.authority)
        resp_500 = client_500.sessions_send_message(ticket_500, "sess-1", body)
        self.assertEqual(len(fixture_500.calls), 1)
        self.assertTrue(resp_500.outcome.uncertain_effect)

        # 7. 503 Service Unavailable (NEVER retried for mutation!)
        ticket_503 = self.authority.mint("op-503", req_hash)
        fixture_503 = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=503, retry_after=10.0, uncertain_effect=True)}
        )
        client_503 = JulesClient(transport=fixture_503, ticket_authority=self.authority)
        resp_503 = client_503.sessions_send_message(ticket_503, "sess-1", body)
        self.assertEqual(len(fixture_503.calls), 1)
        self.assertTrue(resp_503.outcome.uncertain_effect)

        # 8. Timeout (NEVER retried for mutation!)
        ticket_to = self.authority.mint("op-to", req_hash)
        fixture_to = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=0, sanitized_error_code=ErrorCode.TIMEOUT, uncertain_effect=True)}
        )
        client_to = JulesClient(transport=fixture_to, ticket_authority=self.authority)
        resp_to = client_to.sessions_send_message(ticket_to, "sess-1", body)
        self.assertEqual(len(fixture_to.calls), 1)
        self.assertTrue(resp_to.outcome.uncertain_effect)

        # 9. Disconnect (NEVER retried for mutation!)
        ticket_disc = self.authority.mint("op-disc", req_hash)
        fixture_disc = FixtureTransport(
            responses={("POST", target): TransportOutcome(status=0, sanitized_error_code=ErrorCode.TRANSPORT_ERROR, uncertain_effect=True)}
        )
        client_disc = JulesClient(transport=fixture_disc, ticket_authority=self.authority)
        resp_disc = client_disc.sessions_send_message(ticket_disc, "sess-1", body)
        self.assertEqual(len(fixture_disc.calls), 1)
        self.assertTrue(resp_disc.outcome.uncertain_effect)

    def test_s02_t06_get_backoff_rate_limited_respects_retry_after(self) -> None:
        """S02-T06: GET 429 honors Retry-After header with fake clock sleep and retries."""
        fake_clock = FakeClock()
        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions/sess-1",
            [
                TransportOutcome(status=429, retry_after=3.5, sanitized_error_code=ErrorCode.RATE_LIMITED),
                TransportOutcome(status=200, body=b'{"name": "sessions/sess-1", "state": "ACTIVE"}'),
            ],
        )
        client = JulesClient(transport=fixture, clock=fake_clock)
        record = client.sessions_get("sess-1")
        self.assertEqual(record.name, "sessions/sess-1")
        self.assertEqual(len(fixture.calls), 2)
        self.assertEqual(fake_clock.sleep_calls, [3.5])

    def test_s02_t06_get_backoff_service_unavailable_respects_retry_after(self) -> None:
        """S02-T06: GET 503 honors Retry-After header with fake clock sleep and retries."""
        fake_clock = FakeClock()
        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions/sess-1",
            [
                TransportOutcome(status=503, retry_after=4.0, sanitized_error_code=ErrorCode.TRANSPORT_ERROR),
                TransportOutcome(status=200, body=b'{"name": "sessions/sess-1", "state": "ACTIVE"}'),
            ],
        )
        client = JulesClient(transport=fixture, clock=fake_clock)
        record = client.sessions_get("sess-1")
        self.assertEqual(record.name, "sessions/sess-1")
        self.assertEqual(len(fixture.calls), 2)
        self.assertEqual(fake_clock.sleep_calls, [4.0])

    def test_s02_t06_get_exponential_backoff_on_transient_500(self) -> None:
        """S02-T06: GET 500 uses exponential backoff and succeeds on 3rd attempt."""
        fake_clock = FakeClock()
        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions/sess-1",
            [
                TransportOutcome(status=500, sanitized_error_code=ErrorCode.TRANSPORT_ERROR),
                TransportOutcome(status=500, sanitized_error_code=ErrorCode.TRANSPORT_ERROR),
                TransportOutcome(status=200, body=b'{"name": "sessions/sess-1", "state": "ACTIVE"}'),
            ],
        )
        client = JulesClient(transport=fixture, clock=fake_clock, initial_backoff=1.0)
        record = client.sessions_get("sess-1")
        self.assertEqual(record.name, "sessions/sess-1")
        self.assertEqual(len(fixture.calls), 3)
        self.assertEqual(fake_clock.sleep_calls, [1.0, 2.0])

    def test_s02_t06_get_retries_bounded_by_max_attempts(self) -> None:
        """S02-T06: GET retry stops after max_get_retries and raises OctodotError."""
        fake_clock = FakeClock()
        fixture = FixtureTransport()
        fixture.set_response(
            "GET",
            "/v1alpha/sessions/sess-1",
            [
                TransportOutcome(status=500, sanitized_error_code=ErrorCode.TRANSPORT_ERROR),
                TransportOutcome(status=500, sanitized_error_code=ErrorCode.TRANSPORT_ERROR),
                TransportOutcome(status=500, sanitized_error_code=ErrorCode.TRANSPORT_ERROR),
            ],
        )
        client = JulesClient(transport=fixture, clock=fake_clock, max_get_retries=3)
        with self.assertRaises(OctodotError) as ctx:
            client.sessions_get("sess-1")
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSPORT_ERROR)
        self.assertEqual(len(fixture.calls), 3)
        self.assertEqual(len(fake_clock.sleep_calls), 2)  # Slept after attempt 1 and 2

    def test_s02_t06_get_does_not_retry_non_transient_4xx(self) -> None:
        """S02-T06: Non-transient 4xx (404, 401, 403) are never retried."""
        fake_clock = FakeClock()
        fixture_404 = FixtureTransport(
            responses={("GET", "/v1alpha/sessions/sess-nonexistent"): TransportOutcome(status=404)}
        )
        client_404 = JulesClient(transport=fixture_404, clock=fake_clock)
        with self.assertRaises(OctodotError) as ctx404:
            client_404.sessions_get("sess-nonexistent")
        self.assertEqual(ctx404.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(len(fixture_404.calls), 1)
        self.assertEqual(len(fake_clock.sleep_calls), 0)

        fixture_401 = FixtureTransport(
            responses={("GET", "/v1alpha/sessions/sess-1"): TransportOutcome(status=401, sanitized_error_code=ErrorCode.AUTH_DENIED)}
        )
        client_401 = JulesClient(transport=fixture_401, clock=fake_clock)
        with self.assertRaises(OctodotError) as ctx401:
            client_401.sessions_get("sess-1")
        self.assertEqual(ctx401.exception.code, ErrorCode.AUTH_DENIED)
        self.assertEqual(len(fixture_401.calls), 1)
        self.assertEqual(len(fake_clock.sleep_calls), 0)


if __name__ == "__main__":
    unittest.main()
