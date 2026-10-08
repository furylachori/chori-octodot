#!/usr/bin/env python3
"""octodot - Stateless Jules REST API client.

Executable client implementing the Google Jules REST API contract.
"""

from __future__ import annotations

import argparse
import calendar
import email.utils
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

VERSION = "1.0.0"
BASE_URL = "https://jules.googleapis.com/v1alpha"
REDACTED_TEXT = "[REDACTED]"

OUTPUT_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
INTERRUPTED = False


def build_parser() -> argparse.ArgumentParser:
    """Build the specification-mandated ArgumentParser with allow_abbrev=False."""
    parser = argparse.ArgumentParser(
        prog="octodot",
        usage="octodot [-new] [-list-repos] [-list-sessions] [-status SESSION] "
              "[-activities SESSION] [-results SESSION] [-pull SESSION] [-teleport SESSION] [options]",
        description="Stateless Jules REST API client.",
        allow_abbrev=False,
        add_help=False,
    )
    # Actions
    parser.add_argument("-new", "--new", action="store_true", help="Create new session(s)")
    parser.add_argument("-list-repos", "--list-repos", action="store_true", help="List accessible repositories")
    parser.add_argument("-list-sessions", "--list-sessions", action="store_true", help="List sessions")
    parser.add_argument("-status", "--status", dest="status", metavar="SESSION", help="Get session status")
    parser.add_argument("-activities", "--activities", dest="activities", metavar="SESSION", help="List session activities")
    parser.add_argument("-results", "--results", dest="results", metavar="SESSION", help="Get session results")
    parser.add_argument("-pull", "--pull", dest="pull", metavar="SESSION", help="Pull patch from session")
    parser.add_argument("-teleport", "--teleport", dest="teleport", metavar="SESSION", help="Clone and apply patch")

    # Options
    parser.add_argument("-prompt", "--prompt", dest="prompt", help="Prompt text or - for stdin")
    parser.add_argument("--repo", dest="repo", help="Repository OWNER/REPO or .")
    parser.add_argument("--branch", dest="branch", help="Starting branch name")
    parser.add_argument("--parallel", dest="parallel", type=int, default=1, help="Total parallel sessions (1-100)")
    parser.add_argument("--title", dest="title", help="Optional task title")
    parser.add_argument("--json", dest="json", action="store_true", help="Return JSON envelope for pull")
    parser.add_argument("--activity", dest="activity", help="Explicit activity resource selector")
    parser.add_argument("--artifact", dest="artifact", help="Artifact index in changeSets")
    parser.add_argument("--apply", dest="apply", action="store_true", help="Apply patch to repository")
    parser.add_argument("--cwd", dest="cwd", help="Working directory for git operations")
    parser.add_argument("--dir", dest="dir", help="Target directory for teleport")
    parser.add_argument("--timeout", dest="timeout", default=30.0, help="Per-request timeout in seconds (default: 30)")
    parser.add_argument("--deadline", dest="deadline", default=120.0, help="Total operation deadline in seconds (default: 120)")
    parser.add_argument("-h", "--help", action="store_true", help="Show help message and exit")
    parser.add_argument("--version", action="store_true", help="Show program version and exit")
    return parser

VERSION = "1.0.0"
BASE_URL = "https://jules.googleapis.com/v1alpha"
REDACTED_TEXT = "[REDACTED]"

OUTPUT_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
INTERRUPTED = False


class OctodotError(Exception):
    """Exception carrying a sanitized error record and process exit code."""

    def __init__(self, record: dict[str, Any], exit_code: int = 4):
        super().__init__(record.get("message", "Error"))
        self.record = record
        self.exit_code = exit_code


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that prevents all HTTP redirects."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _signal_handler(signum: int, frame: Any) -> None:
    global INTERRUPTED
    INTERRUPTED = True
    STOP_EVENT.set()


def install_signals() -> None:
    """Install signal handlers for graceful shutdown."""
    try:
        signal.signal(signal.SIGINT, _signal_handler)
    except (ValueError, AttributeError):
        pass
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass


def redact(val: Any, key: str | None) -> Any:
    """Recursively redact occurrences of key from strings and data structures."""
    if not key:
        return val
    if isinstance(val, str):
        return val.replace(key, REDACTED_TEXT)
    if isinstance(val, dict):
        return {redact(k, key): redact(v, key) for k, v in val.items()}
    if isinstance(val, list):
        return [redact(item, key) for item in val]
    return val


def emit_json(stream: Any, payload: Any, key: str | None = None) -> None:
    """Serialize and flush a JSON payload with atomic whole-line locking."""
    sanitized = redact(payload, key)
    text = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with OUTPUT_LOCK:
        stream.write(text)
        stream.flush()


def error_record(
    kind: str,
    message: str,
    operation: str,
    http_status: int | None = None,
    provider: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Build a sanitized error record matching the standardized error shape."""
    sanitized_provider: dict[str, Any] | None = None
    if isinstance(provider, dict):
        code = provider.get("code")
        status = provider.get("status")
        prov_msg = provider.get("message")
        code_int = code if isinstance(code, int) else None
        status_str = status if isinstance(status, str) else None
        msg_str = prov_msg if isinstance(prov_msg, str) else None
        if msg_str is not None:
            if key:
                msg_str = msg_str.replace(key, REDACTED_TEXT)
            if len(msg_str) > 2048:
                msg_str = msg_str[:2048]
        sanitized_provider = {
            "code": code_int,
            "message": msg_str,
            "status": status_str,
        }
    return {
        "httpStatus": http_status,
        "kind": kind,
        "message": message,
        "operation": operation,
        "provider": sanitized_provider,
    }


def parse_rfc3339_nanoseconds(ts_str: str) -> int | None:
    """Parse RFC3339 timestamp to integer nanoseconds since UTC epoch."""
    if not isinstance(ts_str, str):
        return None
    pattern = r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:([Zz])|([+-])(\d{2}):(\d{2}))?$"
    m = re.match(pattern, ts_str)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
    frac_str = m.group(7)
    z_indicator = m.group(8)
    tz_sign = m.group(9)
    tz_h = m.group(10)
    tz_m = m.group(11)

    if frac_str:
        nanos = int(frac_str.ljust(9, "0")[:9])
    else:
        nanos = 0

    try:
        epoch_secs = calendar.timegm((year, month, day, hour, minute, second))
    except (ValueError, OverflowError):
        return None

    offset_nanos = 0
    if tz_sign:
        off_secs = int(tz_h) * 3600 + int(tz_m) * 60
        if tz_sign == "-":
            offset_nanos = -off_secs * 1_000_000_000
        else:
            offset_nanos = off_secs * 1_000_000_000
    elif not z_indicator:
        # RFC3339 requires timezone specification
        return None

    return epoch_secs * 1_000_000_000 + nanos - offset_nanos


def safe_quote_resource_name(res_name: str) -> str:
    """Validate resource name shape and URL-quote each slash segment."""
    segments = res_name.split("/")
    for seg in segments:
        if not seg or seg in (".", "..") or "\\" in seg or "?" in seg or "#" in seg:
            raise OctodotError(
                error_record("protocol_error", f"Invalid resource name: {res_name}", "url_quote"),
                exit_code=4,
            )
        if any(ord(c) < 32 or ord(c) == 127 for c in seg):
            raise OctodotError(
                error_record("protocol_error", f"Invalid resource name: {res_name}", "url_quote"),
                exit_code=4,
            )
    return "/".join(urllib.parse.quote(seg, safe="") for seg in segments)


def validate_session_name(raw_name: str) -> str:
    """Normalize and validate a session input to sessions/{suffix}."""
    if not raw_name or not isinstance(raw_name, str):
        raise OctodotError(
            error_record("usage_error", "Session identifier cannot be empty", "arg_parse"),
            exit_code=2,
        )
    suffix = raw_name
    if suffix.startswith("sessions/"):
        suffix = suffix[len("sessions/") :]
    if not re.match(r"^[A-Za-z0-9_-]+$", suffix):
        raise OctodotError(
            error_record("usage_error", f"Invalid session format: {raw_name}", "arg_parse"),
            exit_code=2,
        )
    return f"sessions/{suffix}"


def validate_activity_name(raw_name: str, expected_session: str) -> str:
    """Validate activity selector matching sessions/S/activities/A."""
    if not raw_name or not isinstance(raw_name, str):
        raise OctodotError(
            error_record("usage_error", "Activity identifier cannot be empty", "arg_parse"),
            exit_code=2,
        )
    m = re.match(r"^(sessions/[A-Za-z0-9_-]+)/activities/([A-Za-z0-9_-]+)$", raw_name)
    if not m:
        raise OctodotError(
            error_record("usage_error", f"Invalid activity format: {raw_name}", "arg_parse"),
            exit_code=2,
        )
    session_part = m.group(1)
    if session_part != expected_session:
        raise OctodotError(
            error_record(
                "usage_error",
                f"Activity session {session_part} does not match target session {expected_session}",
                "arg_parse",
            ),
            exit_code=2,
        )
    return raw_name


def validate_repo_arg(repo_str: str) -> tuple[str, str] | None:
    """Validate explicit repo argument. Returns (owner, repo) or None if '.'."""
    if repo_str == ".":
        return None
    parts = repo_str.split("/")
    if len(parts) != 2:
        raise OctodotError(
            error_record("usage_error", f"Repository must be OWNER/REPO or .: {repo_str}", "arg_parse"),
            exit_code=2,
        )
    owner, repo = parts
    if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or owner in (".", ".."):
        raise OctodotError(
            error_record("usage_error", f"Invalid owner format: {owner}", "arg_parse"),
            exit_code=2,
        )
    if not re.match(r"^[A-Za-z0-9_.-]+$", repo) or repo in (".", ".."):
        raise OctodotError(
            error_record("usage_error", f"Invalid repo format: {repo}", "arg_parse"),
            exit_code=2,
        )
    return owner, repo


def parse_args(argv: list[str]) -> dict[str, Any]:
    """Strictly parse command-line arguments according to the specification."""
    parser = build_parser()
    if not argv:
        raise OctodotError(
            error_record("usage_error", "Action required", "arg_parse"),
            exit_code=2,
        )

    # Check for standalone help/version
    if argv in (["-h"], ["--help"]):
        return {"action": "help"}
    if argv == ["--version"]:
        return {"action": "version"}

    if any(arg in ("-h", "--help", "--version") for arg in argv):
        raise OctodotError(
            error_record("usage_error", "Help and version cannot be combined with other arguments", "arg_parse"),
            exit_code=2,
        )

    # Duplicate flag detection and unknown flag detection
    canonical_flags = {
        "-new": "new",
        "--new": "new",
        "-list-repos": "list-repos",
        "--list-repos": "list-repos",
        "-list-sessions": "list-sessions",
        "--list-sessions": "list-sessions",
        "-status": "status",
        "--status": "status",
        "-activities": "activities",
        "--activities": "activities",
        "-results": "results",
        "--results": "results",
        "-pull": "pull",
        "--pull": "pull",
        "-teleport": "teleport",
        "--teleport": "teleport",
        "-prompt": "prompt",
        "--prompt": "prompt",
        "--repo": "repo",
        "--branch": "branch",
        "--parallel": "parallel",
        "--title": "title",
        "--json": "json",
        "--activity": "activity",
        "--artifact": "artifact",
        "--apply": "apply",
        "--cwd": "cwd",
        "--dir": "dir",
        "--timeout": "timeout",
        "--deadline": "deadline",
    }

    seen_options: set[str] = set()
    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token.startswith("-") and token != "-":
            flag_name = token.split("=", 1)[0]
            canon = canonical_flags.get(flag_name)
            if not canon:
                raise OctodotError(
                    error_record("usage_error", f"Unrecognized option: {flag_name}", "arg_parse"),
                    exit_code=2,
                )
            if canon in seen_options:
                raise OctodotError(
                    error_record("usage_error", f"Duplicate option specified: {token}", "arg_parse"),
                    exit_code=2,
                )
            seen_options.add(canon)
        idx += 1

    actions_seen: list[tuple[str, str | None]] = []
    options: dict[str, Any] = {
        "prompt": None,
        "repo": None,
        "branch": None,
        "parallel": 1,
        "title": None,
        "json": False,
        "activity": None,
        "artifact": None,
        "apply": False,
        "cwd": None,
        "dir": None,
        "timeout": 30.0,
        "deadline": 120.0,
    }

    takes_arg = {
        "status",
        "activities",
        "results",
        "pull",
        "teleport",
        "prompt",
        "repo",
        "branch",
        "parallel",
        "title",
        "activity",
        "artifact",
        "cwd",
        "dir",
        "timeout",
        "deadline",
    }

    i = 0
    while i < len(argv):
        token = argv[i]
        val = None
        if token.startswith("-") and token != "-":
            if "=" in token:
                flag_name, val = token.split("=", 1)
            else:
                flag_name = token
            canon = canonical_flags[flag_name]
            if canon in (
                "new",
                "list-repos",
                "list-sessions",
                "status",
                "activities",
                "results",
                "pull",
                "teleport",
            ):
                if canon in ("status", "activities", "results", "pull", "teleport"):
                    if val is None:
                        if i + 1 >= len(argv) or (argv[i + 1].startswith("-") and argv[i + 1] != "-"):
                            raise OctodotError(
                                error_record("usage_error", f"Missing argument for {flag_name}", "arg_parse"),
                                exit_code=2,
                            )
                        val = argv[i + 1]
                        i += 1
                actions_seen.append((canon, val))
            elif canon == "json":
                options["json"] = True
            elif canon == "apply":
                options["apply"] = True
            elif canon in takes_arg:
                if val is None:
                    if i + 1 >= len(argv):
                        raise OctodotError(
                            error_record("usage_error", f"Missing argument for {flag_name}", "arg_parse"),
                            exit_code=2,
                        )
                    val = argv[i + 1]
                    i += 1
                options[canon] = val
        else:
            raise OctodotError(
                error_record("usage_error", f"Unexpected positional argument: {token}", "arg_parse"),
                exit_code=2,
            )
        i += 1

    if not actions_seen:
        raise OctodotError(
            error_record("usage_error", "No action specified", "arg_parse"),
            exit_code=2,
        )
    if len(actions_seen) > 1:
        raise OctodotError(
            error_record("usage_error", "Only one action may be specified", "arg_parse"),
            exit_code=2,
        )

    action, action_arg = actions_seen[0]

    # Validate option applicability
    if action != "new":
        for opt in ("prompt", "repo", "branch", "title"):
            if options[opt] is not None:
                raise OctodotError(
                    error_record("usage_error", f"Option --{opt} is only permitted for -new", "arg_parse"),
                    exit_code=2,
                )
        if "parallel" in seen_options:
            raise OctodotError(
                error_record("usage_error", "Option --parallel is only permitted for -new", "arg_parse"),
                exit_code=2,
            )

    if options["json"]:
        if action != "pull" or options["apply"]:
            raise OctodotError(
                error_record("usage_error", "--json is only permitted for -pull without --apply", "arg_parse"),
                exit_code=2,
            )

    if options["activity"] is not None or options["artifact"] is not None:
        if action not in ("pull", "teleport"):
            raise OctodotError(
                error_record(
                    "usage_error", "--activity and --artifact are only permitted for -pull and -teleport", "arg_parse"
                ),
                exit_code=2,
            )
        if options["activity"] is None or options["artifact"] is None:
            raise OctodotError(
                error_record("usage_error", "--activity and --artifact must be specified together", "arg_parse"),
                exit_code=2,
            )

    if options["apply"]:
        if action not in ("pull", "teleport"):
            raise OctodotError(
                error_record("usage_error", "--apply is only permitted for -pull and -teleport", "arg_parse"),
                exit_code=2,
            )

    if action == "teleport" and not options["apply"]:
        raise OctodotError(
            error_record("usage_error", "--apply is mandatory for -teleport", "arg_parse"),
            exit_code=2,
        )

    if options["dir"] is not None:
        if action != "teleport":
            raise OctodotError(
                error_record("usage_error", "--dir is only permitted for -teleport", "arg_parse"),
                exit_code=2,
            )
    elif action == "teleport":
        raise OctodotError(
            error_record("usage_error", "--dir is mandatory for -teleport", "arg_parse"),
            exit_code=2,
        )

    if options["cwd"] is not None:
        if action == "new":
            if options["repo"] not in (None, "."):
                raise OctodotError(
                    error_record("usage_error", "--cwd cannot be used with explicit OWNER/REPO", "arg_parse"),
                    exit_code=2,
                )
        elif action == "pull":
            if not options["apply"]:
                raise OctodotError(
                    error_record("usage_error", "pull --cwd requires --apply", "arg_parse"),
                    exit_code=2,
                )
        else:
            raise OctodotError(
                error_record("usage_error", "--cwd is only permitted for -new or -pull --apply", "arg_parse"),
                exit_code=2,
            )

    # Validate numbers
    def parse_float(name: str, val: Any) -> float:
        try:
            num = float(val)
        except (ValueError, TypeError):
            raise OctodotError(
                error_record("usage_error", f"{name} must be a valid number", "arg_parse"),
                exit_code=2,
            )
        if math.isnan(num) or math.isinf(num) or num <= 0:
            raise OctodotError(
                error_record("usage_error", f"{name} must be a finite positive number", "arg_parse"),
                exit_code=2,
            )
        return num

    options["timeout"] = parse_float("timeout", options["timeout"])
    options["deadline"] = parse_float("deadline", options["deadline"])

    if "parallel" in seen_options:
        try:
            par = int(options["parallel"])
            if str(par) != str(options["parallel"]):
                raise ValueError()
        except (ValueError, TypeError):
            raise OctodotError(
                error_record("usage_error", "--parallel must be an integer between 1 and 100", "arg_parse"),
                exit_code=2,
            )
        if par < 1 or par > 100:
            raise OctodotError(
                error_record("usage_error", "--parallel must be between 1 and 100", "arg_parse"),
                exit_code=2,
            )
        options["parallel"] = par

    if options["artifact"] is not None:
        try:
            art = int(options["artifact"])
            if str(art) != str(options["artifact"]) or art < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise OctodotError(
                error_record("usage_error", "--artifact must be a nonnegative integer", "arg_parse"),
                exit_code=2,
            )
        options["artifact"] = art

    # Action-specific validation
    normalized_session = None
    if action in ("status", "activities", "results", "pull", "teleport"):
        normalized_session = validate_session_name(action_arg)

    if options["activity"] is not None:
        options["activity"] = validate_activity_name(options["activity"], normalized_session)

    if action == "new":
        if options["title"] is not None:
            if not isinstance(options["title"], str) or not options["title"].strip():
                raise OctodotError(
                    error_record("usage_error", "--title cannot be empty or whitespace-only", "arg_parse"),
                    exit_code=2,
                )
        if options["branch"] is not None and not options["branch"]:
            raise OctodotError(
                error_record("usage_error", "--branch cannot be empty", "arg_parse"),
                exit_code=2,
            )
        if options["repo"] is not None:
            validate_repo_arg(options["repo"])

    return {
        "action": action,
        "session": normalized_session,
        **options,
    }


def resolve_prompt(prompt_opt: str | None) -> str:
    """Read and validate the prompt string according to the precedence rules."""
    if prompt_opt is not None and prompt_opt != "-":
        if not prompt_opt.strip():
            raise OctodotError(
                error_record("usage_error", "Prompt cannot be whitespace-only", "arg_parse"),
                exit_code=2,
            )
        return prompt_opt

    is_tty = False
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        is_tty = False

    if prompt_opt == "-":
        if is_tty:
            raise OctodotError(
                error_record("usage_error", "Interactive prompt on TTY not permitted for -prompt -", "arg_parse"),
                exit_code=2,
            )
        try:
            content = sys.stdin.read()
        except UnicodeDecodeError:
            raise OctodotError(
                error_record("usage_error", "Invalid UTF-8 in prompt stdin", "arg_parse"),
                exit_code=2,
            )
        if not content.strip():
            raise OctodotError(
                error_record("usage_error", "Prompt from stdin cannot be empty or whitespace-only", "arg_parse"),
                exit_code=2,
            )
        return content

    # Prompt was omitted
    if is_tty:
        raise OctodotError(
            error_record("usage_error", "Prompt is required on interactive TTY", "arg_parse"),
            exit_code=2,
        )
    try:
        content = sys.stdin.read()
    except UnicodeDecodeError:
        raise OctodotError(
            error_record("usage_error", "Invalid UTF-8 in prompt stdin", "arg_parse"),
            exit_code=2,
        )
    if not content.strip():
        raise OctodotError(
            error_record("usage_error", "Prompt from stdin cannot be empty or whitespace-only", "arg_parse"),
            exit_code=2,
        )
    return content


def run_git(
    args: list[str],
    cwd: str | None = None,
    input_bytes: bytes | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[bytes]:
    """Execute a Git command safely with clean environment and bounded timeout."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(var, None)

    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"] + args
    try:
        res = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
        return res
    except subprocess.TimeoutExpired:
        raise OctodotError(
            error_record("git_timeout", f"Git command timed out: {' '.join(args)}", "run_git"),
            exit_code=4,
        )
    except Exception as exc:
        raise OctodotError(
            error_record("git_error", f"Git execution error: {type(exc).__name__}", "run_git"),
            exit_code=4,
        )


def check_git_version() -> None:
    """Require Git >= 2.36 for local operations."""
    res = run_git(["--version"])
    if res.returncode != 0:
        raise OctodotError(
            error_record("git_error", "Failed to determine Git version", "git_version"),
            exit_code=3,
        )
    out = res.stdout.decode("utf-8", errors="replace")
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        raise OctodotError(
            error_record("git_error", f"Unrecognized Git version output: {out}", "git_version"),
            exit_code=3,
        )
    major, minor = int(m.group(1)), int(m.group(2))
    if (major, minor) < (2, 36):
        raise OctodotError(
            error_record(
                "unsupported_platform",
                f"Installed Git version {major}.{minor} is older than required 2.36",
                "git_version",
            ),
            exit_code=3,
        )


def check_git_config(cwd: str | None = None) -> None:
    """Reject url.* rewrites and filter.* command configurations."""
    res = run_git(["config", "--get-regexp", r"^url\..*\.(insteadOf|pushInsteadOf)$"], cwd=cwd)
    if res.returncode == 0 and res.stdout.strip():
        raise OctodotError(
            error_record(
                "unsupported_git_config",
                "Configured url insteadOf rewrites are forbidden",
                "git_config",
            ),
            exit_code=3,
        )
    res2 = run_git(["config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"], cwd=cwd)
    if res2.returncode == 0 and res2.stdout.strip():
        raise OctodotError(
            error_record(
                "unsupported_git_config",
                "Configured filter smudge/clean/process commands are forbidden",
                "git_config",
            ),
            exit_code=3,
        )


def parse_remote_url(raw_url: str) -> tuple[str, str] | None:
    """Parse owner/repo from supported GitHub remote URLs."""
    url = raw_url.strip()
    if url.endswith("/"):
        url = url[:-1]
    if url.endswith(".git"):
        url = url[:-4]

    # Patterns:
    # https://github.com/OWNER/REPO
    # git@github.com:OWNER/REPO
    # ssh://git@github.com/OWNER/REPO
    m = re.match(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def infer_repo(cwd: str, branch_arg: str | None) -> tuple[str, str, str]:
    """Infer repository owner/repo and branch using local Git."""
    res_root = run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if res_root.returncode != 0:
        raise OctodotError(
            error_record("git_error", "Not inside a valid Git repository", "infer_repo"),
            exit_code=2,
        )
    res_remotes = run_git(["remote", "get-url", "--all", "origin"], cwd=cwd)
    if res_remotes.returncode != 0:
        raise OctodotError(
            error_record("git_error", "Remote 'origin' not found", "infer_repo"),
            exit_code=2,
        )
    lines = [line.strip() for line in res_remotes.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if len(lines) != 1:
        raise OctodotError(
            error_record("git_error", "Expected exactly one origin fetch URL", "infer_repo"),
            exit_code=2,
        )
    parsed = parse_remote_url(lines[0])
    if not parsed:
        raise OctodotError(
            error_record("git_error", f"Unsupported origin URL format: {lines[0]}", "infer_repo"),
            exit_code=2,
        )
    owner, repo = parsed

    if branch_arg is not None:
        branch = branch_arg
    else:
        res_branch = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd)
        if res_branch.returncode != 0:
            raise OctodotError(
                error_record(
                    "usage_error",
                    "Detached HEAD detected; --branch must be explicitly specified",
                    "infer_repo",
                ),
                exit_code=2,
            )
        branch = res_branch.stdout.decode("utf-8", errors="replace").strip()
        if not branch:
            raise OctodotError(
                error_record("usage_error", "Could not infer current branch", "infer_repo"),
                exit_code=2,
            )
    return owner, repo, branch


def get_remaining_budget(start_time: float | None, deadline: float) -> float:
    """Return remaining seconds in invocation deadline."""
    if not start_time:
        return deadline
    return deadline - (time.monotonic() - start_time)


def request_json(
    method: str,
    path: str,
    key: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    deadline_start: float | None = None,
    deadline: float = 120.0,
    is_post: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Execute an HTTP request with redirect blocking, retry logic, and deadline checks."""
    if deadline_start is None:
        deadline_start = time.monotonic()
    url = f"{BASE_URL}{path}"
    headers = {
        "X-Goog-Api-Key": key,
        "Accept": "application/json",
    }
    encoded_body = None
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        encoded_body = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    opener = urllib.request.build_opener(NoRedirectHandler(), urllib.request.ProxyHandler())
    max_attempts = 1 if is_post else 3
    base_delays = [1.0, 2.0]

    for attempt in range(max_attempts):
        rem = get_remaining_budget(deadline_start, deadline)
        if rem <= 0:
            raise OctodotError(
                error_record("deadline_exceeded", "Invocation deadline exceeded", "http_request"),
                exit_code=4,
            )
        eff_timeout = min(timeout, rem)
        if eff_timeout <= 0:
            raise OctodotError(
                error_record("deadline_exceeded", "Invocation deadline exceeded", "http_request"),
                exit_code=4,
            )

        req = urllib.request.Request(url, data=encoded_body, headers=headers, method=method)
        try:
            with opener.open(req, timeout=eff_timeout) as resp:
                status_code = resp.status
                raw_bytes = resp.read()
                try:
                    data = json.loads(raw_bytes.decode("utf-8"))
                except Exception:
                    raise OctodotError(
                        error_record("protocol_error", "Malformed JSON in response body", "http_request", status_code),
                        exit_code=5 if is_post else 4,
                    )
                if not isinstance(data, dict):
                    raise OctodotError(
                        error_record("protocol_error", "Response root must be a JSON object", "http_request", status_code),
                        exit_code=5 if is_post else 4,
                    )
                return status_code, data

        except urllib.error.HTTPError as exc:
            status_code = exc.code
            raw_bytes = b""
            try:
                raw_bytes = exc.read()
            except Exception:
                pass

            provider_obj = None
            try:
                err_json = json.loads(raw_bytes.decode("utf-8"))
                if isinstance(err_json, dict) and isinstance(err_json.get("error"), dict):
                    provider_obj = err_json["error"]
            except Exception:
                pass

            # Auth failures are exit 3
            if status_code in (401, 403):
                kind = "auth_error" if status_code == 401 else "access_denied"
                raise OctodotError(
                    error_record(
                        kind,
                        f"HTTP {status_code} authentication/access gate failure",
                        "http_request",
                        status_code,
                        provider_obj,
                        key,
                    ),
                    exit_code=3,
                )

            # Check if retryable for GET
            is_retryable_get = (not is_post) and (
                status_code in (408, 429) or (500 <= status_code <= 599)
            )

            if is_retryable_get and attempt < max_attempts - 1:
                # Parse Retry-After
                delay = base_delays[attempt]
                retry_after_hdr = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after_hdr:
                    try:
                        parsed_secs = float(retry_after_hdr)
                        if parsed_secs >= 0:
                            delay = max(delay, parsed_secs)
                    except ValueError:
                        try:
                            dt = email.utils.parsedate_to_datetime(retry_after_hdr)
                            target_epoch = dt.timestamp()
                            diff = target_epoch - time.time()
                            if diff > 0:
                                delay = max(delay, diff)
                        except Exception:
                            pass

                if delay > get_remaining_budget(deadline_start, deadline):
                    raise OctodotError(
                        error_record("deadline_exceeded", "Retry delay exceeds remaining deadline", "http_request", status_code),
                        exit_code=4,
                    )
                time.sleep(delay)
                continue

            # Non-retryable or retries exhausted
            exit_code = 5 if is_post else 4
            kind = "http_error"
            if 300 <= status_code < 400:
                kind = "redirect_denied"
            elif status_code == 429:
                kind = "quota_exhausted"
            elif status_code == 404:
                kind = "not_found"

            raise OctodotError(
                error_record(kind, f"HTTP error {status_code}", "http_request", status_code, provider_obj, key),
                exit_code=exit_code,
            )

        except urllib.error.URLError as exc:
            reason = exc.reason
            # TLS failure check
            is_tls = "certificate" in str(reason).lower() or "ssl" in str(reason).lower()
            if is_post:
                raise OctodotError(
                    error_record("transport_error", "POST transport error", "http_request"),
                    exit_code=5,
                )
            if (not is_tls) and attempt < max_attempts - 1:
                delay = base_delays[attempt]
                if delay > get_remaining_budget(deadline_start, deadline):
                    raise OctodotError(
                        error_record("deadline_exceeded", "Retry delay exceeds remaining deadline", "http_request"),
                        exit_code=4,
                    )
                time.sleep(delay)
                continue
            raise OctodotError(
                error_record("transport_error", f"Network transport error: {type(exc).__name__}", "http_request"),
                exit_code=4,
            )

        except TimeoutError:
            if is_post:
                raise OctodotError(
                    error_record("timeout", "POST socket timeout", "http_request"),
                    exit_code=5,
                )
            if attempt < max_attempts - 1:
                delay = base_delays[attempt]
                if delay > get_remaining_budget(deadline_start, deadline):
                    raise OctodotError(
                        error_record("deadline_exceeded", "Retry delay exceeds remaining deadline", "http_request"),
                        exit_code=4,
                    )
                time.sleep(delay)
                continue
            raise OctodotError(
                error_record("timeout", "Socket timeout", "http_request"),
                exit_code=4,
            )

    raise OctodotError(
        error_record("retry_exhausted", "HTTP retries exhausted", "http_request"),
        exit_code=4,
    )


def paginate(
    endpoint: str,
    collection_key: str,
    key: str,
    timeout: float = 30.0,
    deadline_start: float | None = None,
    deadline: float = 120.0,
) -> tuple[list[dict[str, Any]], bool, OctodotError | None]:
    """Scan all pages of a resource collection. Returns (items, complete, error)."""
    if deadline_start is None:
        deadline_start = time.monotonic()
    items: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None

    while True:
        params = {"pageSize": "100"}
        if page_token:
            params["pageToken"] = page_token
        query_str = urllib.parse.urlencode(params)
        sep = "&" if "?" in endpoint else "?"
        url_path = f"{endpoint}{sep}{query_str}"

        try:
            status, resp = request_json(
                "GET",
                url_path,
                key=key,
                timeout=timeout,
                deadline_start=deadline_start,
                deadline=deadline,
            )
        except OctodotError as err:
            return items, False, err

        if collection_key not in resp:
            page_items = []
        else:
            raw_list = resp[collection_key]
            if not isinstance(raw_list, list):
                err = OctodotError(
                    error_record("protocol_error", f"Collection {collection_key} must be a list", "paginate"),
                    exit_code=4,
                )
                return items, False, err
            page_items = raw_list

        for item in page_items:
            if not isinstance(item, dict):
                err = OctodotError(
                    error_record("protocol_error", "Collection item must be an object", "paginate"),
                    exit_code=4,
                )
                return items, False, err
            name = item.get("name")
            if not name or not isinstance(name, str):
                err = OctodotError(
                    error_record("protocol_error", "Item missing valid name property", "paginate"),
                    exit_code=4,
                )
                return items, False, err
            if name in seen_names:
                err = OctodotError(
                    error_record("protocol_error", f"Duplicate resource name encountered: {name}", "paginate"),
                    exit_code=4,
                )
                return items, False, err
            seen_names.add(name)
            items.append(item)

        next_token = resp.get("nextPageToken")
        if not next_token:
            break
        if not isinstance(next_token, str):
            err = OctodotError(
                error_record("protocol_error", "nextPageToken must be a string", "paginate"),
                exit_code=4,
            )
            return items, False, err
        if next_token in seen_tokens:
            err = OctodotError(
                error_record("protocol_error", "nextPageToken repetition detected", "paginate"),
                exit_code=4,
            )
            return items, False, err
        seen_tokens.add(next_token)
        page_token = next_token

    return items, True, None


def resolve_source(
    owner: str,
    repo: str,
    requested_branch: str | None,
    key: str,
    timeout: float = 30.0,
    deadline_start: float | None = None,
    deadline: float = 120.0,
) -> tuple[dict[str, Any], str, str]:
    """Find and verify the Google Jules source for the specified repository."""
    if deadline_start is None:
        deadline_start = time.monotonic()
    sources, complete, err = paginate(
        "/sources",
        "sources",
        key=key,
        timeout=timeout,
        deadline_start=deadline_start,
        deadline=deadline,
    )
    if not complete:
        raise err or OctodotError(
            error_record("protocol_error", "Incomplete sources scan", "resolve_source"),
            exit_code=4,
        )

    matched: list[dict[str, Any]] = []
    for s in sources:
        gh = s.get("githubRepo")
        if isinstance(gh, dict):
            s_owner = gh.get("owner", "")
            s_repo = gh.get("repo", "")
            if isinstance(s_owner, str) and isinstance(s_repo, str):
                if s_owner.lower() == owner.lower() and s_repo.lower() == repo.lower():
                    matched.append(s)

    if not matched:
        raise OctodotError(
            error_record("source_not_found", f"No source found matching {owner}/{repo}", "resolve_source"),
            exit_code=4,
        )
    if len(matched) > 1:
        raise OctodotError(
            error_record("ambiguous_source", f"Multiple sources found matching {owner}/{repo}", "resolve_source"),
            exit_code=4,
        )

    listed_source = matched[0]
    source_name = listed_source.get("name")
    if not source_name or not isinstance(source_name, str):
        raise OctodotError(
            error_record("protocol_error", "Source has invalid name", "resolve_source"),
            exit_code=4,
        )

    # GET detailed source
    quoted_name = safe_quote_resource_name(source_name)
    status, detail = request_json(
        "GET",
        f"/{quoted_name}",
        key=key,
        timeout=timeout,
        deadline_start=deadline_start,
        deadline=deadline,
    )
    if detail.get("name") != source_name:
        raise OctodotError(
            error_record("protocol_error", "Detailed source name mismatch", "resolve_source"),
            exit_code=4,
        )

    detail_gh = detail.get("githubRepo")
    if (
        not isinstance(detail_gh, dict)
        or detail_gh.get("owner", "").lower() != owner.lower()
        or detail_gh.get("repo", "").lower() != repo.lower()
    ):
        raise OctodotError(
            error_record("protocol_error", "Detailed source githubRepo mismatch", "resolve_source"),
            exit_code=4,
        )

    # Branch resolution
    default_branch_obj = detail.get("defaultBranch")
    default_name = (
        default_branch_obj.get("displayName")
        if isinstance(default_branch_obj, dict) and isinstance(default_branch_obj.get("displayName"), str)
        else None
    )

    branches_list = detail.get("branches")
    all_branch_names: set[str] = set()
    if default_name:
        all_branch_names.add(default_name)
    if isinstance(branches_list, list):
        for b in branches_list:
            if isinstance(b, dict) and isinstance(b.get("displayName"), str) and b["displayName"]:
                all_branch_names.add(b["displayName"])

    if not all_branch_names:
        raise OctodotError(
            error_record("source_error", "Source has no branches configured", "resolve_source"),
            exit_code=4,
        )

    chosen_branch: str
    if requested_branch is not None:
        if requested_branch not in all_branch_names:
            raise OctodotError(
                error_record(
                    "branch_not_found",
                    f"Branch '{requested_branch}' not found on source {source_name}",
                    "resolve_source",
                ),
                exit_code=4,
            )
        chosen_branch = requested_branch
    else:
        if not default_name:
            raise OctodotError(
                error_record("source_error", "Source has no default branch", "resolve_source"),
                exit_code=4,
            )
        chosen_branch = default_name

    return detail, source_name, chosen_branch


def read_session(
    session_name: str,
    key: str,
    timeout: float = 30.0,
    deadline_start: float | None = None,
    deadline: float = 120.0,
) -> dict[str, Any]:
    """Fetch session detail by resource name."""
    if deadline_start is None:
        deadline_start = time.monotonic()
    quoted = safe_quote_resource_name(session_name)
    status, session = request_json(
        "GET",
        f"/{quoted}",
        key=key,
        timeout=timeout,
        deadline_start=deadline_start,
        deadline=deadline,
    )
    return session


def read_activities(
    session_name: str,
    key: str,
    timeout: float = 30.0,
    deadline_start: float | None = None,
    deadline: float = 120.0,
) -> tuple[list[dict[str, Any]], bool, OctodotError | None]:
    """Fetch all activities for a session."""
    if deadline_start is None:
        deadline_start = time.monotonic()
    quoted = safe_quote_resource_name(session_name)
    return paginate(
        f"/{quoted}/activities",
        "activities",
        key=key,
        timeout=timeout,
        deadline_start=deadline_start,
        deadline=deadline,
    )


def classify_session(session: dict[str, Any]) -> str:
    """Classify session state into pending, blocked, failed, completed, or unknown."""
    raw_state = session.get("state")
    if raw_state in ("QUEUED", "PLANNING", "IN_PROGRESS"):
        return "pending"
    if raw_state in ("PAUSED", "AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK"):
        return "blocked"
    if raw_state == "FAILED":
        return "failed"
    if raw_state == "COMPLETED":
        return "completed"
    return "unknown"


def collect_patches(
    session_name: str,
    source_name: str,
    activities: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    """Enumerate candidate patches matching session source."""
    candidates: list[dict[str, Any]] = []
    for act in activities:
        act_name = act.get("name", "")
        create_time = act.get("createTime")
        artifacts = act.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        for idx, art in enumerate(artifacts):
            if not isinstance(art, dict):
                continue
            change_set = art.get("changeSet")
            if not isinstance(change_set, dict):
                continue
            if change_set.get("source") != source_name:
                continue
            git_patch = change_set.get("gitPatch")
            if not isinstance(git_patch, dict):
                continue

            base_commit = git_patch.get("baseCommitId")
            sugg_msg = git_patch.get("suggestedCommitMessage")
            raw_patch = git_patch.get("unidiffPatch")

            patch_str = raw_patch if isinstance(raw_patch, str) else None
            base_str = base_commit if isinstance(base_commit, str) else None
            msg_str = sugg_msg if isinstance(sugg_msg, str) else None

            patch_available = bool(patch_str and (key not in patch_str))
            apply_base_available = bool(
                base_str and (len(base_str) in (40, 64)) and re.match(r"^[0-9a-fA-F]+$", base_str)
            )

            patch_sha = None
            if patch_available and patch_str is not None:
                patch_sha = hashlib.sha256(patch_str.encode("utf-8")).hexdigest()

            candidates.append(
                {
                    "activity": act_name,
                    "applyBaseAvailable": apply_base_available,
                    "artifactIndex": idx,
                    "baseCommitId": base_str,
                    "createTime": create_time,
                    "patchAvailable": patch_available,
                    "patchSha256": patch_sha,
                    "sessionName": session_name,
                    "source": source_name,
                    "suggestedCommitMessage": msg_str,
                }
            )
    return candidates


def select_patch(
    session: dict[str, Any],
    activities: list[dict[str, Any]],
    selector_activity: str | None,
    selector_artifact: int | None,
    key: str,
    timeout: float = 30.0,
    deadline_start: float | None = None,
    deadline: float = 120.0,
) -> tuple[dict[str, Any], str]:
    """Select target patch either explicitly or automatically."""
    if deadline_start is None:
        deadline_start = time.monotonic()
    session_name = session.get("name")
    source_ctx = session.get("sourceContext")
    source_name = source_ctx.get("source") if isinstance(source_ctx, dict) else None
    if not session_name or not source_name:
        raise OctodotError(
            error_record("protocol_error", "Session missing name or sourceContext", "select_patch"),
            exit_code=4,
        )

    candidates = collect_patches(session_name, source_name, activities, key)

    if selector_activity is not None and selector_artifact is not None:
        # Explicit selection
        matched_cand = None
        for cand in candidates:
            if cand["activity"] == selector_activity and cand["artifactIndex"] == selector_artifact:
                matched_cand = cand
                break
        if not matched_cand:
            raise OctodotError(
                error_record(
                    "not_found",
                    f"Selected activity/artifact not found: {selector_activity}[{selector_artifact}]",
                    "select_patch",
                ),
                exit_code=4,
            )

        # GET exact activity
        quoted_act = safe_quote_resource_name(selector_activity)
        status, fresh_act = request_json(
            "GET",
            f"/{quoted_act}",
            key=key,
            timeout=timeout,
            deadline_start=deadline_start,
            deadline=deadline,
        )
        artifacts = fresh_act.get("artifacts", [])
        if selector_artifact >= len(artifacts):
            raise OctodotError(
                error_record("protocol_error", "Selected artifact index out of bounds", "select_patch"),
                exit_code=4,
            )
        art = artifacts[selector_artifact]
        change_set = art.get("changeSet", {})
        if change_set.get("source") != source_name:
            raise OctodotError(
                error_record("source_mismatch", "Selected artifact source mismatch", "select_patch"),
                exit_code=4,
            )
        git_patch = change_set.get("gitPatch")
        if not isinstance(git_patch, dict):
            raise OctodotError(
                error_record("no_patch_available", "Selected artifact has no gitPatch", "select_patch"),
                exit_code=4,
            )
        raw_patch = git_patch.get("unidiffPatch")
        if not isinstance(raw_patch, str) or not raw_patch:
            raise OctodotError(
                error_record("no_patch_available", "Selected artifact has empty patch", "select_patch"),
                exit_code=4,
            )
        if key in raw_patch:
            raise OctodotError(
                error_record("secret_in_artifact", "Artifact contains API key", "select_patch"),
                exit_code=4,
            )
        return matched_cand, raw_patch

    # Automatic selection
    if not candidates:
        raise OctodotError(
            error_record("no_patch_available", "No matching patches available", "select_patch"),
            exit_code=4,
        )

    # Check timestamps and order by nanoseconds
    parsed_candidates: list[tuple[int, dict[str, Any]]] = []
    for cand in candidates:
        ts_str = cand.get("createTime")
        nanos = parse_rfc3339_nanoseconds(ts_str) if ts_str else None
        if nanos is None:
            raise OctodotError(
                error_record(
                    "ambiguous_patch",
                    f"Candidate has invalid RFC3339 createTime: {ts_str}",
                    "select_patch",
                ),
                exit_code=4,
            )
        parsed_candidates.append((nanos, cand))

    parsed_candidates.sort(key=lambda pair: pair[0], reverse=True)
    max_nanos = parsed_candidates[0][0]
    top_candidates = [cand for nanos, cand in parsed_candidates if nanos == max_nanos]

    if len(top_candidates) > 1:
        raise OctodotError(
            error_record(
                "ambiguous_patch",
                "Multiple candidate patches tied at latest timestamp",
                "select_patch",
            ),
            exit_code=4,
        )

    chosen_cand = top_candidates[0]
    if not chosen_cand["patchAvailable"]:
        raise OctodotError(
            error_record(
                "no_patch_available",
                "Latest candidate patch is empty or unavailable",
                "select_patch",
            ),
            exit_code=4,
        )

    # GET chosen activity to verify against listed snapshot
    quoted_act = safe_quote_resource_name(chosen_cand["activity"])
    status, fresh_act = request_json(
        "GET",
        f"/{quoted_act}",
        key=key,
        timeout=timeout,
        deadline_start=deadline_start,
        deadline=deadline,
    )

    art_idx = chosen_cand["artifactIndex"]
    fresh_artifacts = fresh_act.get("artifacts", [])
    if art_idx >= len(fresh_artifacts):
        raise OctodotError(
            error_record("artifact_changed", "Artifact index out of bounds on re-read", "select_patch"),
            exit_code=4,
        )
    fresh_art = fresh_artifacts[art_idx]
    fresh_cs = fresh_art.get("changeSet", {})
    if fresh_cs.get("source") != source_name:
        raise OctodotError(
            error_record("artifact_changed", "Source changed on re-read", "select_patch"),
            exit_code=4,
        )
    fresh_gp = fresh_cs.get("gitPatch", {})
    fresh_patch = fresh_gp.get("unidiffPatch")
    fresh_base = fresh_gp.get("baseCommitId")
    fresh_msg = fresh_gp.get("suggestedCommitMessage")
    fresh_time = fresh_act.get("createTime")

    if (
        fresh_time != chosen_cand["createTime"]
        or fresh_base != chosen_cand["baseCommitId"]
        or fresh_msg != chosen_cand["suggestedCommitMessage"]
    ):
        raise OctodotError(
            error_record("artifact_changed", "Artifact metadata changed on re-read", "select_patch"),
            exit_code=4,
        )

    if not isinstance(fresh_patch, str) or not fresh_patch:
        raise OctodotError(
            error_record("no_patch_available", "Re-read patch is empty", "select_patch"),
            exit_code=4,
        )

    fresh_sha = hashlib.sha256(fresh_patch.encode("utf-8")).hexdigest()
    if fresh_sha != chosen_cand["patchSha256"]:
        raise OctodotError(
            error_record("artifact_changed", "Patch content changed on re-read", "select_patch"),
            exit_code=4,
        )

    if key in fresh_patch:
        raise OctodotError(
            error_record("secret_in_artifact", "Artifact contains API key", "select_patch"),
            exit_code=4,
        )

    return chosen_cand, fresh_patch


def inspect_patch_safety(patch_bytes: bytes, cwd: str) -> None:
    """Preflight check patch for renames, copies, symlinks, and unsafe paths."""
    patch_lines = patch_bytes.splitlines()
    forbidden_prefixes = (
        b"rename from ",
        b"rename to ",
        b"copy from ",
        b"copy to ",
        b"similarity index ",
    )
    for line in patch_lines:
        for pfx in forbidden_prefixes:
            if line.startswith(pfx):
                raise OctodotError(
                    error_record(
                        "unsupported_patch",
                        f"Rename or copy patches are refused: {line.decode('utf-8', errors='replace')}",
                        "inspect_patch",
                    ),
                    exit_code=4,
                )

    # Preflight numstat
    res_numstat = run_git(["apply", "--numstat", "-z", "-"], cwd=cwd, input_bytes=patch_bytes)
    if res_numstat.returncode != 0:
        raise OctodotError(
            error_record("git_error", "git apply --numstat failed", "inspect_patch"),
            exit_code=4,
        )

    # numstat -z output: <added>\t<deleted>\t<path>\0
    parts = res_numstat.stdout.split(b"\0")
    for part in parts:
        if not part:
            continue
        tab_parts = part.split(b"\t")
        if len(tab_parts) >= 3:
            path_bytes = tab_parts[2]
            path_str = path_bytes.decode("utf-8", errors="replace")
            # Path checks
            if (
                path_str.startswith("/")
                or ".." in path_str.split("/")
                or any(seg == "" for seg in path_str.split("/"))
                or any(seg.lower() == ".git" for seg in path_str.split("/"))
                or path_str == ".gitmodules"
            ):
                raise OctodotError(
                    error_record("unsafe_path", f"Unsafe path in patch: {path_str}", "inspect_patch"),
                    exit_code=4,
                )

    # Preflight summary
    res_summary = run_git(["apply", "--summary", "-"], cwd=cwd, input_bytes=patch_bytes)
    if res_summary.returncode != 0:
        raise OctodotError(
            error_record("git_error", "git apply --summary failed", "inspect_patch"),
            exit_code=4,
        )
    summary_text = res_summary.stdout.decode("utf-8", errors="replace")
    if "120000" in summary_text or "160000" in summary_text:
        raise OctodotError(
            error_record("unsupported_patch", "Symlink or submodule mode changes refused", "inspect_patch"),
            exit_code=4,
        )


def verify_clean_worktree(cwd: str) -> None:
    """Verify that worktree is completely clean including untracked and ignored."""
    res = run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"], cwd=cwd)
    if res.returncode != 0:
        raise OctodotError(
            error_record("git_error", "git status failed", "verify_clean"),
            exit_code=4,
        )
    if res.stdout.strip():
        raise OctodotError(
            error_record("dirty_worktree", "Worktree is not clean", "verify_clean"),
            exit_code=4,
        )


def check_worktree_structure(cwd: str) -> None:
    """Verify that repo is not bare, not sparse, has no merge, submodules, or symlinks."""
    res_bare = run_git(["rev-parse", "--is-bare-repository"], cwd=cwd)
    if res_bare.stdout.strip() == b"true":
        raise OctodotError(
            error_record("unsupported_repo", "Bare repositories are not supported", "check_worktree"),
            exit_code=4,
        )

    res_sparse = run_git(["config", "--bool", "core.sparseCheckout"], cwd=cwd)
    if res_sparse.stdout.strip() == b"true":
        raise OctodotError(
            error_record("unsupported_repo", "Sparse checkouts are not supported", "check_worktree"),
            exit_code=4,
        )

    # Check merge head
    merge_head = os.path.join(cwd, ".git", "MERGE_HEAD")
    if os.path.exists(merge_head):
        raise OctodotError(
            error_record("unresolved_merge", "Unresolved merge detected", "check_worktree"),
            exit_code=4,
        )

    # Check tracked submodules and symlinks in index
    res_files = run_git(["ls-files", "-s", "-z"], cwd=cwd)
    for entry in res_files.stdout.split(b"\0"):
        if not entry:
            continue
        mode = entry.split(b" ")[0]
        if mode == b"160000":
            raise OctodotError(
                error_record("unsupported_repo", "Tracked submodule / gitlink found", "check_worktree"),
                exit_code=4,
            )
        if mode == b"120000":
            raise OctodotError(
                error_record("unsupported_repo", "Tracked symlink found", "check_worktree"),
                exit_code=4,
            )

    # Check skip-worktree / assume-unchanged
    res_v = run_git(["ls-files", "-v", "-z"], cwd=cwd)
    for entry in res_v.stdout.split(b"\0"):
        if not entry:
            continue
        flag = entry[:1]
        if flag.islower() or flag in (b"S", b"s"):
            raise OctodotError(
                error_record("unsupported_repo", "skip-worktree or assume-unchanged flag present", "check_worktree"),
                exit_code=4,
            )

    # Walk worktree to detect nested .git
    for root, dirs, files in os.walk(cwd):
        rel = os.path.relpath(root, cwd)
        if rel == ".":
            dirs[:] = [d for d in dirs if d != ".git"]
            continue
        if ".git" in dirs or ".git" in files:
            raise OctodotError(
                error_record("unsupported_repo", "Nested .git detected in worktree", "check_worktree"),
                exit_code=4,
            )


def apply_patch(
    cwd: str,
    patch_bytes: bytes,
    base_commit: str,
    source_owner: str,
    source_repo: str,
) -> None:
    """Safely apply patch bytes to the target worktree."""
    if sys.platform not in ("linux", "darwin"):
        raise OctodotError(
            error_record("unsupported_platform", f"Platform {sys.platform} is not supported", "apply_patch"),
            exit_code=3,
        )

    check_git_version()
    check_git_config(cwd=cwd)

    canonical_cwd = os.path.realpath(cwd)
    if not os.path.isdir(canonical_cwd):
        raise OctodotError(
            error_record("invalid_cwd", f"Target directory does not exist: {cwd}", "apply_patch"),
            exit_code=4,
        )

    res_toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=canonical_cwd)
    if res_toplevel.returncode != 0:
        raise OctodotError(
            error_record("invalid_cwd", "Not a git repository", "apply_patch"),
            exit_code=4,
        )
    top_level = os.path.realpath(res_toplevel.stdout.decode("utf-8", errors="replace").strip())
    if canonical_cwd != top_level:
        raise OctodotError(
            error_record("invalid_cwd", "Working directory must equal Git top-level root", "apply_patch"),
            exit_code=4,
        )

    check_worktree_structure(canonical_cwd)

    # Verify origin matches authenticated source
    res_origin = run_git(["remote", "get-url", "--all", "origin"], cwd=canonical_cwd)
    if res_origin.returncode != 0:
        raise OctodotError(
            error_record("origin_mismatch", "Origin remote not found", "apply_patch"),
            exit_code=4,
        )
    urls = [line.strip() for line in res_origin.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if len(urls) != 1:
        raise OctodotError(
            error_record("origin_mismatch", "Multiple origin URLs configured", "apply_patch"),
            exit_code=4,
        )
    parsed = parse_remote_url(urls[0])
    if not parsed or parsed[0].lower() != source_owner.lower() or parsed[1].lower() != source_repo.lower():
        raise OctodotError(
            error_record("origin_mismatch", f"Origin URL {urls[0]} does not match source", "apply_patch"),
            exit_code=4,
        )

    verify_clean_worktree(canonical_cwd)

    # Verify HEAD matches base_commit
    res_head = run_git(["rev-parse", "--verify", "HEAD"], cwd=canonical_cwd)
    if res_head.returncode != 0:
        raise OctodotError(
            error_record("base_mismatch", "Failed to resolve HEAD", "apply_patch"),
            exit_code=4,
        )
    head_sha = res_head.stdout.decode("utf-8", errors="replace").strip()
    if head_sha.lower() != base_commit.lower() or len(head_sha) != len(base_commit):
        raise OctodotError(
            error_record(
                "base_mismatch",
                f"HEAD {head_sha} does not match required baseCommitId {base_commit}",
                "apply_patch",
            ),
            exit_code=4,
        )

    inspect_patch_safety(patch_bytes, canonical_cwd)

    # git apply --check
    res_check = run_git(["apply", "--check", "--whitespace=nowarn", "-"], cwd=canonical_cwd, input_bytes=patch_bytes)
    if res_check.returncode != 0:
        raise OctodotError(
            error_record("apply_failed", "git apply --check failed", "apply_patch"),
            exit_code=4,
        )

    # Repeat clean and base checks
    verify_clean_worktree(canonical_cwd)
    res_head2 = run_git(["rev-parse", "--verify", "HEAD"], cwd=canonical_cwd)
    if res_head2.stdout.decode("utf-8", errors="replace").strip().lower() != base_commit.lower():
        raise OctodotError(
            error_record("base_mismatch", "HEAD changed before apply", "apply_patch"),
            exit_code=4,
        )

    # Capture raw index bytes
    res_idx = run_git(["rev-parse", "--git-path", "index"], cwd=canonical_cwd)
    idx_rel = res_idx.stdout.decode("utf-8", errors="replace").strip()
    idx_path = os.path.join(canonical_cwd, idx_rel)
    if not os.path.isfile(idx_path):
        raise OctodotError(
            error_record("git_error", "Git index file not found", "apply_patch"),
            exit_code=4,
        )
    with open(idx_path, "rb") as f:
        pre_index_bytes = f.read()

    # Apply to worktree only
    res_apply = run_git(["apply", "--whitespace=nowarn", "-"], cwd=canonical_cwd, input_bytes=patch_bytes)
    if res_apply.returncode != 0:
        raise OctodotError(
            error_record("apply_failed", "git apply failed during mutation", "apply_patch"),
            exit_code=4,
        )

    # Verify HEAD and index unchanged
    res_head3 = run_git(["rev-parse", "--verify", "HEAD"], cwd=canonical_cwd)
    if res_head3.stdout.decode("utf-8", errors="replace").strip() != head_sha:
        raise OctodotError(
            error_record("mutation_inconsistent", "HEAD was unexpectedly modified", "apply_patch"),
            exit_code=4,
        )
    with open(idx_path, "rb") as f:
        post_index_bytes = f.read()
    if post_index_bytes != pre_index_bytes:
        raise OctodotError(
            error_record("mutation_inconsistent", "Git index was unexpectedly modified", "apply_patch"),
            exit_code=4,
        )


def teleport(
    target_dir: str,
    session: dict[str, Any],
    patch_bytes: bytes,
    base_commit: str,
    source_owner: str,
    source_repo: str,
    key: str,
) -> tuple[str, str]:
    """Clone repository into absent target_dir, checkout base branch, and apply patch."""
    if sys.platform not in ("linux", "darwin"):
        raise OctodotError(
            error_record("unsupported_platform", f"Platform {sys.platform} is not supported", "teleport"),
            exit_code=3,
        )

    check_git_version()
    check_git_config()

    if os.path.lexists(target_dir):
        raise OctodotError(
            error_record("invalid_dir", f"Destination directory already exists: {target_dir}", "teleport"),
            exit_code=2,
        )

    parent_dir = os.path.dirname(os.path.abspath(target_dir))
    canonical_parent = os.path.realpath(parent_dir)
    if not os.path.isdir(canonical_parent) or not os.access(canonical_parent, os.W_OK):
        raise OctodotError(
            error_record("invalid_dir", f"Parent directory not writable: {canonical_parent}", "teleport"),
            exit_code=2,
        )

    clone_url = f"https://github.com/{source_owner}/{source_repo}.git"
    abs_target = os.path.abspath(target_dir)

    res_clone = run_git(
        ["clone", "--no-checkout", "--no-recurse-submodules", "--template=", "--", clone_url, abs_target]
    )
    if res_clone.returncode != 0:
        raise OctodotError(
            error_record("clone_failed", "Git clone failed", "teleport"),
            exit_code=4,
        )

    # Check base commit exists
    res_cat = run_git(["cat-file", "-t", base_commit], cwd=abs_target)
    if res_cat.returncode != 0 or res_cat.stdout.strip() != b"commit":
        raise OctodotError(
            error_record("base_missing", f"Base commit {base_commit} not found in clone", "teleport"),
            exit_code=4,
        )

    # Check tree for symlinks and submodules
    res_tree = run_git(["ls-tree", "-r", "-z", base_commit], cwd=abs_target)
    for entry in res_tree.stdout.split(b"\0"):
        if not entry:
            continue
        mode = entry.split(b" ")[0]
        if mode in (b"120000", b"160000"):
            raise OctodotError(
                error_record("unsupported_repo", "Base commit contains symlinks or submodules", "teleport"),
                exit_code=4,
            )

    session_name = session.get("name", "")
    suffix = session_name.split("/")[-1] if "/" in session_name else session_name
    branch_name = f"octodot/{suffix}"

    res_co = run_git(["checkout", "-b", branch_name, base_commit, "--"], cwd=abs_target)
    if res_co.returncode != 0:
        raise OctodotError(
            error_record("checkout_failed", "Git checkout failed", "teleport"),
            exit_code=4,
        )

    # Apply patch
    apply_patch(abs_target, patch_bytes, base_commit, source_owner, source_repo)
    return abs_target, branch_name


def create_one(
    attempt: int,
    repo_str: str,
    source_name: str,
    starting_branch: str,
    payload: dict[str, Any],
    fingerprint: str,
    key: str,
    timeout: float,
    deadline_start: float,
    deadline: float,
) -> dict[str, Any]:
    """Execute a single session creation attempt with verified receipts."""
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int((time.time() % 1) * 1000):03d}Z"
    requested_info = {
        "branch": starting_branch,
        "repo": repo_str,
        "source": source_name,
    }

    if STOP_EVENT.is_set() or get_remaining_budget(deadline_start, deadline) <= 0:
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": None,
            "fingerprint": None,
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "not_started",
            "prUrls": [],
            "requested": requested_info,
            "startedAt": None,
            "state": None,
            "type": "attempt",
            "url": None,
        }

    # Emit create_started receipt
    start_receipt = {
        "attempt": attempt,
        "branch": starting_branch,
        "fingerprint": fingerprint,
        "repo": repo_str,
        "source": source_name,
        "startedAt": started_at,
        "type": "create_started",
    }
    try:
        emit_json(sys.stderr, start_receipt, key)
    except Exception:
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": error_record("io_error", "Failed to write create_started", "create_one"),
            "fingerprint": fingerprint,
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "not_started",
            "prUrls": [],
            "requested": requested_info,
            "startedAt": started_at,
            "state": None,
            "type": "attempt",
            "url": None,
        }

    if STOP_EVENT.is_set() or get_remaining_budget(deadline_start, deadline) <= 0:
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": None,
            "fingerprint": fingerprint,
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "not_started",
            "prUrls": [],
            "requested": requested_info,
            "startedAt": started_at,
            "state": None,
            "type": "attempt",
            "url": None,
        }

    # Execute single POST attempt
    try:
        status, resp = request_json(
            "POST",
            "/sessions",
            key=key,
            body=payload,
            timeout=timeout,
            deadline_start=deadline_start,
            deadline=deadline,
            is_post=True,
        )
    except OctodotError as err:
        STOP_EVENT.set()
        outcome = "uncertain" if err.exit_code == 5 else "rejected"
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": err.record,
            "fingerprint": fingerprint,
            "id": None,
            "name": None,
            "observed": None,
            "outcome": outcome,
            "prUrls": [],
            "requested": requested_info,
            "startedAt": started_at,
            "state": None,
            "type": "attempt",
            "url": None,
        }

    session_name = resp.get("name")
    session_id = resp.get("id")
    session_url = resp.get("url")
    session_state = resp.get("state")
    session_prs = resp.get("prUrls") if isinstance(resp.get("prUrls"), list) else []

    if not session_name or not isinstance(session_name, str) or not re.match(r"^sessions/[A-Za-z0-9_-]+$", session_name):
        STOP_EVENT.set()
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": error_record("protocol_error", "Returned session name is missing or invalid", "create_one"),
            "fingerprint": fingerprint,
            "id": session_id,
            "name": session_name,
            "observed": None,
            "outcome": "uncertain",
            "prUrls": session_prs,
            "requested": requested_info,
            "startedAt": started_at,
            "state": session_state,
            "type": "attempt",
            "url": session_url,
        }

    # Emit create_accepted receipt
    accept_receipt = {
        "attempt": attempt,
        "fingerprint": fingerprint,
        "id": session_id,
        "name": session_name,
        "startedAt": started_at,
        "type": "create_accepted",
        "url": session_url,
    }
    try:
        emit_json(sys.stderr, accept_receipt, key)
    except Exception:
        STOP_EVENT.set()
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": error_record("io_error", "Failed to flush create_accepted", "create_one"),
            "fingerprint": fingerprint,
            "id": session_id,
            "name": session_name,
            "observed": None,
            "outcome": "created_unverified",
            "prUrls": session_prs,
            "requested": requested_info,
            "startedAt": started_at,
            "state": session_state,
            "type": "attempt",
            "url": session_url,
        }

    # Context verification
    source_ctx = resp.get("sourceContext")
    obs_source = source_ctx.get("source") if isinstance(source_ctx, dict) else None
    gh_ctx = source_ctx.get("githubRepoContext") if isinstance(source_ctx, dict) else None
    obs_branch = gh_ctx.get("startingBranch") if isinstance(gh_ctx, dict) else None

    # If context fields are present in response
    if obs_source is not None and obs_branch is not None:
        obs_info = {"branch": obs_branch, "repo": repo_str, "source": obs_source}
        if obs_source == source_name and obs_branch == starting_branch:
            return {
                "attempt": attempt,
                "contextVerified": True,
                "error": None,
                "fingerprint": fingerprint,
                "id": session_id,
                "name": session_name,
                "observed": obs_info,
                "outcome": "accepted",
                "prUrls": session_prs,
                "requested": requested_info,
                "startedAt": started_at,
                "state": session_state,
                "type": "attempt",
                "url": session_url,
            }
        else:
            STOP_EVENT.set()
            return {
                "attempt": attempt,
                "contextVerified": False,
                "error": error_record("context_mismatch", "Returned context does not match request", "create_one"),
                "fingerprint": fingerprint,
                "id": session_id,
                "name": session_name,
                "observed": obs_info,
                "outcome": "created_context_mismatch",
                "prUrls": session_prs,
                "requested": requested_info,
                "startedAt": started_at,
                "state": session_state,
                "type": "attempt",
                "url": session_url,
            }

    # Missing context in response -> perform single GET verification
    try:
        quoted_name = safe_quote_resource_name(session_name)
        status, verified_sess = request_json(
            "GET",
            f"/{quoted_name}",
            key=key,
            timeout=timeout,
            deadline_start=deadline_start,
            deadline=deadline,
        )
    except OctodotError as err:
        STOP_EVENT.set()
        return {
            "attempt": attempt,
            "contextVerified": None,
            "error": err.record,
            "fingerprint": fingerprint,
            "id": session_id,
            "name": session_name,
            "observed": None,
            "outcome": "created_unverified",
            "prUrls": session_prs,
            "requested": requested_info,
            "startedAt": started_at,
            "state": session_state,
            "type": "attempt",
            "url": session_url,
        }

    v_src_ctx = verified_sess.get("sourceContext")
    v_src = v_src_ctx.get("source") if isinstance(v_src_ctx, dict) else None
    v_gh = v_src_ctx.get("githubRepoContext") if isinstance(v_src_ctx, dict) else None
    v_branch = v_gh.get("startingBranch") if isinstance(v_gh, dict) else None
    obs_info = {"branch": v_branch, "repo": repo_str, "source": v_src}

    if v_src == source_name and v_branch == starting_branch:
        return {
            "attempt": attempt,
            "contextVerified": True,
            "error": None,
            "fingerprint": fingerprint,
            "id": session_id,
            "name": session_name,
            "observed": obs_info,
            "outcome": "accepted",
            "prUrls": verified_sess.get("prUrls") if isinstance(verified_sess.get("prUrls"), list) else session_prs,
            "requested": requested_info,
            "startedAt": started_at,
            "state": verified_sess.get("state", session_state),
            "type": "attempt",
            "url": session_url,
        }
    else:
        STOP_EVENT.set()
        return {
            "attempt": attempt,
            "contextVerified": False,
            "error": error_record("context_mismatch", "Verified context does not match request", "create_one"),
            "fingerprint": fingerprint,
            "id": session_id,
            "name": session_name,
            "observed": obs_info,
            "outcome": "created_context_mismatch",
            "prUrls": session_prs,
            "requested": requested_info,
            "startedAt": started_at,
            "state": session_state,
            "type": "attempt",
            "url": session_url,
        }


def create_many(
    parallel: int,
    repo_str: str,
    source_name: str,
    starting_branch: str,
    prompt: str,
    title: str | None,
    key: str,
    timeout: float,
    deadline_start: float,
    deadline: float,
) -> int:
    """Execute bounded parallel creation using ThreadPoolExecutor waves."""
    STOP_EVENT.clear()
    payload: dict[str, Any] = {
        "automationMode": "AUTO_CREATE_PR",
        "prompt": prompt,
        "requirePlanApproval": False,
        "sourceContext": {
            "githubRepoContext": {
                "startingBranch": starting_branch,
            },
            "source": source_name,
        },
    }
    if title is not None:
        payload["title"] = title

    canonical_body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()

    import concurrent.futures

    max_workers = min(5, parallel)
    results: dict[int, dict[str, Any]] = {}
    next_ordinal = 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        active_futures: dict[concurrent.futures.Future[dict[str, Any]], int] = {}

        # Enqueue initial wave up to max_workers
        while next_ordinal <= parallel and len(active_futures) < max_workers and not STOP_EVENT.is_set():
            ord_num = next_ordinal
            next_ordinal += 1
            fut = executor.submit(
                create_one,
                ord_num,
                repo_str,
                source_name,
                starting_branch,
                payload,
                fingerprint,
                key,
                timeout,
                deadline_start,
                deadline,
            )
            active_futures[fut] = ord_num

        while active_futures:
            done, _ = concurrent.futures.wait(active_futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                ord_num = active_futures.pop(fut)
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {
                        "attempt": ord_num,
                        "contextVerified": None,
                        "error": error_record("worker_exception", str(exc), "create_many"),
                        "fingerprint": fingerprint,
                        "id": None,
                        "name": None,
                        "observed": None,
                        "outcome": "uncertain",
                        "prUrls": [],
                        "requested": {"branch": starting_branch, "repo": repo_str, "source": source_name},
                        "startedAt": None,
                        "state": None,
                        "type": "attempt",
                        "url": None,
                    }
                eff_ord = res.get("attempt") if (isinstance(res, dict) and isinstance(res.get("attempt"), int)) else ord_num
                results[eff_ord] = res

            # Refill if stop event is not set
            if not STOP_EVENT.is_set() and get_remaining_budget(deadline_start, deadline) > 0:
                while next_ordinal <= parallel and len(active_futures) < max_workers:
                    ord_num = next_ordinal
                    next_ordinal += 1
                    fut = executor.submit(
                        create_one,
                        ord_num,
                        repo_str,
                        source_name,
                        starting_branch,
                        payload,
                        fingerprint,
                        key,
                        timeout,
                        deadline_start,
                        deadline,
                    )
                    active_futures[fut] = ord_num

    # Any remaining unsubmitted ordinals become not_started
    requested_info = {"branch": starting_branch, "repo": repo_str, "source": source_name}
    for ord_num in range(1, parallel + 1):
        if ord_num not in results:
            results[ord_num] = {
                "attempt": ord_num,
                "contextVerified": None,
                "error": None,
                "fingerprint": None,
                "id": None,
                "name": None,
                "observed": None,
                "outcome": "not_started",
                "prUrls": [],
                "requested": requested_info,
                "startedAt": None,
                "state": None,
                "type": "attempt",
                "url": None,
            }

    # Emit attempt objects in ordinal order
    for ord_num in range(1, parallel + 1):
        emit_json(sys.stdout, results[ord_num], key)

    # Compute summary
    counts = {
        "accepted": 0,
        "createdContextMismatch": 0,
        "createdUnverified": 0,
        "notStarted": 0,
        "rejected": 0,
        "uncertain": 0,
    }
    summary_error = None
    for ord_num in range(1, parallel + 1):
        att = results[ord_num]
        outc = att["outcome"]
        if outc == "accepted":
            counts["accepted"] += 1
        elif outc == "rejected":
            counts["rejected"] += 1
        elif outc == "uncertain":
            counts["uncertain"] += 1
        elif outc == "created_unverified":
            counts["createdUnverified"] += 1
        elif outc == "created_context_mismatch":
            counts["createdContextMismatch"] += 1
        elif outc == "not_started":
            counts["notStarted"] += 1

        if summary_error is None and att.get("error") is not None:
            summary_error = att["error"]

    if summary_error is None and INTERRUPTED:
        summary_error = error_record("interrupted", "Execution interrupted", "main")

    is_ok = (counts["accepted"] == parallel) and (summary_error is None)

    # Exit code determination
    if counts["uncertain"] > 0 or counts["createdUnverified"] > 0 or counts["createdContextMismatch"] > 0:
        exit_code = 5
    elif any(att.get("error", {}).get("httpStatus") in (401, 403) for att in results.values() if att.get("error")):
        exit_code = 3
    elif is_ok:
        exit_code = 0
    else:
        exit_code = 4

    if INTERRUPTED and exit_code == 0:
        exit_code = 4

    summary = {
        "accepted": counts["accepted"],
        "createdContextMismatch": counts["createdContextMismatch"],
        "createdUnverified": counts["createdUnverified"],
        "error": summary_error,
        "exitCode": exit_code,
        "notStarted": counts["notStarted"],
        "ok": is_ok,
        "rejected": counts["rejected"],
        "requested": parallel,
        "type": "summary",
        "uncertain": counts["uncertain"],
    }
    emit_json(sys.stdout, summary, key)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Main program entrypoint."""
    global INTERRUPTED
    INTERRUPTED = False
    STOP_EVENT.clear()
    install_signals()
    if argv is None:
        argv = sys.argv[1:]

    start_time = time.monotonic()

    # Step 1: Parse arguments
    try:
        args = parse_args(argv)
    except OctodotError as err:
        emit_json(
            sys.stderr,
            {
                "action": "unknown",
                "complete": False,
                "data": None,
                "error": err.record,
                "ok": False,
            },
        )
        return err.exit_code

    action = args["action"]
    if action == "help":
        parser = build_parser()
        print(parser.format_help().strip())
        return 0
    if action == "version":
        print(f"octodot {VERSION}")
        return 0

    timeout = args["timeout"]
    deadline = args["deadline"]

    # Step 2: Read prompt if -new
    prompt_content = None
    if action == "new":
        try:
            prompt_content = resolve_prompt(args["prompt"])
        except OctodotError as err:
            emit_json(
                sys.stderr,
                {
                    "action": "new",
                    "complete": False,
                    "data": None,
                    "error": err.record,
                    "ok": False,
                },
            )
            return err.exit_code

    # Step 3: Validate API key
    raw_key = os.environ.get("JULES_API_KEY")
    if not raw_key or not raw_key.strip():
        err_rec = error_record("missing_configuration", "JULES_API_KEY environment variable is required", "auth")
        if action == "new":
            emit_json(
                sys.stdout,
                {
                    "accepted": 0,
                    "createdContextMismatch": 0,
                    "createdUnverified": 0,
                    "error": err_rec,
                    "exitCode": 3,
                    "notStarted": args["parallel"],
                    "ok": False,
                    "rejected": 0,
                    "requested": args["parallel"],
                    "type": "summary",
                    "uncertain": 0,
                },
            )
            emit_json(sys.stderr, err_rec)
        else:
            emit_json(
                sys.stdout,
                {
                    "action": action,
                    "complete": False,
                    "data": None,
                    "error": err_rec,
                    "ok": False,
                },
            )
            emit_json(sys.stderr, err_rec)
        return 3

    if "\r" in raw_key or "\n" in raw_key:
        err_rec = error_record("invalid_configuration", "JULES_API_KEY contains carriage return or newline", "auth")
        if action == "new":
            emit_json(
                sys.stdout,
                {
                    "accepted": 0,
                    "createdContextMismatch": 0,
                    "createdUnverified": 0,
                    "error": err_rec,
                    "exitCode": 3,
                    "notStarted": args["parallel"],
                    "ok": False,
                    "rejected": 0,
                    "requested": args["parallel"],
                    "type": "summary",
                    "uncertain": 0,
                },
            )
            emit_json(sys.stderr, err_rec)
        else:
            emit_json(
                sys.stdout,
                {
                    "action": action,
                    "complete": False,
                    "data": None,
                    "error": err_rec,
                    "ok": False,
                },
            )
            emit_json(sys.stderr, err_rec)
        return 3

    api_key = raw_key

    # Dispatch actions
    if action == "new":
        # Resolve repo and branch
        repo_arg = args["repo"]
        cwd = args["cwd"] or os.getcwd()
        try:
            if repo_arg is None or repo_arg == ".":
                owner, repo, branch = infer_repo(cwd, args["branch"])
            else:
                parsed_repo = validate_repo_arg(repo_arg)
                owner, repo = parsed_repo
                branch = args["branch"]

            source_obj, source_name, chosen_branch = resolve_source(
                owner,
                repo,
                branch,
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
        except OctodotError as err:
            # Preflight failure: emit all ordinals as not_started plus summary
            p_count = args["parallel"]
            req_info = {"branch": branch if "branch" in locals() else None, "repo": repo_arg, "source": None}
            for ord_num in range(1, p_count + 1):
                emit_json(
                    sys.stdout,
                    {
                        "attempt": ord_num,
                        "contextVerified": None,
                        "error": None,
                        "fingerprint": None,
                        "id": None,
                        "name": None,
                        "observed": None,
                        "outcome": "not_started",
                        "prUrls": [],
                        "requested": req_info,
                        "startedAt": None,
                        "state": None,
                        "type": "attempt",
                        "url": None,
                    },
                    api_key,
                )
            emit_json(
                sys.stdout,
                {
                    "accepted": 0,
                    "createdContextMismatch": 0,
                    "createdUnverified": 0,
                    "error": err.record,
                    "exitCode": err.exit_code,
                    "notStarted": p_count,
                    "ok": False,
                    "rejected": 0,
                    "requested": p_count,
                    "type": "summary",
                    "uncertain": 0,
                },
                api_key,
            )
            emit_json(sys.stderr, err.record, api_key)
            return err.exit_code

        repo_str = f"{owner}/{repo}"
        return create_many(
            args["parallel"],
            repo_str,
            source_name,
            chosen_branch,
            prompt_content,
            args["title"],
            key=api_key,
            timeout=timeout,
            deadline_start=start_time,
            deadline=deadline,
        )

    # Read and local mutation actions
    try:
        if action == "list-repos":
            sources, complete, err = paginate(
                "/sources",
                "sources",
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            if not complete:
                raise err or OctodotError(
                    error_record("protocol_error", "Failed to list sources", "list-repos"),
                    exit_code=4,
                )
            emit_json(
                sys.stdout,
                {
                    "action": "list-repos",
                    "complete": True,
                    "data": {"sources": sources},
                    "error": None,
                    "ok": True,
                },
                api_key,
            )
            return 0

        elif action == "list-sessions":
            sessions, complete, err = paginate(
                "/sessions",
                "sessions",
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            if not complete:
                raise err or OctodotError(
                    error_record("protocol_error", "Failed to list sessions", "list-sessions"),
                    exit_code=4,
                )
            emit_json(
                sys.stdout,
                {
                    "action": "list-sessions",
                    "complete": True,
                    "data": {"sessions": sessions},
                    "error": None,
                    "ok": True,
                },
                api_key,
            )
            return 0

        elif action == "status":
            sess = read_session(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            emit_json(
                sys.stdout,
                {
                    "action": "status",
                    "complete": True,
                    "data": {"session": sess},
                    "error": None,
                    "ok": True,
                },
                api_key,
            )
            return 0

        elif action == "activities":
            acts, complete, err = read_activities(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            if not complete:
                raise err or OctodotError(
                    error_record("protocol_error", "Failed to list activities", "activities"),
                    exit_code=4,
                )
            emit_json(
                sys.stdout,
                {
                    "action": "activities",
                    "complete": True,
                    "data": {"activities": acts, "sessionName": args["session"]},
                    "error": None,
                    "ok": True,
                },
                api_key,
            )
            return 0

        elif action == "results":
            sess = read_session(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            acts, complete, err = read_activities(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            if not complete:
                raise err or OctodotError(
                    error_record("protocol_error", "Failed to list activities for results", "results"),
                    exit_code=4,
                )

            src_name = sess.get("sourceContext", {}).get("source", "")
            patches = collect_patches(args["session"], src_name, acts, key=api_key)
            classification = classify_session(sess)

            # Determine latestActivity
            latest_act = None
            valid_acts = []
            for act in acts:
                t = act.get("createTime")
                nanos = parse_rfc3339_nanoseconds(t) if t else None
                if nanos is not None:
                    valid_acts.append((nanos, act))
            if valid_acts:
                valid_acts.sort(key=lambda x: x[0], reverse=True)
                if len(valid_acts) == 1 or valid_acts[0][0] != valid_acts[1][0]:
                    latest_act = valid_acts[0][1]

            def _has_pr(s: dict[str, Any]) -> bool:
                if s.get("prUrls"):
                    return True
                for out in s.get("outputs", []):
                    if isinstance(out, dict):
                        pr_obj = out.get("pullRequest")
                        if isinstance(pr_obj, dict) and pr_obj.get("url"):
                            return True
                        if out.get("url"):
                            return True
                return False

            # Delivery determination
            delivery = classification
            if classification == "completed":
                if not _has_pr(sess):
                    # One extra fresh session read
                    try:
                        fresh_sess = read_session(
                            args["session"],
                            key=api_key,
                            timeout=timeout,
                            deadline_start=start_time,
                            deadline=deadline,
                        )
                        delivery = "pr_reported" if _has_pr(fresh_sess) else "completed_without_pr"
                    except Exception:
                        raise OctodotError(
                            error_record("protocol_error", "Failed extra session read", "results"),
                            exit_code=4,
                        )
                else:
                    delivery = "pr_reported"

            data = {
                "classification": classification,
                "delivery": delivery,
                "latestActivity": latest_act,
                "outputs": sess.get("outputs", []),
                "patches": patches,
                "session": sess,
            }
            emit_json(
                sys.stdout,
                {
                    "action": "results",
                    "complete": True,
                    "data": data,
                    "error": None,
                    "ok": True,
                },
                api_key,
            )
            return 0

        elif action == "pull":
            sess = read_session(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            acts, complete, err = read_activities(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            if not complete:
                raise err or OctodotError(
                    error_record("protocol_error", "Failed to list activities for pull", "pull"),
                    exit_code=4,
                )

            cand, patch_text = select_patch(
                sess,
                acts,
                args["activity"],
                args["artifact"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )

            if args["json"]:
                data = {
                    "activity": cand["activity"],
                    "artifactIndex": cand["artifactIndex"],
                    "baseCommitId": cand["baseCommitId"],
                    "createTime": cand["createTime"],
                    "patch": patch_text,
                    "patchSha256": cand["patchSha256"],
                    "sessionName": cand["sessionName"],
                    "source": cand["source"],
                    "suggestedCommitMessage": cand["suggestedCommitMessage"],
                }
                emit_json(
                    sys.stdout,
                    {
                        "action": "pull",
                        "complete": True,
                        "data": data,
                        "error": None,
                        "ok": True,
                    },
                    api_key,
                )
                return 0

            elif args["apply"]:
                if not cand["baseCommitId"]:
                    raise OctodotError(
                        error_record("base_missing", "Artifact has no baseCommitId for apply", "pull"),
                        exit_code=4,
                    )
                cwd = args["cwd"] or os.getcwd()
                # Parse source owner/repo from source name or session
                # Fetch source to get owner/repo
                q_src = safe_quote_resource_name(cand["source"])
                _, src_detail = request_json(
                    "GET",
                    f"/{q_src}",
                    key=api_key,
                    timeout=timeout,
                    deadline_start=start_time,
                    deadline=deadline,
                )
                gh = src_detail.get("githubRepo", {})
                apply_patch(
                    cwd,
                    patch_text.encode("utf-8"),
                    cand["baseCommitId"],
                    gh.get("owner", ""),
                    gh.get("repo", ""),
                )
                branch_res = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd)
                curr_branch = branch_res.stdout.decode("utf-8", errors="replace").strip()
                data = {
                    "applied": True,
                    "artifactIndex": cand["artifactIndex"],
                    "baseCommitId": cand["baseCommitId"],
                    "branch": curr_branch,
                    "cwd": os.path.realpath(cwd),
                    "patchSha256": cand["patchSha256"],
                    "sessionName": cand["sessionName"],
                    "source": cand["source"],
                }
                emit_json(
                    sys.stdout,
                    {
                        "action": "pull",
                        "complete": True,
                        "data": data,
                        "error": None,
                        "ok": True,
                    },
                    api_key,
                )
                return 0

            else:
                # Raw patch stream
                meta = {
                    "activity": cand["activity"],
                    "artifactIndex": cand["artifactIndex"],
                    "baseCommitId": cand["baseCommitId"],
                    "createTime": cand["createTime"],
                    "patchSha256": cand["patchSha256"],
                    "sessionName": cand["sessionName"],
                    "source": cand["source"],
                    "suggestedCommitMessage": cand["suggestedCommitMessage"],
                }
                emit_json(sys.stderr, meta, api_key)
                try:
                    sys.stdout.buffer.write(patch_text.encode("utf-8"))
                    sys.stdout.buffer.flush()
                except BrokenPipeError:
                    return 4
                except Exception:
                    return 4
                return 0

        elif action == "teleport":
            sess = read_session(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            acts, complete, err = read_activities(
                args["session"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            if not complete:
                raise err or OctodotError(
                    error_record("protocol_error", "Failed to list activities for teleport", "teleport"),
                    exit_code=4,
                )

            cand, patch_text = select_patch(
                sess,
                acts,
                args["activity"],
                args["artifact"],
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )

            if not cand["baseCommitId"]:
                raise OctodotError(
                    error_record("base_missing", "Artifact has no baseCommitId for teleport", "teleport"),
                    exit_code=4,
                )

            q_src = safe_quote_resource_name(cand["source"])
            _, src_detail = request_json(
                "GET",
                f"/{q_src}",
                key=api_key,
                timeout=timeout,
                deadline_start=start_time,
                deadline=deadline,
            )
            gh = src_detail.get("githubRepo", {})

            dest_dir, branch_name = teleport(
                args["dir"],
                sess,
                patch_text.encode("utf-8"),
                cand["baseCommitId"],
                gh.get("owner", ""),
                gh.get("repo", ""),
                key=api_key,
            )

            data = {
                "applied": True,
                "artifactIndex": cand["artifactIndex"],
                "baseCommitId": cand["baseCommitId"],
                "branch": branch_name,
                "cwd": dest_dir,
                "patchSha256": cand["patchSha256"],
                "sessionName": cand["sessionName"],
                "source": cand["source"],
            }
            emit_json(
                sys.stdout,
                {
                    "action": "teleport",
                    "complete": True,
                    "data": data,
                    "error": None,
                    "ok": True,
                },
                api_key,
            )
            return 0

    except OctodotError as err:
        if action == "pull" and not (args.get("json") or args.get("apply")):
            # Raw pull failure before output: empty stdout, stderr error envelope only
            emit_json(
                sys.stderr,
                {
                    "action": action,
                    "complete": False,
                    "data": None,
                    "error": err.record,
                    "ok": False,
                },
                api_key,
            )
        else:
            emit_json(
                sys.stdout,
                {
                    "action": action,
                    "complete": False,
                    "data": None,
                    "error": err.record,
                    "ok": False,
                },
                api_key,
            )
            emit_json(sys.stderr, err.record, api_key)
        return err.exit_code

    return 0


if __name__ == "__main__":
    sys.exit(main())
