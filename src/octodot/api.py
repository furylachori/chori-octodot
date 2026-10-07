"""Typed Jules API read wrapper and ticket-gated mutation endpoints.

Standard library only. Compatible with Python 3.10+.
Implements JulesReadAPI protocol with bounded GET retries, Retry-After honoring,
cycle-detecting pagination, and ticket-gated single-attempt mutations.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from octodot.contracts import (
    canonical_bytes,
    Clock,
    JulesReadAPI,
    load_strict_json,
    request_hash,
    TicketAuthority,
    Transport,
)
from octodot.errors import ErrorCode, OctodotError
from octodot.models import (
    ActivityRecord,
    Coverage,
    DispatchTicket,
    MutationResponse,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
)
from octodot.transport import FakeClock, MAX_RESPONSE_BYTES, SystemClock


def compute_mutation_request_hash(target: str, body: dict[str, Any]) -> str:
    """Compute canonical request hash for a mutation over target and body."""
    return request_hash({"target": target, "body": body})


class JulesClient:
    """Typed Jules API wrapper implementing the JulesReadAPI protocol.

    - Reads: sources.list/get, sessions.list/get, activities.list/get.
      Bounded GET retries honoring Retry-After on 429/503 and transient errors.
    - Pagination: follows nextPageToken (including empty pages), detects token cycles,
      rejects conflicting duplicate identities, respects page caps (returns partial Coverage).
    - Mutations: create, send_message, approve_plan require a DispatchTicket and call
      TicketAuthority.redeem(ticket, request_hash) before any transport call.
      Exactly one transport attempt, never retried.
    """

    def __init__(
        self,
        transport: Transport,
        ticket_authority: TicketAuthority | None = None,
        clock: Clock | None = None,
        max_get_retries: int = 3,
        initial_backoff: float = 1.0,
        max_backoff: float = 10.0,
        allow_filter: bool = False,
    ) -> None:
        self.transport = transport
        self.ticket_authority = ticket_authority
        self.clock = clock or SystemClock()
        self.max_get_retries = max(1, max_get_retries)
        self.initial_backoff = max(0.01, initial_backoff)
        self.max_backoff = max(self.initial_backoff, max_backoff)
        self.allow_filter = allow_filter

    # =================================================================
    # Internal GET execution with retry
    # =================================================================

    def _execute_get_with_retry(
        self,
        path: str,
        query: dict[str, Any] | None = None,
    ) -> TransportOutcome:
        """Execute GET request with bounded exponential backoff honoring Retry-After."""
        current_backoff = self.initial_backoff
        last_outcome: TransportOutcome | None = None

        for attempt in range(1, self.max_get_retries + 1):
            outcome = self.transport.request("GET", path, query=query)
            last_outcome = outcome

            if outcome.status == 200:
                return outcome

            # If final attempt, do not sleep/retry
            if attempt >= self.max_get_retries:
                break

            # Handle 429 Rate Limited
            if outcome.status == 429:
                wait_time = outcome.retry_after if outcome.retry_after is not None else current_backoff
                sleep_seconds = min(max(wait_time, self.initial_backoff), self.max_backoff)
                self.clock.sleep(sleep_seconds)
                current_backoff = min(current_backoff * 2.0, self.max_backoff)
                continue

            # Handle 503 Service Unavailable
            if outcome.status == 503:
                wait_time = outcome.retry_after if outcome.retry_after is not None else current_backoff
                sleep_seconds = min(max(wait_time, self.initial_backoff), self.max_backoff)
                self.clock.sleep(sleep_seconds)
                current_backoff = min(current_backoff * 2.0, self.max_backoff)
                continue

            # Handle transient server / network errors (500, 502, 504 or status 0)
            if outcome.status in (500, 502, 504) or (
                outcome.status == 0
                and outcome.sanitized_error_code in (ErrorCode.TIMEOUT, ErrorCode.TRANSPORT_ERROR)
            ):
                sleep_seconds = min(current_backoff, self.max_backoff)
                self.clock.sleep(sleep_seconds)
                current_backoff = min(current_backoff * 2.0, self.max_backoff)
                continue

            # Non-retryable error (400, 401, 403, 404, etc.)
            break

        # If we got here, request failed after attempts
        assert last_outcome is not None
        code = last_outcome.sanitized_error_code
        if code == ErrorCode.AUTH_DENIED or last_outcome.status in (401, 403):
            raise OctodotError(ErrorCode.AUTH_DENIED, f"Authentication denied: HTTP {last_outcome.status}")
        if code == ErrorCode.RATE_LIMITED or last_outcome.status == 429:
            raise OctodotError(ErrorCode.RATE_LIMITED, "Rate limited")
        if code == ErrorCode.TIMEOUT:
            raise OctodotError(ErrorCode.TIMEOUT, "Request timed out")
        if code == ErrorCode.BUDGET_EXHAUSTED:
            raise OctodotError(ErrorCode.BUDGET_EXHAUSTED, "Budget exhausted")
        if code == ErrorCode.OVERSIZED_RESPONSE:
            raise OctodotError(ErrorCode.OVERSIZED_RESPONSE, "Response oversized")
        if last_outcome.status == 404:
            raise OctodotError(ErrorCode.INVALID_INPUT, f"Resource not found: {path}")

        raise OctodotError(
            code or ErrorCode.TRANSPORT_ERROR,
            f"GET request failed with HTTP {last_outcome.status}",
        )

    # =================================================================
    # Read API Methods (Single-Page)
    # =================================================================

    def sources_list(
        self,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> tuple[tuple[SourceRecord, ...], str | None]:
        """List connected repository sources (single page)."""
        query: dict[str, Any] = {"pageSize": page_size}
        if page_token:
            query["pageToken"] = page_token

        outcome = self._execute_get_with_retry("/v1alpha/sources", query=query)
        data = self._parse_json_dict(outcome.body, "/v1alpha/sources")

        raw_list = data.get("sources")
        if raw_list is None:
            raw_list = []
        elif not isinstance(raw_list, list):
            raise OctodotError(
                ErrorCode.MALFORMED_RESPONSE,
                "Malformed sources list: 'sources' field must be a list",
            )

        records = tuple(SourceRecord.from_dict(item) for item in raw_list)
        next_token = data.get("nextPageToken") or None
        return records, next_token

    def sources_get(self, name: str) -> SourceRecord:
        """Get source details by resource name."""
        clean_name = name.strip()
        if clean_name.startswith("sources/"):
            path = f"/v1alpha/{clean_name}"
        else:
            path = f"/v1alpha/sources/{clean_name}"

        outcome = self._execute_get_with_retry(path)
        data = self._parse_json_dict(outcome.body, path)
        return SourceRecord.from_dict(data)

    def sessions_list(
        self,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> tuple[tuple[SessionRecord, ...], str | None]:
        """List sessions paginated (single page)."""
        query: dict[str, Any] = {"pageSize": page_size}
        if page_token:
            query["pageToken"] = page_token

        outcome = self._execute_get_with_retry("/v1alpha/sessions", query=query)
        data = self._parse_json_dict(outcome.body, "/v1alpha/sessions")

        raw_list = data.get("sessions")
        if raw_list is None:
            raw_list = []
        elif not isinstance(raw_list, list):
            raise OctodotError(
                ErrorCode.MALFORMED_RESPONSE,
                "Malformed sessions list: 'sessions' field must be a list",
            )

        records = tuple(SessionRecord.from_dict(item) for item in raw_list)
        next_token = data.get("nextPageToken") or None
        return records, next_token

    def sessions_get(self, name: str) -> SessionRecord:
        """Get session details by resource name."""
        clean_name = name.strip()
        if clean_name.startswith("sessions/"):
            path = f"/v1alpha/{clean_name}"
        else:
            path = f"/v1alpha/sessions/{clean_name}"

        outcome = self._execute_get_with_retry(path)
        data = self._parse_json_dict(outcome.body, path)
        return SessionRecord.from_dict(data)

    def activities_list(
        self,
        session_name: str,
        page_token: str | None = None,
        page_size: int = 100,
        create_time_filter: str | None = None,
    ) -> tuple[tuple[ActivityRecord, ...], str | None]:
        """List activities for a session (single page)."""
        clean_session = session_name.strip()
        if clean_session.startswith("sessions/"):
            path = f"/v1alpha/{clean_session}/activities"
        else:
            path = f"/v1alpha/sessions/{clean_session}/activities"

        query: dict[str, Any] = {"pageSize": page_size}
        if page_token:
            query["pageToken"] = page_token
        if create_time_filter:
            if not self.allow_filter:
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    "Filtering activities is disabled unless allow_filter=True",
                )
            query["filter"] = create_time_filter

        outcome = self._execute_get_with_retry(path, query=query)
        data = self._parse_json_dict(outcome.body, path)

        raw_list = data.get("activities")
        if raw_list is None:
            raw_list = []
        elif not isinstance(raw_list, list):
            raise OctodotError(
                ErrorCode.MALFORMED_RESPONSE,
                "Malformed activities list: 'activities' field must be a list",
            )

        records = tuple(ActivityRecord.from_dict(item) for item in raw_list)
        next_token = data.get("nextPageToken") or None
        return records, next_token

    def activities_get(self, name: str) -> ActivityRecord:
        """Get activity by resource name."""
        clean_name = name.strip()
        if clean_name.startswith("sessions/"):
            path = f"/v1alpha/{clean_name}"
        else:
            path = f"/v1alpha/{clean_name.lstrip('/')}"

        outcome = self._execute_get_with_retry(path)
        data = self._parse_json_dict(outcome.body, path)
        return ActivityRecord.from_dict(data)

    # =================================================================
    # Multi-Page Paginators
    # =================================================================

    def paginate_sources(
        self,
        max_pages: int = 100,
        page_size: int = 100,
    ) -> tuple[tuple[SourceRecord, ...], Coverage]:
        """Paginate all sources up to max_pages."""
        return self._paginate(
            fetcher=lambda tok: self.sources_list(page_token=tok, page_size=page_size),
            max_pages=max_pages,
        )

    def paginate_sessions(
        self,
        max_pages: int = 100,
        page_size: int = 100,
    ) -> tuple[tuple[SessionRecord, ...], Coverage]:
        """Paginate all sessions up to max_pages."""
        return self._paginate(
            fetcher=lambda tok: self.sessions_list(page_token=tok, page_size=page_size),
            max_pages=max_pages,
        )

    def paginate_activities(
        self,
        session_name: str,
        max_pages: int = 100,
        page_size: int = 100,
        create_time_filter: str | None = None,
    ) -> tuple[tuple[ActivityRecord, ...], Coverage]:
        """Paginate all activities for a session up to max_pages."""
        return self._paginate(
            fetcher=lambda tok: self.activities_list(
                session_name=session_name,
                page_token=tok,
                page_size=page_size,
                create_time_filter=create_time_filter,
            ),
            max_pages=max_pages,
        )

    def _paginate(
        self,
        fetcher: Callable[[str | None], tuple[tuple[Any, ...], str | None]],
        max_pages: int,
    ) -> tuple[tuple[Any, ...], Coverage]:
        """Generic pagination engine.

        Follows empty pages with continuation, detects token cycles, rejects
        conflicting duplicate identities, and respects page caps with partial Coverage.
        """
        seen_tokens: set[str] = set()
        seen_identities: dict[str, Any] = {}
        all_items: list[Any] = []
        pages_count = 0
        current_token: str | None = None

        while True:
            if pages_count >= max_pages:
                # Page cap hit: return partial coverage without raising
                return tuple(all_items), Coverage(
                    complete=False,
                    pages=pages_count,
                    items=len(all_items),
                    skipped_scope=(),
                    reasons=("page_cap_reached",),
                    resume_ref=current_token,
                )

            if current_token is not None:
                if current_token in seen_tokens:
                    raise OctodotError(
                        ErrorCode.MALFORMED_RESPONSE,
                        f"Page token cycle detected: '{current_token}'",
                    )
                seen_tokens.add(current_token)

            items, next_token = fetcher(current_token)
            pages_count += 1

            for item in items:
                item_name = getattr(item, "name", None)
                if item_name is not None:
                    if item_name in seen_identities:
                        prev = seen_identities[item_name]
                        if prev != item:
                            raise OctodotError(
                                ErrorCode.IDENTITY_AMBIGUOUS,
                                f"Conflicting duplicate identity for resource '{item_name}'",
                            )
                    else:
                        seen_identities[item_name] = item
                        all_items.append(item)
                else:
                    all_items.append(item)

            if not next_token:
                # Finished pagination
                return tuple(all_items), Coverage(
                    complete=True,
                    pages=pages_count,
                    items=len(all_items),
                )

            current_token = next_token

    # =================================================================
    # Mutation Methods (Ticket-gated, single-attempt, no retries)
    # =================================================================

    def sessions_create(
        self,
        ticket: DispatchTicket,
        body: dict[str, Any],
    ) -> MutationResponse:
        """Internal mutation: create task session; requires DispatchTicket.

        Never retries; never raises for transport-level failures.
        Raises only for local pre-dispatch validation failures (zero attempts).
        """
        if not isinstance(ticket, DispatchTicket):
            raise OctodotError(ErrorCode.INVALID_INPUT, "ticket must be a DispatchTicket")
        if not isinstance(body, dict):
            raise OctodotError(ErrorCode.INVALID_INPUT, "body must be a dictionary")
        if self.ticket_authority is None:
            raise OctodotError(ErrorCode.GRANT_INVALID, "TicketAuthority is required for mutations")

        # Compute request hashes (support both canonical path and short name)
        primary_target = "/v1alpha/sessions"
        alt_target = "sessions"
        req_hash_1 = compute_mutation_request_hash(primary_target, body)
        req_hash_2 = compute_mutation_request_hash(alt_target, body)

        redeemed = self.ticket_authority.redeem(ticket, req_hash_1)
        if not redeemed:
            redeemed = self.ticket_authority.redeem(ticket, req_hash_2)

        if not redeemed:
            raise OctodotError(ErrorCode.GRANT_INVALID, "Dispatch ticket redemption failed")

        # Exactly one transport attempt
        raw_body = canonical_bytes(body)
        outcome = self.transport.request(
            "POST",
            primary_target,
            body=raw_body,
            headers={"Content-Type": "application/json"},
        )

        return self._process_mutation_outcome(outcome, expect_session=True)

    def sessions_send_message(
        self,
        ticket: DispatchTicket,
        session_name: str,
        body: dict[str, Any],
    ) -> MutationResponse:
        """Internal mutation: send reply message; requires DispatchTicket.

        Body must be {"prompt": <exact text>}.
        Never retries; never raises for transport-level failures.
        """
        if not isinstance(ticket, DispatchTicket):
            raise OctodotError(ErrorCode.INVALID_INPUT, "ticket must be a DispatchTicket")
        if not isinstance(body, dict):
            raise OctodotError(ErrorCode.INVALID_INPUT, "body must be a dictionary")
        if not session_name or not session_name.strip():
            raise OctodotError(ErrorCode.INVALID_INPUT, "session_name must be a non-empty string")
        if self.ticket_authority is None:
            raise OctodotError(ErrorCode.GRANT_INVALID, "TicketAuthority is required for mutations")

        # Normalize message body to {"prompt": text}
        prompt_val = body.get("prompt")
        if prompt_val is None:
            prompt_val = body.get("text")
        if not isinstance(prompt_val, str):
            raise OctodotError(ErrorCode.INVALID_INPUT, "sendMessage body must contain string 'prompt'")

        outgoing_body = {"prompt": prompt_val}

        clean_sess = session_name.strip()
        if not clean_sess.startswith("sessions/"):
            clean_sess = f"sessions/{clean_sess}"

        primary_target = f"/v1alpha/{clean_sess}:sendMessage"
        alt_target = f"{clean_sess}:sendMessage"

        req_hash_1 = compute_mutation_request_hash(primary_target, outgoing_body)
        req_hash_2 = compute_mutation_request_hash(alt_target, outgoing_body)

        redeemed = self.ticket_authority.redeem(ticket, req_hash_1)
        if not redeemed:
            redeemed = self.ticket_authority.redeem(ticket, req_hash_2)

        if not redeemed:
            raise OctodotError(ErrorCode.GRANT_INVALID, "Dispatch ticket redemption failed")

        # Exactly one transport attempt
        raw_body = canonical_bytes(outgoing_body)
        outcome = self.transport.request(
            "POST",
            primary_target,
            body=raw_body,
            headers={"Content-Type": "application/json"},
        )

        return self._process_mutation_outcome(outcome, expect_session=False)

    def sessions_approve_plan(
        self,
        ticket: DispatchTicket,
        session_name: str,
    ) -> MutationResponse:
        """Internal mutation: approve plan; requires DispatchTicket.

        Body is empty {}.
        Never retries; never raises for transport-level failures.
        """
        if not isinstance(ticket, DispatchTicket):
            raise OctodotError(ErrorCode.INVALID_INPUT, "ticket must be a DispatchTicket")
        if not session_name or not session_name.strip():
            raise OctodotError(ErrorCode.INVALID_INPUT, "session_name must be a non-empty string")
        if self.ticket_authority is None:
            raise OctodotError(ErrorCode.GRANT_INVALID, "TicketAuthority is required for mutations")

        outgoing_body: dict[str, Any] = {}

        clean_sess = session_name.strip()
        if not clean_sess.startswith("sessions/"):
            clean_sess = f"sessions/{clean_sess}"

        primary_target = f"/v1alpha/{clean_sess}:approvePlan"
        alt_target = f"{clean_sess}:approvePlan"

        req_hash_1 = compute_mutation_request_hash(primary_target, outgoing_body)
        req_hash_2 = compute_mutation_request_hash(alt_target, outgoing_body)

        redeemed = self.ticket_authority.redeem(ticket, req_hash_1)
        if not redeemed:
            redeemed = self.ticket_authority.redeem(ticket, req_hash_2)

        if not redeemed:
            raise OctodotError(ErrorCode.GRANT_INVALID, "Dispatch ticket redemption failed")

        # Exactly one transport attempt
        raw_body = canonical_bytes(outgoing_body)
        outcome = self.transport.request(
            "POST",
            primary_target,
            body=raw_body,
            headers={"Content-Type": "application/json"},
        )

        return self._process_mutation_outcome(outcome, expect_session=False)

    # =================================================================
    # Mutation outcome post-processing
    # =================================================================

    def _process_mutation_outcome(
        self,
        outcome: TransportOutcome,
        expect_session: bool,
    ) -> MutationResponse:
        """Map TransportOutcome to MutationResponse per the frozen mutation contract."""
        # 1. Clear 4xx client errors (rejected, uncertain_effect=False)
        if 400 <= outcome.status < 500:
            sanitized_outcome = TransportOutcome(
                status=outcome.status,
                body=outcome.body,
                request_count=outcome.request_count,
                byte_count=outcome.byte_count,
                uncertain_effect=False,
                sanitized_error_code=outcome.sanitized_error_code or ErrorCode.INVALID_INPUT,
                retry_after=outcome.retry_after,
            )
            return MutationResponse(outcome=sanitized_outcome, session=None)

        # 2. Server errors 5xx, timeouts, disconnects (uncertain_effect=True)
        if outcome.status >= 500 or outcome.status == 0 or outcome.uncertain_effect:
            sanitized_outcome = TransportOutcome(
                status=outcome.status,
                body=outcome.body,
                request_count=outcome.request_count,
                byte_count=outcome.byte_count,
                uncertain_effect=True,
                sanitized_error_code=outcome.sanitized_error_code or ErrorCode.TRANSPORT_ERROR,
                retry_after=outcome.retry_after,
            )
            return MutationResponse(outcome=sanitized_outcome, session=None)

        # 3. Success (200 / 201)
        if outcome.status in (200, 201):
            if outcome.body is None:
                # Empty body
                if expect_session:
                    # Expected session record but got empty body -> malformed success
                    sanitized_outcome = TransportOutcome(
                        status=outcome.status,
                        body=None,
                        request_count=outcome.request_count,
                        byte_count=outcome.byte_count,
                        uncertain_effect=True,
                        sanitized_error_code=ErrorCode.MALFORMED_RESPONSE,
                    )
                    return MutationResponse(outcome=sanitized_outcome, session=None)
                return MutationResponse(outcome=outcome, session=None)

            # Check for empty body when not expecting session
            if not outcome.body.strip() and not expect_session:
                return MutationResponse(outcome=outcome, session=None)

            try:
                parsed = load_strict_json(outcome.body, max_bytes=MAX_RESPONSE_BYTES)
            except Exception:
                # Malformed or non-strict JSON (duplicate keys, NaN/Infinity, etc.)
                # in mutation success => uncertain_effect=True (unknown), never retried
                sanitized_outcome = TransportOutcome(
                    status=outcome.status,
                    body=outcome.body,
                    request_count=outcome.request_count,
                    byte_count=outcome.byte_count,
                    uncertain_effect=True,
                    sanitized_error_code=ErrorCode.MALFORMED_RESPONSE,
                )
                return MutationResponse(outcome=sanitized_outcome, session=None)

            if expect_session:
                if "name" not in parsed:
                    # Missing session name -> malformed success
                    sanitized_outcome = TransportOutcome(
                        status=outcome.status,
                        body=outcome.body,
                        request_count=outcome.request_count,
                        byte_count=outcome.byte_count,
                        uncertain_effect=True,
                        sanitized_error_code=ErrorCode.MALFORMED_RESPONSE,
                    )
                    return MutationResponse(outcome=sanitized_outcome, session=None)
                sess = SessionRecord.from_dict(parsed)
                return MutationResponse(outcome=outcome, session=sess)

            return MutationResponse(outcome=outcome, session=None)

        # Other non-2xx status
        sanitized_outcome = TransportOutcome(
            status=outcome.status,
            body=outcome.body,
            request_count=outcome.request_count,
            byte_count=outcome.byte_count,
            uncertain_effect=True,
            sanitized_error_code=outcome.sanitized_error_code or ErrorCode.TRANSPORT_ERROR,
            retry_after=outcome.retry_after,
        )
        return MutationResponse(outcome=sanitized_outcome, session=None)

    # =================================================================
    # JSON Parsing Helper
    # =================================================================

    def _parse_json_dict(self, body: bytes | None, path: str) -> dict[str, Any]:
        """Parse strict JSON dictionary from body bytes using contracts.load_strict_json."""
        if body is None:
            raise OctodotError(ErrorCode.MALFORMED_RESPONSE, f"Empty response body from '{path}'")
        try:
            return load_strict_json(body, max_bytes=MAX_RESPONSE_BYTES)
        except OctodotError as err:
            raise OctodotError(
                ErrorCode.MALFORMED_RESPONSE,
                f"Malformed or non-strict JSON response from '{path}': {err.message}",
            ) from err
        except Exception as err:
            raise OctodotError(
                ErrorCode.MALFORMED_RESPONSE,
                f"Malformed JSON response from '{path}': {err}",
            ) from err
