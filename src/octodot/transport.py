"""Fixed-origin standard-library HTTP transport with TLS, proxy, and credential isolation.

Standard library only. Compatible with Python 3.10+.
Enforces fixed Jules API origin (https://jules.googleapis.com), strict path allowlist,
redirect refusal, environment proxy denial, response byte limits, and deadline budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import email.utils
import http.client
import os
import socket
import ssl
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from octodot.contracts import Clock, CredentialSource, LIVE_INVOCATION_DEFAULTS, Transport
from octodot.errors import ErrorCode, OctodotError
from octodot.models import TransportOutcome

# Fixed origin and API constants
DEFAULT_ORIGIN: str = "https://jules.googleapis.com"
ALLOWED_HOST: str = "jules.googleapis.com"
API_VERSION_PREFIX: str = "/v1alpha/"
API_KEY_HEADER: str = "X-Goog-Api-Key"

# Resource & budget defaults
MAX_RESPONSE_BYTES: int = 8 * 1024 * 1024  # 8 MiB (8,388,608 bytes)
DEFAULT_REQUEST_TIMEOUT: float = 20.0       # <= 20 seconds
DEFAULT_MAX_HTTP_REQUESTS: int = 120
DEFAULT_MAX_TOTAL_BYTES: int = 32 * 1024 * 1024  # 32 MiB (33,554,432 bytes)
DEFAULT_DEADLINE_SECONDS: float = 180.0

# Proxy environment variable names
PROXY_ENV_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


# =====================================================================
# Clocks
# =====================================================================

class SystemClock:
    """Standard system clock providing UTC now and standard sleep."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic in-memory fake clock for testing."""

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time is None:
            self._now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        else:
            self._now = initial_time
        self.sleep_calls: list[float] = []

    def now_utc(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        from datetime import timedelta
        self.sleep_calls.append(seconds)
        if seconds > 0:
            self._now = self._now + timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        if seconds > 0:
            self._now = self._now + timedelta(seconds=seconds)


# =====================================================================
# Credential Spy
# =====================================================================

class SpyCredentialSource:
    """Credential source with observable access tracking for isolation verification."""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self._credentials = dict(credentials or {})
        self._access_count = 0

    def get_credential(self, profile: str) -> str | None:
        self._access_count += 1
        return self._credentials.get(profile)

    def was_accessed(self) -> bool:
        return self._access_count > 0

    def access_count(self) -> int:
        return self._access_count


# =====================================================================
# Budget Tracking
# =====================================================================

class BudgetTracker:
    """Tracks request count, total byte count, and deadlines against invocation caps."""

    def __init__(
        self,
        max_requests: int = DEFAULT_MAX_HTTP_REQUESTS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.max_total_bytes = max_total_bytes
        self.deadline_seconds = deadline_seconds
        self.clock = clock or SystemClock()
        self._start_time = self.clock.now_utc()
        self.request_count = 0
        self.total_bytes = 0

    def is_deadline_exceeded(self) -> bool:
        elapsed = (self.clock.now_utc() - self._start_time).total_seconds()
        return elapsed >= self.deadline_seconds

    def remaining_deadline_seconds(self) -> float:
        elapsed = (self.clock.now_utc() - self._start_time).total_seconds()
        return max(0.0, self.deadline_seconds - elapsed)

    def check_budget_before_request(self) -> ErrorCode | None:
        if self.is_deadline_exceeded():
            return ErrorCode.BUDGET_EXHAUSTED
        if self.request_count >= self.max_requests:
            return ErrorCode.BUDGET_EXHAUSTED
        if self.total_bytes >= self.max_total_bytes:
            return ErrorCode.BUDGET_EXHAUSTED
        return None

    def record_response(self, bytes_received: int) -> None:
        self.request_count += 1
        self.total_bytes += bytes_received


# =====================================================================
# Endpoint and Path Validation
# =====================================================================

def parse_retry_after(header_value: str | None) -> float | None:
    """Parse HTTP Retry-After header into seconds (float) or None."""
    if not header_value or not header_value.strip():
        return None
    cleaned = header_value.strip()
    try:
        val = float(cleaned)
        return max(0.0, val)
    except ValueError:
        pass
    try:
        parsed_tuple = email.utils.parsedate_to_datetime(cleaned)
        now_dt = datetime.now(timezone.utc)
        diff = (parsed_tuple - now_dt).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


def validate_endpoint(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    allow_filter: bool = False,
) -> tuple[str, str, dict[str, str]]:
    """Validate method, path segments, and query parameters against fixed allowlist.

    Returns:
        tuple of (normalized_path, full_url, query_dict)

    Raises:
        OctodotError(ErrorCode.INVALID_INPUT): on any allowlist violation, traversal,
        bad characters, unknown queries, or invalid segments.
    """
    method_upper = method.upper()
    if method_upper not in ("GET", "POST"):
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"HTTP method '{method}' not permitted; only GET and POST are allowlisted",
        )

    # Reject traversal encodings and forbidden characters in raw path string
    path_lower = path.lower()
    if "%2e" in path_lower:
        raise OctodotError(ErrorCode.INVALID_INPUT, "Encoded path traversal (%2e) not permitted")
    if "%2f" in path_lower:
        raise OctodotError(ErrorCode.INVALID_INPUT, "Encoded slash (%2f) not permitted")
    if "%5c" in path_lower:
        raise OctodotError(ErrorCode.INVALID_INPUT, "Encoded backslash (%5c) not permitted")
    if "\\" in path:
        raise OctodotError(ErrorCode.INVALID_INPUT, "Backslash not permitted in path")
    if "@" in path:
        raise OctodotError(ErrorCode.INVALID_INPUT, "Userinfo ('@') not permitted in URL")

    parsed = urllib.parse.urlparse(path)

    # Validate scheme if present
    if parsed.scheme:
        if parsed.scheme.lower() != "https":
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Scheme '{parsed.scheme}' not permitted; only https is allowed",
            )

    # Validate netloc if present
    if parsed.netloc:
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise OctodotError(ErrorCode.INVALID_INPUT, "Userinfo not permitted in URL")
        if parsed.port not in (None, 443):
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Port '{parsed.port}' not permitted; only standard HTTPS port allowed",
            )
        hostname = (parsed.hostname or "").lower()
        if hostname != ALLOWED_HOST:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Host '{hostname}' not permitted; fixed origin is '{ALLOWED_HOST}'",
            )

    raw_path = parsed.path
    if not raw_path or not raw_path.strip():
        raise OctodotError(ErrorCode.INVALID_INPUT, "Path cannot be empty")

    # Normalize leading slash and split segments
    raw_path = "/" + raw_path.lstrip("/")
    segments = raw_path.split("/")
    segs = segments[1:]  # skip leading empty segment from first slash

    # Check for empty segments or traversal dots
    for seg in segs:
        if not seg:
            raise OctodotError(ErrorCode.INVALID_INPUT, "Empty path segments not permitted")
        if seg in (".", ".."):
            raise OctodotError(ErrorCode.INVALID_INPUT, "Path traversal ('.' or '..') not permitted")

    # Prefix normalization: segs must start with 'v1alpha'
    if segs[0] != "v1alpha":
        if segs[0] in ("sources", "sessions"):
            segs = ["v1alpha"] + segs
        else:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Invalid API version prefix '/{segs[0]}'; expected '/v1alpha/'",
            )

    api_segs = segs[1:]
    if not api_segs:
        raise OctodotError(ErrorCode.INVALID_INPUT, "Path must specify a resource under '/v1alpha/'")

    # Check method/path allowlist
    is_activities_list = False

    if method_upper == "GET":
        # Allowlisted GET endpoints:
        # 1: sources
        # 2: sources/{id}
        # 3: sessions
        # 4: sessions/{id}
        # 5: sessions/{id}/activities
        # 6: sessions/{id}/activities/{id}
        if api_segs == ["sources"]:
            pass
        elif len(api_segs) == 2 and api_segs[0] == "sources":
            pass
        elif api_segs == ["sessions"]:
            pass
        elif len(api_segs) == 2 and api_segs[0] == "sessions":
            pass
        elif len(api_segs) == 3 and api_segs[0] == "sessions" and api_segs[2] == "activities":
            is_activities_list = True
        elif len(api_segs) == 4 and api_segs[0] == "sessions" and api_segs[2] == "activities":
            pass
        else:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"GET endpoint '/{'/'.join(segs)}' is not in the allowlist",
            )

    elif method_upper == "POST":
        # Allowlisted POST endpoints:
        # 1: sessions (create)
        # 2: sessions/{id}:sendMessage
        # 3: sessions/{id}:approvePlan
        if api_segs == ["sessions"]:
            pass
        elif len(api_segs) == 2 and api_segs[0] == "sessions":
            sub = api_segs[1]
            if sub.endswith(":sendMessage"):
                sess_id = sub[:-len(":sendMessage")]
                if not sess_id or sess_id in (".", ".."):
                    raise OctodotError(ErrorCode.INVALID_INPUT, "Invalid session ID in sendMessage")
            elif sub.endswith(":approvePlan"):
                sess_id = sub[:-len(":approvePlan")]
                if not sess_id or sess_id in (".", ".."):
                    raise OctodotError(ErrorCode.INVALID_INPUT, "Invalid session ID in approvePlan")
            else:
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    f"POST action on '/{'/'.join(segs)}' is not in the allowlist",
                )
        else:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"POST endpoint '/{'/'.join(segs)}' is not in the allowlist",
            )

    normalized_path = "/" + "/".join(segs)

    # Query parameters validation
    parsed_query_dict: dict[str, str] = {}
    if parsed.query:
        for k, v_list in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items():
            if v_list:
                parsed_query_dict[k] = v_list[0]

    if query:
        for k, v in query.items():
            if v is not None:
                parsed_query_dict[k] = str(v)

    if parsed_query_dict:
        if is_activities_list:
            allowed_query_keys = {"pageSize", "pageToken", "filter"} if allow_filter else {"pageSize", "pageToken"}
        else:
            allowed_query_keys = {"pageSize", "pageToken"}

        unknown_keys = set(parsed_query_dict.keys()) - allowed_query_keys
        if unknown_keys:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                f"Unknown or unauthorized query parameter(s): {sorted(unknown_keys)}",
            )

        if "pageSize" in parsed_query_dict:
            ps_str = parsed_query_dict["pageSize"]
            try:
                ps_int = int(ps_str)
                if ps_int <= 0:
                    raise ValueError()
            except ValueError:
                raise OctodotError(
                    ErrorCode.INVALID_INPUT,
                    f"pageSize must be a positive integer, got '{ps_str}'",
                )

        if "pageToken" in parsed_query_dict:
            pt_str = parsed_query_dict["pageToken"]
            if not pt_str.strip():
                raise OctodotError(ErrorCode.INVALID_INPUT, "pageToken must be a non-empty string")

        if "filter" in parsed_query_dict:
            filt_str = parsed_query_dict["filter"]
            if not filt_str.strip():
                raise OctodotError(ErrorCode.INVALID_INPUT, "filter must be a non-empty string")

    full_url = f"{DEFAULT_ORIGIN}{normalized_path}"
    return normalized_path, full_url, parsed_query_dict


# =====================================================================
# Redirect Handler (Refuses Redirects)
# =====================================================================

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses all HTTP redirects without following."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        # Returning None causes urllib to raise HTTPError for the redirect status
        return None


# =====================================================================
# Real HTTP Transport (urllib based)
# =====================================================================

class HttpTransport:
    """Bounded, verifying HTTPS transport for the fixed Jules origin.

    - Refuses redirects (no redirect following).
    - Refuses environment proxies by default (sanitized proxy denial).
    - Verifies TLS certificates using system CA trust roots.
    - Bound by per-request timeout (<=20s), response byte cap (8 MiB),
      total byte cap (32 MiB), max request cap, and deadline budget.
    - Lazily requests credential from CredentialSource only when a real
      request is about to be sent over the wire.
    - Never leaks credentials, raw HTML bodies, or secrets in outcomes/exceptions.
    """

    def __init__(
        self,
        credential_source: CredentialSource | None = None,
        profile: str = "default",
        clock: Clock | None = None,
        budget_tracker: BudgetTracker | None = None,
        allow_env_proxies: bool = False,
        allow_filter: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.credential_source = credential_source
        self.profile = profile
        self.clock = clock or SystemClock()
        self.budget_tracker = budget_tracker or BudgetTracker(clock=self.clock)
        self.allow_env_proxies = allow_env_proxies
        self.allow_filter = allow_filter
        self.ssl_context = ssl_context or ssl.create_default_context()

        if opener is not None:
            self._opener = opener
        else:
            # Policy: Environment proxies are strictly ignored by installing an empty ProxyHandler
            # to prevent silent proxy traversal.
            handlers: list[urllib.request.BaseHandler] = [
                NoRedirectHandler(),
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=self.ssl_context),
            ]
            self._opener = urllib.request.build_opener(*handlers)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> TransportOutcome:
        """Execute request against fixed allowlisted origin."""
        method_upper = method.upper()

        # 1. Endpoint and URL validation (raises OctodotError on invalid input before attempt)
        normalized_path, full_url, query_dict = validate_endpoint(
            method_upper, path, query, allow_filter=self.allow_filter
        )

        # 2. Check environment proxies policy
        if not self.allow_env_proxies:
            for env_var in PROXY_ENV_VARS:
                if os.environ.get(env_var):
                    return TransportOutcome(
                        status=0,
                        request_count=0,
                        byte_count=0,
                        uncertain_effect=False,
                        sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                    )

        # 3. Check invocation budgets / deadlines
        budget_err = self.budget_tracker.check_budget_before_request()
        if budget_err is not None:
            return TransportOutcome(
                status=0,
                request_count=0,
                byte_count=0,
                uncertain_effect=False,
                sanitized_error_code=budget_err,
            )

        # 4. Determine request timeout (bounded by DEFAULT_REQUEST_TIMEOUT and remaining deadline)
        req_timeout = DEFAULT_REQUEST_TIMEOUT
        if timeout is not None:
            req_timeout = min(timeout, DEFAULT_REQUEST_TIMEOUT)
        remaining_deadline = self.budget_tracker.remaining_deadline_seconds()
        req_timeout = max(0.1, min(req_timeout, remaining_deadline))

        # 5. Build URL query string if present
        if query_dict:
            encoded_query = urllib.parse.urlencode(query_dict)
            target_url = f"{full_url}?{encoded_query}"
        else:
            target_url = full_url

        # 6. Lazy credential acquisition (only at the moment a real request is about to be sent)
        req_headers = dict(headers or {})
        if self.credential_source is not None:
            secret = self.credential_source.get_credential(self.profile)
            if secret:
                req_headers[API_KEY_HEADER] = secret

        # 7. Construct urllib Request
        req = urllib.request.Request(
            url=target_url,
            data=body,
            headers=req_headers,
            method=method_upper,
        )

        # 8. Execute HTTP request
        is_post = (method_upper == "POST")
        try:
            with self._opener.open(req, timeout=req_timeout) as resp:
                status_code = resp.status

                # Read response up to MAX_RESPONSE_BYTES
                chunks: list[bytes] = []
                bytes_read = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > MAX_RESPONSE_BYTES:
                        self.budget_tracker.record_response(bytes_read)
                        return TransportOutcome(
                            status=status_code,
                            body=None,
                            request_count=1,
                            byte_count=bytes_read,
                            uncertain_effect=is_post,
                            sanitized_error_code=ErrorCode.OVERSIZED_RESPONSE,
                        )
                    if self.budget_tracker.total_bytes + bytes_read > self.budget_tracker.max_total_bytes:
                        self.budget_tracker.record_response(bytes_read)
                        return TransportOutcome(
                            status=status_code,
                            body=None,
                            request_count=1,
                            byte_count=bytes_read,
                            uncertain_effect=is_post,
                            sanitized_error_code=ErrorCode.BUDGET_EXHAUSTED,
                        )
                    chunks.append(chunk)

                resp_body = b"".join(chunks)
                self.budget_tracker.record_response(len(resp_body))

                return TransportOutcome(
                    status=status_code,
                    body=resp_body,
                    request_count=1,
                    byte_count=len(resp_body),
                    uncertain_effect=False,
                    sanitized_error_code=None,
                )

        except urllib.error.HTTPError as err:
            self.budget_tracker.record_response(0)
            code = err.code
            err_hdrs = getattr(err, "headers", None) or getattr(err, "hdrs", None) or {}
            retry_header = err_hdrs.get("Retry-After") if hasattr(err_hdrs, "get") else None
            retry_after = parse_retry_after(retry_header)

            # Refused redirect (3xx)
            if code in (301, 302, 303, 307, 308):
                return TransportOutcome(
                    status=code,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=False,
                    sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                )

            # Auth denied (401, 403)
            if code in (401, 403):
                return TransportOutcome(
                    status=code,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=False,
                    sanitized_error_code=ErrorCode.AUTH_DENIED,
                )

            # Rate limited (429)
            if code == 429:
                return TransportOutcome(
                    status=429,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=False,
                    sanitized_error_code=ErrorCode.RATE_LIMITED,
                    retry_after=retry_after,
                )

            # Client error (4xx)
            if 400 <= code < 500:
                return TransportOutcome(
                    status=code,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=False,
                    sanitized_error_code=ErrorCode.INVALID_INPUT,
                )

            # Server error 503
            if code == 503:
                return TransportOutcome(
                    status=503,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=is_post,
                    sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                    retry_after=retry_after,
                )

            # Other Server errors (5xx)
            if code >= 500:
                return TransportOutcome(
                    status=code,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=is_post,
                    sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                )

            return TransportOutcome(
                status=code,
                body=None,
                request_count=1,
                byte_count=0,
                uncertain_effect=is_post,
                sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
            )

        except urllib.error.URLError as err:
            self.budget_tracker.record_response(0)
            reason = getattr(err, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return TransportOutcome(
                    status=0,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=is_post,
                    sanitized_error_code=ErrorCode.TIMEOUT,
                )
            if isinstance(reason, ssl.SSLError):
                return TransportOutcome(
                    status=0,
                    body=None,
                    request_count=1,
                    byte_count=0,
                    uncertain_effect=False,
                    sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
                )
            # Other disconnect / network errors
            return TransportOutcome(
                status=0,
                body=None,
                request_count=1,
                byte_count=0,
                uncertain_effect=is_post,
                sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
            )

        except (socket.timeout, TimeoutError):
            self.budget_tracker.record_response(0)
            return TransportOutcome(
                status=0,
                body=None,
                request_count=1,
                byte_count=0,
                uncertain_effect=is_post,
                sanitized_error_code=ErrorCode.TIMEOUT,
            )

        except ssl.SSLError:
            self.budget_tracker.record_response(0)
            return TransportOutcome(
                status=0,
                body=None,
                request_count=1,
                byte_count=0,
                uncertain_effect=False,
                sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
            )

        except http.client.IncompleteRead as err:
            self.budget_tracker.record_response(len(err.partial))
            return TransportOutcome(
                status=0,
                body=None,
                request_count=1,
                byte_count=len(err.partial),
                uncertain_effect=is_post,
                sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
            )

        except Exception:
            self.budget_tracker.record_response(0)
            return TransportOutcome(
                status=0,
                body=None,
                request_count=1,
                byte_count=0,
                uncertain_effect=is_post,
                sanitized_error_code=ErrorCode.TRANSPORT_ERROR,
            )


# =====================================================================
# Fixture Transport (Scripted Offline Fake)
# =====================================================================

class FixtureTransport:
    """Scripted fake transport for deterministic offline testing.

    Never accesses CredentialSource or any network sockets.
    Can be configured with fixed outcomes or sequence of outcomes per (method, path).
    Records all calls for assertion inspection.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, str], TransportOutcome | Sequence[TransportOutcome]] | None = None,
        handler: Callable[..., TransportOutcome] | None = None,
        allow_filter: bool = False,
    ) -> None:
        self._responses: dict[tuple[str, str], list[TransportOutcome]] = {}
        if responses:
            for k, v in responses.items():
                if isinstance(v, (list, tuple)):
                    self._responses[k] = list(v)
                else:
                    self._responses[k] = [v]
        self._handler = handler
        self.allow_filter = allow_filter
        self.calls: list[dict[str, Any]] = []

    def set_response(
        self,
        method: str,
        path: str,
        outcome: TransportOutcome | Sequence[TransportOutcome],
    ) -> None:
        key = (method.upper(), path)
        if isinstance(outcome, (list, tuple)):
            self._responses[key] = list(outcome)
        else:
            self._responses[key] = [outcome]

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> TransportOutcome:
        method_upper = method.upper()

        # Validate endpoint (still enforces allowlist and traversal rejection)
        normalized_path, full_url, query_dict = validate_endpoint(
            method_upper, path, query, allow_filter=self.allow_filter
        )

        call_record = {
            "method": method_upper,
            "path": normalized_path,
            "query": query_dict,
            "headers": dict(headers or {}),
            "body": body,
            "timeout": timeout,
        }
        self.calls.append(call_record)

        if self._handler is not None:
            return self._handler(method_upper, normalized_path, query=query_dict, headers=headers, body=body)

        key = (method_upper, normalized_path)
        outcomes = self._responses.get(key)
        if not outcomes:
            return TransportOutcome(
                status=404,
                body=b"{}",
                request_count=1,
                byte_count=2,
                uncertain_effect=False,
                sanitized_error_code=ErrorCode.INVALID_INPUT,
            )

        if len(outcomes) > 1:
            return outcomes.pop(0)
        return outcomes[0]
