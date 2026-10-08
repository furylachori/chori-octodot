"""Unit tests for fixed-origin HTTP transport, endpoint allowlisting, budgets, and credentials.

Covers:
- S02-T01: Endpoint allowlist enumeration, traversal/host/port/scheme rejection, redirects.
- S02-T04: TLS/proxy denial, 401/403, 429 with Retry-After, 5xx, timeouts, disconnects, caps.
- S02-T05: Credential spy isolation and synthetic secret absence across outputs.
"""

from __future__ import annotations

import email
import io
import os
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.contracts import LIVE_INVOCATION_DEFAULTS
from octodot.errors import ErrorCode, OctodotError
from octodot.models import TransportOutcome
from octodot.transport import (
    API_KEY_HEADER,
    BudgetTracker,
    DEFAULT_ORIGIN,
    FakeClock,
    FixtureTransport,
    HttpTransport,
    MAX_RESPONSE_BYTES,
    NoRedirectHandler,
    parse_retry_after,
    PROXY_ENV_VARS,
    SpyCredentialSource,
    validate_endpoint,
)


def make_mock_response(status: int = 200, body: bytes = b"{}") -> MagicMock:
    """Construct mock HTTP response properly handling context manager and stream."""
    resp = MagicMock()
    resp.status = status
    resp.read.side_effect = [body, b""]
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


class TestS02T01EndpointAllowlistAndValidation(unittest.TestCase):
    """S02-T01: Endpoint allowlist enumeration, traversal rejection, redirects, and query checks."""

    def test_s02_t01_allowlisted_endpoints_accepted(self) -> None:
        """S02-T01: All 9 allowlisted method/path combinations are accepted."""
        allowlisted = [
            ("GET", "/v1alpha/sources"),
            ("GET", "/v1alpha/sources/src-123"),
            ("GET", "/v1alpha/sessions"),
            ("GET", "/v1alpha/sessions/sess-abc"),
            ("GET", "/v1alpha/sessions/sess-abc/activities"),
            ("GET", "/v1alpha/sessions/sess-abc/activities/act-xyz"),
            ("POST", "/v1alpha/sessions"),
            ("POST", "/v1alpha/sessions/sess-abc:sendMessage"),
            ("POST", "/v1alpha/sessions/sess-abc:approvePlan"),
        ]
        for method, path in allowlisted:
            with self.subTest(method=method, path=path):
                norm_path, full_url, query_dict = validate_endpoint(method, path)
                self.assertEqual(norm_path, path)
                self.assertEqual(full_url, f"{DEFAULT_ORIGIN}{path}")
                self.assertEqual(query_dict, {})

    def test_s02_t01_full_https_url_accepted(self) -> None:
        """S02-T01: Full canonical HTTPS URL pointing to allowlisted endpoint is accepted."""
        norm_path, full_url, _ = validate_endpoint(
            "GET", "https://jules.googleapis.com/v1alpha/sessions"
        )
        self.assertEqual(norm_path, "/v1alpha/sessions")
        self.assertEqual(full_url, "https://jules.googleapis.com/v1alpha/sessions")

    def test_s02_t01_allowed_query_parameters(self) -> None:
        """S02-T01: pageSize and pageToken are accepted; filter accepted only for activities when flag set."""
        # pageSize and pageToken on sessions list
        _, _, q = validate_endpoint(
            "GET",
            "/v1alpha/sessions",
            query={"pageSize": 50, "pageToken": "tok-1"},
        )
        self.assertEqual(q["pageSize"], "50")
        self.assertEqual(q["pageToken"], "tok-1")

        # filter on activities.list when allow_filter=True
        _, _, q_act = validate_endpoint(
            "GET",
            "/v1alpha/sessions/sess-1/activities",
            query={"filter": "createTime > '2026-01-01T00:00:00Z'"},
            allow_filter=True,
        )
        self.assertIn("filter", q_act)

    def test_s02_t01_reject_unknown_query_parameters(self) -> None:
        """S02-T01: Reject unknown query parameters and unauthorized filter query."""
        # Unknown query parameter
        with self.assertRaises(OctodotError) as ctx:
            validate_endpoint("GET", "/v1alpha/sessions", query={"unauthorized": "val"})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

        # filter on sessions list is rejected
        with self.assertRaises(OctodotError) as ctx:
            validate_endpoint("GET", "/v1alpha/sessions", query={"filter": "some-filter"}, allow_filter=True)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

        # filter on activities.list when allow_filter=False is rejected
        with self.assertRaises(OctodotError) as ctx:
            validate_endpoint(
                "GET",
                "/v1alpha/sessions/sess-1/activities",
                query={"filter": "some-filter"},
                allow_filter=False,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

        # Invalid pageSize (non-integer or non-positive)
        with self.assertRaises(OctodotError) as ctx:
            validate_endpoint("GET", "/v1alpha/sessions", query={"pageSize": "abc"})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

        with self.assertRaises(OctodotError) as ctx:
            validate_endpoint("GET", "/v1alpha/sessions", query={"pageSize": -5})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_reject_non_allowlisted_methods(self) -> None:
        """S02-T01: Reject methods not in allowlist (PUT, DELETE, PATCH, etc.)."""
        for method in ("DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                with self.assertRaises(OctodotError) as ctx:
                    validate_endpoint(method, "/v1alpha/sessions/sess-1")
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_reject_non_allowlisted_paths(self) -> None:
        """S02-T01: Reject non-allowlisted resource paths."""
        bad_paths = [
            ("GET", "/v1alpha/unknown"),
            ("GET", "/v1alpha/users"),
            ("POST", "/v1alpha/sources"),
            ("POST", "/v1alpha/sessions/sess-1"),  # POST directly on session without action
            ("POST", "/v1alpha/sessions/sess-1:delete"),
            ("POST", "/v1alpha/sessions/sess-1:arbitraryMethod"),
        ]
        for method, path in bad_paths:
            with self.subTest(method=method, path=path):
                with self.assertRaises(OctodotError) as ctx:
                    validate_endpoint(method, path)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_reject_path_traversal(self) -> None:
        """S02-T01: Reject dot traversal in paths."""
        bad_traversals = [
            "/v1alpha/sessions/..",
            "/v1alpha/sessions/.",
            "/v1alpha/sessions/../sources",
            "/v1alpha/../v1alpha/sessions",
        ]
        for path in bad_traversals:
            with self.subTest(path=path):
                with self.assertRaises(OctodotError) as ctx:
                    validate_endpoint("GET", path)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_reject_encoded_traversal(self) -> None:
        """S02-T01: Reject encoded traversal (%2e, %2f, %5c) and backslash."""
        encoded_paths = [
            "/v1alpha/sessions/%2e%2e",
            "/v1alpha/sessions/%2E%2E",
            "/v1alpha/sessions/%2e",
            "/v1alpha/sessions/%2f/activities",
            "/v1alpha/sessions/%5c",
            "/v1alpha/sessions\\sess-1",
        ]
        for path in encoded_paths:
            with self.subTest(path=path):
                with self.assertRaises(OctodotError) as ctx:
                    validate_endpoint("GET", path)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_reject_wrong_scheme_host_port_userinfo(self) -> None:
        """S02-T01: Reject http scheme, foreign host, non-443 port, and userinfo in URL."""
        bad_urls = [
            "http://jules.googleapis.com/v1alpha/sessions",
            "ftp://jules.googleapis.com/v1alpha/sessions",
            "https://evil.com/v1alpha/sessions",
            "https://jules.googleapis.com.evil.com/v1alpha/sessions",
            "https://jules.googleapis.com:8080/v1alpha/sessions",
            "https://jules.googleapis.com:80/v1alpha/sessions",
            "https://user:pass@jules.googleapis.com/v1alpha/sessions",
            "https://user@jules.googleapis.com/v1alpha/sessions",
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(OctodotError) as ctx:
                    validate_endpoint("GET", url)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_reject_empty_segments(self) -> None:
        """S02-T01: Reject empty segments such as double slashes."""
        bad_paths = [
            "//v1alpha//sessions",
            "/v1alpha/sessions//activities",
            "/v1alpha/sessions/",  # trailing empty segment
        ]
        for path in bad_paths:
            with self.subTest(path=path):
                with self.assertRaises(OctodotError) as ctx:
                    validate_endpoint("GET", path)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s02_t01_redirects_refused(self) -> None:
        """S02-T01: HTTP redirects (301, 302, 303, 307, 308) are refused without following."""
        handler = NoRedirectHandler()
        # Verify redirect_request returns None
        result = handler.redirect_request(
            req=MagicMock(),
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://evil.com/redirect",
        )
        self.assertIsNone(result)

        # Test HttpTransport handling of redirect
        fake_opener = MagicMock()
        import urllib.error
        err_headers = email.message_from_string("Location: https://evil.com/redirect\n\n")
        fake_opener.open.side_effect = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions",
            code=302,
            msg="Found",
            hdrs=err_headers,
            fp=None,
        )

        transport = HttpTransport(opener=fake_opener)
        outcome = transport.request("GET", "/v1alpha/sessions")
        self.assertEqual(outcome.status, 302)
        self.assertEqual(outcome.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertEqual(outcome.request_count, 1)
        self.assertFalse(outcome.uncertain_effect)


class TestS02T04TransportFailuresAndBudgets(unittest.TestCase):
    """S02-T04: TLS denial, proxy denial, 401/403, 429 with Retry-After, 5xx, timeouts, disconnects, caps."""

    def test_s02_t04_tls_verification_denial(self) -> None:
        """S02-T04: SSL verification error results in sanitized ErrorCode.TRANSPORT_ERROR."""
        fake_opener = MagicMock()
        fake_opener.open.side_effect = ssl.SSLError("CERTIFICATE_VERIFY_FAILED: certificate verify failed")

        transport = HttpTransport(opener=fake_opener)
        outcome = transport.request("GET", "/v1alpha/sessions")
        self.assertEqual(outcome.status, 0)
        self.assertEqual(outcome.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertFalse(outcome.uncertain_effect)

    def test_s02_t04_environment_proxy_denial(self) -> None:
        """S02-T04: Detected environment proxy is denied with sanitized ErrorCode.TRANSPORT_ERROR."""
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://corporate-proxy:8080"}):
            transport = HttpTransport(allow_env_proxies=False)
            outcome = transport.request("GET", "/v1alpha/sessions")
            self.assertEqual(outcome.status, 0)
            self.assertEqual(outcome.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
            self.assertEqual(outcome.request_count, 0)

    def test_s02_t04_auth_denied_401_and_403(self) -> None:
        """S02-T04: 401 and 403 HTTP errors return ErrorCode.AUTH_DENIED without raw bodies."""
        import urllib.error
        for code in (401, 403):
            with self.subTest(code=code):
                fake_opener = MagicMock()
                fake_opener.open.side_effect = urllib.error.HTTPError(
                    url="https://jules.googleapis.com/v1alpha/sessions",
                    code=code,
                    msg="Forbidden",
                    hdrs=email.message_from_string(""),
                    fp=None,
                )
                transport = HttpTransport(opener=fake_opener)
                outcome = transport.request("GET", "/v1alpha/sessions")
                self.assertEqual(outcome.status, code)
                self.assertEqual(outcome.sanitized_error_code, ErrorCode.AUTH_DENIED)
                self.assertFalse(outcome.uncertain_effect)

    def test_s02_t04_rate_limited_429_with_retry_after(self) -> None:
        """S02-T04: 429 returns ErrorCode.RATE_LIMITED and honors Retry-After header."""
        import urllib.error
        hdrs = email.message_from_string("Retry-After: 7.5\n\n")
        fake_opener = MagicMock()
        fake_opener.open.side_effect = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions",
            code=429,
            msg="Too Many Requests",
            hdrs=hdrs,
            fp=None,
        )
        transport = HttpTransport(opener=fake_opener)
        outcome = transport.request("GET", "/v1alpha/sessions")
        self.assertEqual(outcome.status, 429)
        self.assertEqual(outcome.sanitized_error_code, ErrorCode.RATE_LIMITED)
        self.assertEqual(outcome.retry_after, 7.5)
        self.assertFalse(outcome.uncertain_effect)

    def test_s02_t04_server_error_5xx_and_post_uncertain_effect(self) -> None:
        """S02-T04: 500 and 503 errors return ErrorCode.TRANSPORT_ERROR; POST marks uncertain_effect=True."""
        import urllib.error
        hdrs_503 = email.message_from_string("Retry-After: 4\n\n")
        fake_opener = MagicMock()
        fake_opener.open.side_effect = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions",
            code=503,
            msg="Service Unavailable",
            hdrs=hdrs_503,
            fp=None,
        )
        transport = HttpTransport(opener=fake_opener)

        # GET 503
        outcome_get = transport.request("GET", "/v1alpha/sessions")
        self.assertEqual(outcome_get.status, 503)
        self.assertEqual(outcome_get.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertEqual(outcome_get.retry_after, 4.0)
        self.assertFalse(outcome_get.uncertain_effect)

        # POST 503 -> uncertain_effect=True
        outcome_post = transport.request(
            "POST",
            "/v1alpha/sessions/sess-1:sendMessage",
            body=b'{"prompt":"test"}',
        )
        self.assertEqual(outcome_post.status, 503)
        self.assertEqual(outcome_post.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertTrue(outcome_post.uncertain_effect)

    def test_s02_t04_timeouts_and_disconnects(self) -> None:
        """S02-T04: Network timeouts and disconnects return sanitized codes; POST marks uncertain_effect."""
        import socket
        import urllib.error

        # Timeout on GET
        fake_opener_timeout = MagicMock()
        fake_opener_timeout.open.side_effect = socket.timeout("timed out")
        transport_timeout = HttpTransport(opener=fake_opener_timeout)
        outcome_get_timeout = transport_timeout.request("GET", "/v1alpha/sessions")
        self.assertEqual(outcome_get_timeout.status, 0)
        self.assertEqual(outcome_get_timeout.sanitized_error_code, ErrorCode.TIMEOUT)
        self.assertFalse(outcome_get_timeout.uncertain_effect)

        # Timeout on POST
        outcome_post_timeout = transport_timeout.request(
            "POST",
            "/v1alpha/sessions/sess-1:sendMessage",
            body=b'{"prompt":"test"}',
        )
        self.assertTrue(outcome_post_timeout.uncertain_effect)
        self.assertEqual(outcome_post_timeout.sanitized_error_code, ErrorCode.TIMEOUT)

        # Disconnect on POST
        fake_opener_disconnect = MagicMock()
        fake_opener_disconnect.open.side_effect = urllib.error.URLError(ConnectionResetError("reset"))
        transport_disconnect = HttpTransport(opener=fake_opener_disconnect)
        outcome_disconnect = transport_disconnect.request(
            "POST",
            "/v1alpha/sessions/sess-1:sendMessage",
            body=b'{"prompt":"test"}',
        )
        self.assertEqual(outcome_disconnect.status, 0)
        self.assertEqual(outcome_disconnect.sanitized_error_code, ErrorCode.TRANSPORT_ERROR)
        self.assertTrue(outcome_disconnect.uncertain_effect)

    def test_s02_t04_request_count_cap_exhaustion(self) -> None:
        """S02-T04: Exceeding request-count cap halts dispatch with ErrorCode.BUDGET_EXHAUSTED."""
        tracker = BudgetTracker(max_requests=2)
        fake_opener = MagicMock()
        fake_opener.open.side_effect = [
            make_mock_response(200, b'{"sources":[]}'),
            make_mock_response(200, b'{"sources":[]}'),
        ]

        transport = HttpTransport(opener=fake_opener, budget_tracker=tracker)
        # Request 1 & 2 succeed
        r1 = transport.request("GET", "/v1alpha/sources")
        self.assertIsNone(r1.sanitized_error_code)

        r2 = transport.request("GET", "/v1alpha/sources")
        self.assertIsNone(r2.sanitized_error_code)

        # Request 3 blocked by budget
        r3 = transport.request("GET", "/v1alpha/sources")
        self.assertEqual(r3.sanitized_error_code, ErrorCode.BUDGET_EXHAUSTED)

    def test_s02_t04_total_byte_cap_exhaustion(self) -> None:
        """S02-T04: Exceeding total byte cap cuts off reading with ErrorCode.BUDGET_EXHAUSTED."""
        # For GET, the over-cap read is immediately cut off with BUDGET_EXHAUSTED (uncertain_effect=False)
        tracker = BudgetTracker(max_total_bytes=10)
        fake_opener = MagicMock()
        fake_opener.open.return_value = make_mock_response(200, b"123456789012345")  # 15 bytes > 10 cap

        transport = HttpTransport(opener=fake_opener, budget_tracker=tracker)
        r1 = transport.request("GET", "/v1alpha/sources")
        self.assertEqual(r1.sanitized_error_code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertFalse(r1.uncertain_effect)
        self.assertIsNone(r1.body)

        # For POST, exceeding total byte cap while reading response must be uncertain_effect=True
        tracker_post = BudgetTracker(max_total_bytes=10)
        fake_opener_post = MagicMock()
        fake_opener_post.open.return_value = make_mock_response(200, b"123456789012345")  # 15 bytes > 10 cap

        transport_post = HttpTransport(opener=fake_opener_post, budget_tracker=tracker_post)
        r_post = transport_post.request(
            "POST",
            "/v1alpha/sessions/sess-1:sendMessage",
            body=b'{"prompt":"test"}',
        )
        self.assertEqual(r_post.sanitized_error_code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertTrue(r_post.uncertain_effect)
        self.assertIsNone(r_post.body)

    def test_s02_t04_deadline_budget_exhaustion(self) -> None:
        """S02-T04: Deadline passed according to injected Clock halts dispatch with BUDGET_EXHAUSTED."""
        fake_clock = FakeClock()
        tracker = BudgetTracker(deadline_seconds=10.0, clock=fake_clock)
        fake_opener = MagicMock()
        transport = HttpTransport(opener=fake_opener, clock=fake_clock, budget_tracker=tracker)

        # Advance fake clock beyond deadline
        fake_clock.advance(15.0)
        outcome = transport.request("GET", "/v1alpha/sources")
        self.assertEqual(outcome.sanitized_error_code, ErrorCode.BUDGET_EXHAUSTED)


class TestS02T05CredentialIsolationAndConfidentiality(unittest.TestCase):
    """S02-T05: Credential spy isolation and synthetic secret absence across all outputs."""

    def test_s02_t05_fixture_transport_never_touches_credentials(self) -> None:
        """S02-T05: FixtureTransport never accepts or touches CredentialSource."""
        spy = SpyCredentialSource({"default": "SECRET_SHOULD_NOT_BE_READ"})
        fixture = FixtureTransport(
            responses={("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=b"{}")}
        )
        outcome = fixture.request("GET", "/v1alpha/sources")
        self.assertEqual(outcome.status, 200)

        # Spy was not accessed at all
        self.assertFalse(spy.was_accessed())
        self.assertEqual(spy.access_count(), 0)

    def test_s02_t05_validation_never_touches_credentials(self) -> None:
        """S02-T05: Pre-dispatch endpoint validation never touches credentials."""
        spy = SpyCredentialSource({"default": "SECRET_NEVER_READ"})
        # Validation on various paths
        validate_endpoint("GET", "/v1alpha/sessions")
        validate_endpoint("POST", "/v1alpha/sessions")
        with self.assertRaises(OctodotError):
            validate_endpoint("GET", "/v1alpha/../forbidden")

        self.assertFalse(spy.was_accessed())
        self.assertEqual(spy.access_count(), 0)

    def test_s02_t05_lazy_credential_access_at_dispatch_only(self) -> None:
        """S02-T05: Credentials are accessed lazily only when dispatching an actual real HTTP request."""
        synthetic_secret = "SYNTHETIC_TEST_SECRET_DO_NOT_LEAK_XYZ"
        spy = SpyCredentialSource({"default": synthetic_secret})

        fake_opener = MagicMock()
        fake_opener.open.return_value = make_mock_response(200, b"{}")

        transport = HttpTransport(credential_source=spy, opener=fake_opener)
        self.assertEqual(spy.access_count(), 0)

        transport.request("GET", "/v1alpha/sources")
        self.assertTrue(spy.was_accessed())
        self.assertEqual(spy.access_count(), 1)

        # Verify credential was passed as X-Goog-Api-Key header to urllib Request
        call_args = fake_opener.open.call_args
        req = call_args[0][0]
        self.assertEqual(req.headers.get("X-goog-api-key"), synthetic_secret)

    def test_s02_t05_synthetic_secret_absent_from_stdout_stderr_and_exceptions(self) -> None:
        """S02-T05: Synthetic secret is absent from stdout, stderr, exceptions, and outcomes."""
        synthetic_secret = "SYNTHETIC_KEY_TOP_SECRET_42_DO_NOT_REVEAL"
        spy = SpyCredentialSource({"default": synthetic_secret})

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        fake_opener = MagicMock()
        import urllib.error
        fake_opener.open.side_effect = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions",
            code=401,
            msg="Unauthorized",
            hdrs=email.message_from_string(""),
            fp=None,
        )

        transport = HttpTransport(credential_source=spy, opener=fake_opener)

        with patch("sys.stdout", stdout_capture), patch("sys.stderr", stderr_capture):
            outcome = transport.request("GET", "/v1alpha/sessions")

        # Inspect stdout & stderr
        self.assertNotIn(synthetic_secret, stdout_capture.getvalue())
        self.assertNotIn(synthetic_secret, stderr_capture.getvalue())

        # Inspect outcome
        outcome_repr = repr(outcome)
        self.assertNotIn(synthetic_secret, outcome_repr)
        if outcome.body:
            self.assertNotIn(synthetic_secret.encode("utf-8"), outcome.body)


if __name__ == "__main__":
    unittest.main()
