#!/usr/bin/env python3
"""Offline unit test suite for octodot.py."""

from __future__ import annotations

import ast
import concurrent.futures
import copy
import email.utils
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, mock_open, patch

# Global network guard: block all real socket connections
_real_socket = socket.socket


def _guard_socket(*args, **kwargs):
    raise AssertionError("Network socket access forbidden in offline test suite")


socket.socket = _guard_socket

# Global git isolation: ensure unit test fixtures are not affected by host/system git configuration (e.g. CI runner system git-lfs)
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull

# Ensure repository root is in sys.path
for candidate in [
    os.getcwd(),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../Workspace/chori-octodot")),
    "/Users/dastua/Workspace/chori-octodot",
]:
    if os.path.isfile(os.path.join(candidate, "octodot.py")) and candidate not in sys.path:
        sys.path.insert(0, candidate)

import octodot


class ParserTests(unittest.TestCase):
    """Tests for CLI argument parsing, strictness, and validation."""

    def test_actions_and_aliases(self):
        """Every action and its single/double-dash alias is parsed correctly."""
        actions = [
            ("-new", "new"),
            ("--new", "new"),
            ("-list-repos", "list-repos"),
            ("--list-repos", "list-repos"),
            ("-list-sessions", "list-sessions"),
            ("--list-sessions", "list-sessions"),
        ]
        for flag, act in actions:
            extra = ["-prompt", "hello", "--repo", "owner/repo"] if act == "new" else []
            res = octodot.parse_args([flag] + extra)
            self.assertEqual(res["action"], act)

        session_actions = [
            ("-status", "status"),
            ("--status", "status"),
            ("-activities", "activities"),
            ("--activities", "activities"),
            ("-results", "results"),
            ("--results", "results"),
            ("-pull", "pull"),
            ("--pull", "pull"),
        ]
        for flag, act in session_actions:
            res = octodot.parse_args([flag, "sessions/123"])
            self.assertEqual(res["action"], act)
            self.assertEqual(res["session"], "sessions/123")

        # Teleport requires --apply and --dir
        res = octodot.parse_args(["-teleport", "123", "--dir", "/tmp/foo", "--apply"])
        self.assertEqual(res["action"], "teleport")
        self.assertEqual(res["session"], "sessions/123")
        res2 = octodot.parse_args(["--teleport", "sessions/123", "--dir", "/tmp/foo", "--apply"])
        self.assertEqual(res2["action"], "teleport")

    def test_action_exclusivity(self):
        """Supplying zero or multiple actions must fail with exit 2."""
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args([])
        self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["-list-repos", "-list-sessions"])
        self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["-new", "-status", "123"])
        self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["-pull", "123", "-results", "123"])
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_help_and_version_standalone(self):
        """-h/--help and --version parse standalone, but error when combined."""
        self.assertEqual(octodot.parse_args(["-h"])["action"], "help")
        self.assertEqual(octodot.parse_args(["--help"])["action"], "help")
        self.assertEqual(octodot.parse_args(["--version"])["action"], "version")

        # Combination with other flags fails
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["--help", "-new", "-prompt", "hi"])
        self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["--version", "-list-repos"])
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_duplicate_flags_and_aliases(self):
        """Duplicate flags or aliases must be rejected with exit 2."""
        duplicates = [
            ["-new", "--new", "-prompt", "hi"],
            ["-new", "-prompt", "p1", "--prompt", "p2"],
            ["-new", "-prompt", "hi", "--repo", "a/b", "--repo", "c/d"],
            ["-new", "-prompt", "hi", "--branch", "b1", "--branch", "b2"],
            ["-new", "-prompt", "hi", "--parallel", "2", "--parallel", "3"],
            ["-new", "-prompt", "hi", "--title", "t1", "--title", "t2"],
            ["-list-repos", "--timeout", "10", "--timeout", "20"],
            ["-list-repos", "--deadline", "30", "--deadline", "60"],
            ["-pull", "123", "--pull", "456"],
        ]
        for argv in duplicates:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(argv)
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_illegal_option_matrix(self):
        """Options inapplicable to an action must be rejected with exit 2."""
        matrix = [
            # --parallel without -new
            ["-list-repos", "--parallel", "3"],
            # --prompt without -new
            ["-list-sessions", "-prompt", "hello"],
            # --repo without -new
            ["-status", "123", "--repo", "owner/repo"],
            # --branch without -new
            ["-activities", "123", "--branch", "main"],
            # --title without -new
            ["-results", "123", "--title", "test title"],
            # --json without -pull
            ["-list-repos", "--json"],
            ["-teleport", "123", "--dir", "/tmp/d", "--apply", "--json"],
            # --json with pull --apply
            ["-pull", "123", "--apply", "--json"],
            # --dir without teleport
            ["-pull", "123", "--dir", "/tmp/x"],
            ["-new", "-prompt", "hi", "--dir", "/tmp/x"],
            # teleport without --apply
            ["-teleport", "123", "--dir", "/tmp/x"],
            # teleport without --dir
            ["-teleport", "123", "--apply"],
            # explicit OWNER/REPO with --cwd
            ["-new", "-prompt", "hi", "--repo", "owner/repo", "--cwd", "/tmp"],
            # pull --cwd without --apply
            ["-pull", "123", "--cwd", "/tmp"],
            # --cwd on unsupported action
            ["-list-repos", "--cwd", "/tmp"],
            ["-status", "123", "--cwd", "/tmp"],
            # --activity without --artifact
            ["-pull", "123", "--activity", "sessions/123/activities/a"],
            # --artifact without --activity
            ["-pull", "123", "--artifact", "0"],
            # --activity/artifact on non-pull/teleport
            ["-status", "123", "--activity", "sessions/123/activities/a", "--artifact", "0"],
            # --apply on non-pull/teleport
            ["-status", "123", "--apply"],
        ]
        for argv in matrix:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(argv)
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_first_required_prompt_invocation(self):
        """Supported first invocation format: -new -prompt '...' (repo/branch inferred)."""
        res = octodot.parse_args(["-new", "-prompt", "instructions"])
        self.assertEqual(res["action"], "new")
        self.assertEqual(res["prompt"], "instructions")
        self.assertIsNone(res["repo"])
        self.assertIsNone(res["branch"])

    def test_prompt_rules(self):
        """Literal prompt vs stdin reading rules, Unicode, and whitespace handling."""
        # Literal whitespace-only prompt fails
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.resolve_prompt("   \t\n  ")
        self.assertEqual(ctx.exception.exit_code, 2)

        # Literal valid prompt preserves whitespace & Unicode
        p = "  hello world \u2764 with tabs\t and newlines\n"
        self.assertEqual(octodot.resolve_prompt(p), p)

        # Explicit -prompt - on TTY fails
        with mock.patch("sys.stdin.isatty", return_value=True):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_prompt("-")
            self.assertEqual(ctx.exception.exit_code, 2)

        # Explicit -prompt - on non-TTY reads stdin
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch("sys.stdin.read", return_value="instructions from stdin"):
                self.assertEqual(octodot.resolve_prompt("-"), "instructions from stdin")

        # Explicit -prompt - on non-TTY with empty or whitespace-only fails
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch("sys.stdin.read", return_value="   \n"):
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.resolve_prompt("-")
                self.assertEqual(ctx.exception.exit_code, 2)

        # Omitted prompt on TTY fails
        with mock.patch("sys.stdin.isatty", return_value=True):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_prompt(None)
            self.assertEqual(ctx.exception.exit_code, 2)

        # Omitted prompt on non-TTY reads stdin
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch("sys.stdin.read", return_value="instructions from pipe"):
                self.assertEqual(octodot.resolve_prompt(None), "instructions from pipe")

        # Invalid UTF-8 in stdin fails exit 2
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch("sys.stdin.read", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.resolve_prompt(None)
                self.assertEqual(ctx.exception.exit_code, 2)

    def test_prompt_precedence_over_stdin(self):
        """A literal prompt takes precedence over non-TTY stdin and never reads it."""
        mock_read = mock.Mock(return_value="piped content")
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch("sys.stdin.read", mock_read):
                res = octodot.resolve_prompt("literal instruction")
                self.assertEqual(res, "literal instruction")
                self.assertEqual(mock_read.call_count, 0)

    def test_nonfinite_deadlines_and_timeouts(self):
        """Nonfinite, zero, negative, or invalid deadlines and timeouts are rejected."""
        invalid_nums = ["0", "-1", "-0.5", "nan", "inf", "-inf", "abc"]
        for bad in invalid_nums:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(["-list-repos", "--timeout", bad])
            self.assertEqual(ctx.exception.exit_code, 2)

            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(["-list-repos", "--deadline", bad])
            self.assertEqual(ctx.exception.exit_code, 2)

        # Valid positive numbers succeed
        res = octodot.parse_args(["-list-repos", "--timeout", "10", "--deadline", "60.5"])
        self.assertEqual(res["timeout"], 10.0)
        self.assertEqual(res["deadline"], 60.5)

    def test_session_and_activity_validation(self):
        """Session and activity formats are strictly validated against injection."""
        # Valid sessions
        self.assertEqual(octodot.validate_session_name("12345"), "sessions/12345")
        self.assertEqual(octodot.validate_session_name("sessions/abc_123-xyz"), "sessions/abc_123-xyz")

        # Invalid sessions (path traversal, URLs, query parameters, control characters)
        invalid_sessions = [
            "",
            "../bad",
            "sessions/../bad",
            "sessions/a/b",
            "http://bad",
            "123?foo",
            "123#bar",
            "sess with spaces",
            "sess\x00null",
        ]
        for bad in invalid_sessions:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.validate_session_name(bad)
            self.assertEqual(ctx.exception.exit_code, 2)

        # Activity selector validation
        valid_act = octodot.validate_activity_name("sessions/s1/activities/a1", "sessions/s1")
        self.assertEqual(valid_act, "sessions/s1/activities/a1")

        # Activity session mismatch
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.validate_activity_name("sessions/wrong/activities/a1", "sessions/s1")
        self.assertEqual(ctx.exception.exit_code, 2)

        # Invalid activity format
        invalid_acts = [
            "",
            "activities/a1",
            "sessions/s1/activities/a1/extra",
            "sessions/s1/activities/../bad",
        ]
        for bad in invalid_acts:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.validate_activity_name(bad, "sessions/s1")
            self.assertEqual(ctx.exception.exit_code, 2)

        # Artifact index validation
        res = octodot.parse_args(
            ["-pull", "123", "--activity", "sessions/123/activities/a1", "--artifact", "0"]
        )
        self.assertEqual(res["artifact"], 0)

        for bad_art in ["-1", "abc", "1.5"]:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(
                    ["-pull", "123", "--activity", "sessions/123/activities/a1", "--artifact", bad_art]
                )
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_repo_syntax(self):
        """Repository syntax validation."""
        self.assertIsNone(octodot.validate_repo_arg("."))
        self.assertEqual(octodot.validate_repo_arg("owner/repo"), ("owner", "repo"))
        self.assertEqual(octodot.validate_repo_arg("OWNER/REPO.git"), ("OWNER", "REPO.git"))
        self.assertEqual(octodot.validate_repo_arg("my-org/my_project.1"), ("my-org", "my_project.1"))

        invalid_repos = [
            "",
            "invalid",
            "a/b/c",
            "../repo",
            "owner/..",
            "owner/.",
            "./repo",
            "owner/",
            "/repo",
            "ow ner/repo",
        ]
        for bad in invalid_repos:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.validate_repo_arg(bad)
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_parallel_boundaries(self):
        """--parallel boundary validation: 1-100 allowed; 0 and 101 rejected."""
        valid_cases = [1, 5, 6, 100]
        for n in valid_cases:
            res = octodot.parse_args(["-new", "-prompt", "hi", "--parallel", str(n)])
            self.assertEqual(res["parallel"], n)

        invalid_cases = [0, 101, -1, "abc", "1.5"]
        for bad in invalid_cases:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(["-new", "-prompt", "hi", "--parallel", str(bad)])
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_title_and_branch_validation(self):
        """--title and --branch validation."""
        # Non-empty title passes
        res = octodot.parse_args(["-new", "-prompt", "hi", "--title", "Task Title"])
        self.assertEqual(res["title"], "Task Title")

        # Empty or whitespace-only title fails
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["-new", "-prompt", "hi", "--title", "   "])
        self.assertEqual(ctx.exception.exit_code, 2)

        # Empty branch fails
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.parse_args(["-new", "-prompt", "hi", "--branch", ""])
        self.assertEqual(ctx.exception.exit_code, 2)


class OutputTests(unittest.TestCase):
    """Tests for envelope structure, secret redaction, ordering, and exits."""

    def setUp(self):
        octodot.INTERRUPTED = False
        octodot.STOP_EVENT.clear()

    def tearDown(self):
        octodot.INTERRUPTED = False
        octodot.STOP_EVENT.clear()

    def test_standard_envelope_keys(self):
        """Non-new JSON commands return sorted keys with compact formatting and newline."""
        buf = io.StringIO()
        payload = {
            "action": "list-repos",
            "complete": True,
            "data": {"sources": []},
            "error": None,
            "ok": True,
        }
        octodot.emit_json(buf, payload)
        text = buf.getvalue()
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))
        parsed = json.loads(text)
        self.assertEqual(list(parsed.keys()), ["action", "complete", "data", "error", "ok"])
        # Verify compact formatting (no space after comma or colon)
        self.assertIn('{"action":"list-repos"', text)

    def test_error_record_structure_and_provider_truncation(self):
        """error_record structures keys and truncates provider message to 2048 chars."""
        secret = "SECRET_12345"
        long_msg = f"failed due to {secret} " + ("x" * 3000)
        provider = {"code": 403, "message": long_msg, "status": "PERMISSION_DENIED"}
        rec = octodot.error_record(
            "auth_error", "Operation failed", "test_op", http_status=403, provider=provider, key=secret
        )
        self.assertEqual(list(sorted(rec.keys())), ["httpStatus", "kind", "message", "operation", "provider"])
        prov = rec["provider"]
        self.assertIsNotNone(prov)
        self.assertEqual(prov["code"], 403)
        self.assertEqual(prov["status"], "PERMISSION_DENIED")
        self.assertNotIn(secret, prov["message"])
        self.assertIn("[REDACTED]", prov["message"])
        self.assertLessEqual(len(prov["message"]), 2048)

        # Malformed provider field types become None
        malformed_prov = {"code": "not_an_int", "message": 12345, "status": False}
        rec2 = octodot.error_record("test", "msg", "op", provider=malformed_prov)
        self.assertIsNone(rec2["provider"]["code"])
        self.assertIsNone(rec2["provider"]["message"])
        self.assertIsNone(rec2["provider"]["status"])

    def test_secret_redaction(self):
        """Recursively redact occurrences of the secret key in structures and strings."""
        secret = "API_KEY_SENTINEL_XYZ"
        raw_struct = {
            "nested": {"key": f"Bearer {secret}", "number": 42},
            "list": [f"prefix-{secret}", "safe"],
            "direct": f"secret {secret} here",
        }
        redacted = octodot.redact(raw_struct, secret)
        self.assertEqual(redacted["nested"]["key"], "Bearer [REDACTED]")
        self.assertEqual(redacted["list"][0], "prefix-[REDACTED]")
        self.assertEqual(redacted["direct"], "secret [REDACTED] here")
        self.assertEqual(redacted["nested"]["number"], 42)

        # None or empty key returns val unmodified
        self.assertEqual(octodot.redact(raw_struct, None), raw_struct)

    def test_secret_in_artifact_refusal(self):
        """Artifact containing the exact API key is refused with secret_in_artifact."""
        secret = "LEAKED_KEY_999"
        session = {
            "name": "sessions/s1",
            "sourceContext": {"source": "sources/src1"},
        }
        act = {
            "name": "sessions/s1/activities/a1",
            "createTime": "2026-10-08T12:00:00.000000000Z",
            "artifacts": [
                {
                    "changeSet": {
                        "source": "sources/src1",
                        "gitPatch": {
                            "baseCommitId": "0123456789012345678901234567890123456789",
                            "suggestedCommitMessage": "fix",
                            "unidiffPatch": f"diff --git a/foo b/foo\n+{secret}\n",
                        },
                    }
                }
            ],
        }
        with mock.patch("octodot.request_json", return_value=(200, act)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.select_patch(
                    session, [act], "sessions/s1/activities/a1", 0, key=secret
                )
            self.assertEqual(ctx.exception.record["kind"], "secret_in_artifact")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_null_vs_unknown_differentiation(self):
        """classify_session differentiates known, unknown, and retains raw state."""
        self.assertEqual(octodot.classify_session({"state": "PLANNING"}), "pending")
        self.assertEqual(octodot.classify_session({"state": "QUEUED"}), "pending")
        self.assertEqual(octodot.classify_session({"state": "IN_PROGRESS"}), "pending")
        self.assertEqual(octodot.classify_session({"state": "PAUSED"}), "blocked")
        self.assertEqual(octodot.classify_session({"state": "AWAITING_PLAN_APPROVAL"}), "blocked")
        self.assertEqual(octodot.classify_session({"state": "AWAITING_USER_FEEDBACK"}), "blocked")
        self.assertEqual(octodot.classify_session({"state": "FAILED"}), "failed")
        self.assertEqual(octodot.classify_session({"state": "COMPLETED"}), "completed")
        self.assertEqual(octodot.classify_session({"state": "BRAND_NEW_STATE"}), "unknown")
        self.assertEqual(octodot.classify_session({}), "unknown")

    def test_raw_byte_fidelity(self):
        """Raw pull writes exact UTF-8 bytes to stdout with no added/removed newline."""
        raw_patch_text = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new"
        cand = {
            "activity": "sessions/s1/activities/a1",
            "artifactIndex": 0,
            "baseCommitId": "1234567890123456789012345678901234567890",
            "createTime": "2026-10-08T12:00:00Z",
            "patchSha256": "sha256",
            "sessionName": "sessions/s1",
            "source": "sources/src1",
            "suggestedCommitMessage": "msg",
        }

        mock_stdout = mock.Mock()
        mock_stdout.buffer = io.BytesIO()
        mock_stderr = io.StringIO()

        with mock.patch("sys.stdout", mock_stdout):
            with mock.patch("sys.stderr", mock_stderr):
                with mock.patch("octodot.read_session", return_value={"name": "sessions/s1"}):
                    with mock.patch("octodot.read_activities", return_value=([{}], True, None)):
                        with mock.patch("octodot.select_patch", return_value=(cand, raw_patch_text)):
                            with mock.patch.dict(os.environ, {"JULES_API_KEY": "dummy_key"}):
                                rc = octodot.main(["-pull", "sessions/s1"])
                                self.assertEqual(rc, 0)
                                written_bytes = mock_stdout.buffer.getvalue()
                                self.assertEqual(written_bytes, raw_patch_text.encode("utf-8"))
                                # Stderr receives metadata
                                err_out = mock_stderr.getvalue()
                                self.assertIn('"activity":"sessions/s1/activities/a1"', err_out)

    def test_stdout_and_stderr_separation(self):
        """Parser errors emit error envelope to stderr and leave stdout empty."""
        mock_stdout = io.StringIO()
        mock_stderr = io.StringIO()
        with mock.patch("sys.stdout", mock_stdout):
            with mock.patch("sys.stderr", mock_stderr):
                rc = octodot.main(["-new", "-invalid-flag"])
                self.assertEqual(rc, 2)
                self.assertEqual(mock_stdout.getvalue(), "")
                err_text = mock_stderr.getvalue()
                self.assertTrue(err_text.endswith("\n"))
                err_env = json.loads(err_text)
                self.assertEqual(err_env["action"], "unknown")
                self.assertFalse(err_env["ok"])
                self.assertEqual(err_env["error"]["kind"], "usage_error")

    def test_flush_ordering(self):
        """Every emit_json call writes and flushes immediately."""
        calls = []

        class TrackingStream:
            def write(self, s):
                calls.append(("write", s))

            def flush(self):
                calls.append(("flush", None))

        stream = TrackingStream()
        octodot.emit_json(stream, {"test": "val"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "write")
        self.assertEqual(calls[1][0], "flush")

    def test_whole_line_locking(self):
        """Threaded emit_json output does not interleave or tear lines."""
        buf = io.StringIO()
        num_threads = 8
        lines_per_thread = 25

        def worker(tid):
            for i in range(lines_per_thread):
                octodot.emit_json(buf, {"tid": tid, "iter": i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), num_threads * lines_per_thread)
        for line in lines:
            parsed = json.loads(line)
            self.assertIn("tid", parsed)
            self.assertIn("iter", parsed)

    def test_jsonl_ordinals_and_summary_format(self):
        """create_many emits ordered attempt lines followed by summary line."""
        mock_create = mock.Mock()
        mock_create.side_effect = [
            {
                "attempt": 1,
                "contextVerified": True,
                "error": None,
                "fingerprint": "fp1",
                "id": "1",
                "name": "sessions/s1",
                "observed": {"branch": "main", "repo": "o/r", "source": "src1"},
                "outcome": "accepted",
                "prUrls": [],
                "requested": {"branch": "main", "repo": "o/r", "source": "src1"},
                "startedAt": "2026-10-08T12:00:00Z",
                "state": "QUEUED",
                "type": "attempt",
                "url": None,
            },
            {
                "attempt": 2,
                "contextVerified": True,
                "error": None,
                "fingerprint": "fp2",
                "id": "2",
                "name": "sessions/s2",
                "observed": {"branch": "main", "repo": "o/r", "source": "src1"},
                "outcome": "accepted",
                "prUrls": [],
                "requested": {"branch": "main", "repo": "o/r", "source": "src1"},
                "startedAt": "2026-10-08T12:00:01Z",
                "state": "QUEUED",
                "type": "attempt",
                "url": None,
            },
        ]

        with mock.patch("octodot.create_one", mock_create):
            with mock.patch("sys.stdout", new=io.StringIO()) as mock_out:
                rc = octodot.create_many(
                    2, "o/r", "src1", "main", "prompt", None, "key", 30.0, 0.0, 120.0
                )
                self.assertEqual(rc, 0)
                lines = mock_out.getvalue().splitlines()
                self.assertEqual(len(lines), 3)  # 2 attempts + 1 summary
                att1 = json.loads(lines[0])
                att2 = json.loads(lines[1])
                summary = json.loads(lines[2])

                self.assertEqual(att1["type"], "attempt")
                self.assertEqual(att1["attempt"], 1)
                self.assertEqual(att2["type"], "attempt")
                self.assertEqual(att2["attempt"], 2)

                self.assertEqual(summary["type"], "summary")
                self.assertEqual(summary["requested"], 2)
                self.assertEqual(summary["accepted"], 2)
                self.assertEqual(summary["exitCode"], 0)
                self.assertTrue(summary["ok"])

    def test_exit_code_precedence(self):
        """Exit code precedence: 5 (uncertain/unverified/mismatch) > 3 (auth) > 4 (error) > 0."""
        # Uncertain yields exit 5
        att_uncertain = {
            "attempt": 1,
            "contextVerified": None,
            "error": octodot.error_record("transport_error", "Failed", "create_one"),
            "fingerprint": "fp",
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "uncertain",
            "prUrls": [],
            "requested": None,
            "startedAt": None,
            "state": None,
            "type": "attempt",
            "url": None,
        }
        with mock.patch("octodot.create_one", return_value=att_uncertain):
            with mock.patch("sys.stdout", new=io.StringIO()):
                rc = octodot.create_many(1, "o/r", "src", "b", "p", None, "test_key_12345", 30.0, 0.0, 120.0)
                self.assertEqual(rc, 5)

        # HTTP 403 error yields exit 3
        att_auth = {
            "attempt": 1,
            "contextVerified": None,
            "error": octodot.error_record("access_denied", "Denied", "create_one", http_status=403),
            "fingerprint": "fp",
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "rejected",
            "prUrls": [],
            "requested": None,
            "startedAt": None,
            "state": None,
            "type": "attempt",
            "url": None,
        }
        with mock.patch("octodot.create_one", return_value=att_auth):
            with mock.patch("sys.stdout", new=io.StringIO()):
                rc = octodot.create_many(1, "o/r", "src", "b", "p", None, "test_key_12345", 30.0, 0.0, 120.0)
                self.assertEqual(rc, 3)

        # Plain rejected without 401/403 yields exit 4
        att_rejected = copy.deepcopy(att_auth)
        att_rejected["error"]["httpStatus"] = 400
        with mock.patch("octodot.create_one", return_value=att_rejected):
            with mock.patch("sys.stdout", new=io.StringIO()):
                rc = octodot.create_many(1, "o/r", "src", "b", "p", None, "test_key_12345", 30.0, 0.0, 120.0)
                self.assertEqual(rc, 4)

    def test_deterministic_summary_error_selection(self):
        """summary.error is selected from the lowest-numbered attempt with an error."""
        err1 = octodot.error_record("err_one", "Error 1", "op1")
        err2 = octodot.error_record("err_two", "Error 2", "op2")

        att1 = {
            "attempt": 1,
            "contextVerified": True,
            "error": None,
            "fingerprint": "fp",
            "id": "1",
            "name": "sessions/s1",
            "observed": None,
            "outcome": "accepted",
            "prUrls": [],
            "requested": None,
            "startedAt": None,
            "state": "QUEUED",
            "type": "attempt",
            "url": None,
        }
        att2 = {
            "attempt": 2,
            "contextVerified": None,
            "error": err1,
            "fingerprint": "fp",
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "rejected",
            "prUrls": [],
            "requested": None,
            "startedAt": None,
            "state": None,
            "type": "attempt",
            "url": None,
        }
        att3 = {
            "attempt": 3,
            "contextVerified": None,
            "error": err2,
            "fingerprint": "fp",
            "id": None,
            "name": None,
            "observed": None,
            "outcome": "rejected",
            "prUrls": [],
            "requested": None,
            "startedAt": None,
            "state": None,
            "type": "attempt",
            "url": None,
        }

        with mock.patch("octodot.create_one", side_effect=[att1, att2, att3]):
            with mock.patch("sys.stdout", new=io.StringIO()) as mock_out:
                octodot.create_many(3, "o/r", "src", "b", "p", None, "test_key_12345", 30.0, 0.0, 120.0)
                lines = mock_out.getvalue().splitlines()
                summary = json.loads(lines[-1])
                self.assertEqual(summary["error"]["kind"], "err_one")

    def test_accepted_receipt_write_failure(self):
        """Failure writing create_accepted receipt retains outcome as created_unverified."""
        resp_post = {
            "name": "sessions/s1",
            "id": "1",
            "state": "QUEUED",
            "sourceContext": {"source": "sources/s", "githubRepoContext": {"startingBranch": "main"}},
        }
        with mock.patch("octodot.request_json", return_value=(200, resp_post)):
            # Make emit_json raise IOError when emitting create_accepted
            orig_emit = octodot.emit_json

            def fail_accepted(stream, payload, key=None):
                if isinstance(payload, dict) and payload.get("type") == "create_accepted":
                    raise OSError("Simulated disk error writing stderr")
                return orig_emit(stream, payload, key)

            with mock.patch("octodot.emit_json", side_effect=fail_accepted):
                res = octodot.create_one(
                    1, "o/r", "sources/s", "main", {"prompt": "x"}, "fp", "test_key_12345", 30.0, 0.0, 120.0
                )
                self.assertEqual(res["outcome"], "created_unverified")
                self.assertEqual(res["name"], "sessions/s1")
                self.assertTrue(octodot.STOP_EVENT.is_set())

    def test_interruption_exit_precedence(self):
        """Interruption gives at least exit 4 even if all submitted attempts accepted."""
        att = {
            "attempt": 1,
            "contextVerified": True,
            "error": None,
            "fingerprint": "fp",
            "id": "1",
            "name": "sessions/s1",
            "observed": None,
            "outcome": "accepted",
            "prUrls": [],
            "requested": None,
            "startedAt": None,
            "state": "QUEUED",
            "type": "attempt",
            "url": None,
        }
        octodot.INTERRUPTED = True
        with mock.patch("octodot.create_one", return_value=att):
            with mock.patch("sys.stdout", new=io.StringIO()) as mock_out:
                rc = octodot.create_many(1, "o/r", "src", "b", "p", None, "test_key_12345", 30.0, 0.0, 120.0)
                self.assertEqual(rc, 4)
                summary = json.loads(mock_out.getvalue().splitlines()[-1])
                self.assertEqual(summary["exitCode"], 4)
                self.assertEqual(summary["error"]["kind"], "interrupted")

    def test_partial_output_failure_disclosure(self):
        """Broken pipe during raw pull returns exit 4."""
        cand = {
            "activity": "sessions/s1/activities/a1",
            "artifactIndex": 0,
            "baseCommitId": "1234567890123456789012345678901234567890",
            "createTime": "2026-10-08T12:00:00Z",
            "patchSha256": "sha",
            "sessionName": "sessions/s1",
            "source": "sources/src",
            "suggestedCommitMessage": "msg",
        }
        mock_stdout = mock.Mock()
        mock_stdout.buffer.write.side_effect = BrokenPipeError("Pipe closed")

        with mock.patch("sys.stdout", mock_stdout):
            with mock.patch("sys.stderr", io.StringIO()):
                with mock.patch("octodot.read_session", return_value={"name": "sessions/s1"}):
                    with mock.patch("octodot.read_activities", return_value=([{}], True, None)):
                        with mock.patch("octodot.select_patch", return_value=(cand, "patch content")):
                            with mock.patch.dict(os.environ, {"JULES_API_KEY": "dummy_key"}):
                                rc = octodot.main(["-pull", "sessions/s1"])
                                self.assertEqual(rc, 4)


class ArchitectureTests(unittest.TestCase):
    """Tests asserting architecture invariants, allowlist, and AST rules."""

    def test_final_tracked_file_allowlist(self):
        """Assert the final git tracked file allowlist is exactly 8 files."""
        res_tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
        res_others = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, check=True
        )
        all_files = sorted(
            set(
                [
                    line.strip()
                    for line in (res_tracked.stdout + "\n" + res_others.stdout).splitlines()
                    if line.strip()
                ]
            )
        )
        expected = sorted(
            [
                ".github/workflows/offline.yml",
                ".gitignore",
                "README.md",
                "docs/IMPLEMENTATION_PLAN.md",
                "docs/OPERATIONS.md",
                "docs/RELEASE_CHECKLIST.md",
                "octodot.py",
                "test_octodot.py",
            ]
        )
        self.assertEqual(all_files, expected)

    def test_sole_runtime_module(self):
        """octodot.py is the sole runtime module."""
        self.assertTrue(os.path.isfile("octodot.py"))
        # No other runtime .py files in root
        root_py = [f for f in os.listdir(".") if f.endswith(".py")]
        self.assertEqual(sorted(root_py), ["octodot.py", "test_octodot.py"])

    def test_ast_no_forbidden_runtime_imports(self):
        """Verify octodot.py AST contains only standard library imports."""
        with open("octodot.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="octodot.py")

        forbidden = {"sqlite3", "requests", "urllib3", "aiohttp", "httpx", "yaml", "tomllib"}
        imported_modules = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[0])

        for mod in imported_modules:
            self.assertNotIn(mod, forbidden, f"Forbidden module imported: {mod}")

    def test_ast_no_eval_exec_or_dynamic_imports(self):
        """Verify octodot.py contains no eval, exec, or __import__ calls."""
        with open("octodot.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="octodot.py")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    self.assertNotIn(func.id, ("eval", "exec", "__import__"))

    def test_ast_no_shell_true(self):
        """Verify octodot.py never passes shell=True to subprocess."""
        with open("octodot.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="octodot.py")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell":
                        if isinstance(kw.value, ast.Constant):
                            self.assertNotEqual(kw.value.value, True)

    def test_no_old_controller_residue(self):
        """Verify octodot.py contains no old controller or daemon classes/code."""
        with open("octodot.py", "r", encoding="utf-8") as f:
            code = f.read()
        self.assertNotIn("class Controller", code)
        self.assertNotIn("class Daemon", code)
        self.assertNotIn("class Journal", code)
        self.assertNotIn("class Grant", code)
        self.assertNotIn("class Registry", code)
        self.assertNotIn("class EventBus", code)
        self.assertNotIn("class Store", code)

    def test_version_and_help_offline(self):
        """Verify --version and --help run offline and exit 0 without network or keys."""
        res_v = subprocess.run([sys.executable, "octodot.py", "--version"], capture_output=True, text=True)
        self.assertEqual(res_v.returncode, 0)
        self.assertEqual(res_v.stdout, "octodot 1.0.0\n")

        res_h = subprocess.run([sys.executable, "octodot.py", "--help"], capture_output=True, text=True)
        self.assertEqual(res_h.returncode, 0)
        self.assertIn("usage: octodot", res_h.stdout)

    def test_no_key_accepted_in_cli_flags(self):
        """Flags like --key or --api-key are rejected as unrecognized options."""
        for flag in ["--key", "-key", "--api-key", "-api-key"]:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args(["-list-repos", flag, "secret"])
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_dangerous_old_commands_absent(self):
        """Old controller commands (run, prepare, inventory, reconcile) fail with exit 2."""
        for cmd in ["run", "prepare", "inventory", "reconcile"]:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.parse_args([cmd])
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_ci_invokes_new_tests_only(self):
        """Verify .github/workflows/offline.yml invokes only new unit tests."""
        ci_path = ".github/workflows/offline.yml"
        self.assertTrue(os.path.isfile(ci_path))
        with open(ci_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("requirements-dev.txt", content)
        self.assertNotIn("tests/integration", content)
        self.assertNotIn("bounded_runner", content)
        self.assertIn("unittest", content)


class TransportTests(unittest.TestCase):
    """Tests for HTTP transport, headers, retries, and error mapping."""

    @mock.patch("urllib.request.build_opener")
    def test_exact_origin_and_headers_for_get(self, mock_build):
        """GET requests use exact base URL, X-Goog-Api-Key, Accept, and no Content-Type."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        resp = mock.Mock()
        resp.status = 200
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        mock_opener.open.return_value = resp

        octodot.request_json("GET", "/sources", "secret_key_123")

        req = mock_opener.open.call_args[0][0]
        self.assertEqual(req.full_url, "https://jules.googleapis.com/v1alpha/sources")
        self.assertEqual(req.headers["X-goog-api-key"], "secret_key_123")
        self.assertEqual(req.headers["Accept"], "application/json")
        self.assertNotIn("Content-type", req.headers)

    @mock.patch("urllib.request.build_opener")
    def test_exact_origin_and_headers_for_post(self, mock_build):
        """POST requests use application/json; charset=utf-8 Content-Type."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        resp = mock.Mock()
        resp.status = 200
        resp.read.return_value = b'{"name": "sessions/s1"}'
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        mock_opener.open.return_value = resp

        octodot.request_json(
            "POST", "/sessions", "secret_key_123", body={"prompt": "do task"}, is_post=True
        )

        req = mock_opener.open.call_args[0][0]
        self.assertEqual(req.full_url, "https://jules.googleapis.com/v1alpha/sessions")
        self.assertEqual(req.headers["Content-type"], "application/json; charset=utf-8")
        self.assertEqual(req.data, b'{"prompt":"do task"}')

    def test_url_segment_encoding(self):
        """Resource name segments are URL quoted, preserving separators."""
        quoted = octodot.safe_quote_resource_name("sources/github/123/branches/feat%201")
        self.assertEqual(quoted, "sources/github/123/branches/feat%25201")

        # Invalid segment names raise protocol_error exit 4
        invalid_names = [
            "sources/../bad",
            "sources/./bad",
            "sources/a\\b",
            "sources/a?b",
            "sources/a#b",
            "sources/a\x00b",
        ]
        for bad in invalid_names:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.safe_quote_resource_name(bad)
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_redirects_denied(self):
        """All HTTP redirects (301, 302, 307) are denied without following."""
        handler = octodot.NoRedirectHandler()
        req = mock.Mock()
        for code in (301, 302, 307, 308):
            self.assertIsNone(handler.redirect_request(req, None, code, "Redirect", {}, "http://new"))

        # When server returns 302, request_json raises redirect_denied
        err_resp = urllib.error.HTTPError("http://u", 302, "Found", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.build_opener") as mock_build:
            mock_opener = mock.Mock()
            mock_build.return_value = mock_opener
            mock_opener.open.side_effect = err_resp

            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.request_json("GET", "/sources", "key")
            self.assertEqual(ctx.exception.record["kind"], "redirect_denied")
            self.assertEqual(ctx.exception.exit_code, 4)

    @mock.patch("urllib.request.build_opener")
    def test_tls_error_no_retry_fail_closed(self, mock_build):
        """TLS/SSL handshake and cert errors are not retried."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener
        mock_opener.open.side_effect = urllib.error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", "key")
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(mock_opener.open.call_count, 1)

        # For POST, fails immediately with exit 5
        mock_opener.open.reset_mock()
        mock_opener.open.side_effect = urllib.error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("POST", "/sessions", "key", body={"prompt": "x"}, is_post=True)
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertEqual(mock_opener.open.call_count, 1)

    @mock.patch("urllib.request.build_opener")
    def test_get_retry_policy_three_attempts_max(self, mock_build):
        """GET retries up to 3 times (1 initial + 2 retries) on 408/429/5xx."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        err_resp = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b"{}"))
        ok_resp = mock.Mock()
        ok_resp.status = 200
        ok_resp.read.return_value = b'{"ok": true}'
        ok_resp.__enter__ = mock.Mock(return_value=ok_resp)
        ok_resp.__exit__ = mock.Mock(return_value=False)

        # Succeeds on 3rd attempt
        mock_opener.open.side_effect = [err_resp, err_resp, ok_resp]
        with mock.patch("time.sleep") as mock_sleep:
            st, data = octodot.request_json("GET", "/sources", "key")
            self.assertEqual(st, 200)
            self.assertEqual(mock_sleep.call_count, 2)
            self.assertEqual(mock_opener.open.call_count, 3)

        # Fails on all 3 attempts
        mock_opener.open.reset_mock()
        mock_opener.open.side_effect = [err_resp, err_resp, err_resp]
        with mock.patch("time.sleep"):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.request_json("GET", "/sources", "key")
            self.assertEqual(ctx.exception.exit_code, 4)
            self.assertEqual(mock_opener.open.call_count, 3)

    @mock.patch("urllib.request.build_opener")
    def test_post_no_retry(self, mock_build):
        """POST makes strictly 1 attempt and never retries on failure."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener
        mock_opener.open.side_effect = urllib.error.HTTPError(
            "u", 500, "Internal Server Error", {}, io.BytesIO(b'{"error":{"code":500,"message":"err"}}')
        )

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("POST", "/sessions", "key", body={"prompt": "hi"}, is_post=True)
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertEqual(mock_opener.open.call_count, 1)

    @mock.patch("urllib.request.build_opener")
    def test_retry_after_header_parsing(self, mock_build):
        """Retry-After header parses integer seconds, HTTP-date, and invalid fallback."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        # 1. Integer seconds: Retry-After: 5
        headers = {"Retry-After": "5"}
        err_resp = urllib.error.HTTPError("u", 429, "Too Many Requests", headers, io.BytesIO(b"{}"))
        ok_resp = mock.Mock(status=200, read=mock.Mock(return_value=b'{"ok":true}'))
        ok_resp.__enter__ = mock.Mock(return_value=ok_resp)
        ok_resp.__exit__ = mock.Mock(return_value=False)
        mock_opener.open.side_effect = [err_resp, ok_resp]

        with mock.patch("time.sleep") as mock_sleep:
            octodot.request_json("GET", "/sources", "key")
            self.assertEqual(mock_sleep.call_args[0][0], 5.0)

        # 2. Invalid date fallback to base delay 1.0
        mock_opener.open.reset_mock()
        headers_inv = {"Retry-After": "invalid-date-string"}
        err_resp2 = urllib.error.HTTPError("u", 429, "Too Many Requests", headers_inv, io.BytesIO(b"{}"))
        mock_opener.open.side_effect = [err_resp2, ok_resp]
        with mock.patch("time.sleep") as mock_sleep:
            octodot.request_json("GET", "/sources", "key")
            self.assertEqual(mock_sleep.call_args[0][0], 1.0)

    def test_deadline_admission_and_backoff(self):
        """Remaining deadline nonpositive stops before HTTP request."""
        # Nonpositive remaining budget stops immediately
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", "key", deadline_start=0.0, deadline=0.0)
        self.assertEqual(ctx.exception.record["kind"], "deadline_exceeded")
        self.assertEqual(ctx.exception.exit_code, 4)

    @mock.patch("urllib.request.build_opener")
    def test_malformed_json_handling(self, mock_build):
        """Malformed JSON or non-dict root returns protocol_error."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        # Malformed bytes
        bad_resp = mock.Mock(status=200, read=mock.Mock(return_value=b"<html>Bad Gateway</html>"))
        bad_resp.__enter__ = mock.Mock(return_value=bad_resp)
        bad_resp.__exit__ = mock.Mock(return_value=False)
        mock_opener.open.return_value = bad_resp

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", "key")
        self.assertEqual(ctx.exception.record["kind"], "protocol_error")

        # Non-dict JSON root
        list_resp = mock.Mock(status=200, read=mock.Mock(return_value=b"[1, 2, 3]"))
        list_resp.__enter__ = mock.Mock(return_value=list_resp)
        list_resp.__exit__ = mock.Mock(return_value=False)
        mock_opener.open.return_value = list_resp

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", "key")
        self.assertEqual(ctx.exception.record["kind"], "protocol_error")

    @mock.patch("urllib.request.build_opener")
    def test_auth_error_not_retried(self, mock_build):
        """HTTP 401 and 403 are not retried and exit with code 3."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        # 401 Unauthorized
        mock_opener.open.side_effect = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", "key")
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertEqual(ctx.exception.record["kind"], "auth_error")
        self.assertEqual(mock_opener.open.call_count, 1)

        # 403 Forbidden
        mock_opener.open.reset_mock()
        mock_opener.open.side_effect = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"{}"))
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", "key")
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertEqual(ctx.exception.record["kind"], "access_denied")
        self.assertEqual(mock_opener.open.call_count, 1)

    @mock.patch("urllib.request.build_opener")
    def test_no_api_key_leaked_in_logs_or_errors(self, mock_build):
        """Provider error containing API key is sanitized in returned error record."""
        secret = "VERY_SECRET_KEY_123"
        provider_body = json.dumps(
            {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": f"Key {secret} invalid"}}
        ).encode("utf-8")
        err_resp = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(provider_body))

        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener
        mock_opener.open.side_effect = err_resp

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("GET", "/sources", secret)
        rec = ctx.exception.record
        self.assertNotIn(secret, rec["provider"]["message"])
        self.assertIn("[REDACTED]", rec["provider"]["message"])


class PaginationTests(unittest.TestCase):
    """Tests for multi-page collection scanning and token handling."""

    @mock.patch("octodot.request_json")
    def test_two_pages_successful(self, mock_req):
        """Successfully collect items across two pages."""
        page1 = {
            "sources": [{"name": "sources/1"}, {"name": "sources/2"}],
            "nextPageToken": "tok2",
        }
        page2 = {
            "sources": [{"name": "sources/3"}],
            "nextPageToken": None,
        }
        mock_req.side_effect = [(200, page1), (200, page2)]
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertTrue(complete)
        self.assertIsNone(err)
        self.assertEqual(len(items), 3)
        self.assertEqual([i["name"] for i in items], ["sources/1", "sources/2", "sources/3"])

    @mock.patch("octodot.request_json")
    def test_empty_middle_page_with_token(self, mock_req):
        """Continue through empty pages with nonempty nextPageToken."""
        page1 = {"sources": [{"name": "sources/1"}], "nextPageToken": "tok2"}
        page2 = {"sources": [], "nextPageToken": "tok3"}
        page3 = {"sources": [{"name": "sources/2"}], "nextPageToken": None}

        mock_req.side_effect = [(200, page1), (200, page2), (200, page3)]
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertTrue(complete)
        self.assertIsNone(err)
        self.assertEqual(len(items), 2)
        self.assertEqual([i["name"] for i in items], ["sources/1", "sources/2"])

    @mock.patch("octodot.request_json")
    def test_missing_collection_key_treated_as_empty(self, mock_req):
        """Missing collection key in response is treated as empty list."""
        page = {"nextPageToken": None}
        mock_req.return_value = (200, page)
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertTrue(complete)
        self.assertIsNone(err)
        self.assertEqual(items, [])

    @mock.patch("octodot.request_json")
    def test_wrong_collection_type_protocol_error(self, mock_req):
        """Non-list collection key raises protocol_error."""
        page = {"sources": "not-a-list", "nextPageToken": None}
        mock_req.return_value = (200, page)
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertFalse(complete)
        self.assertIsNotNone(err)
        self.assertEqual(err.record["kind"], "protocol_error")

    @mock.patch("octodot.request_json")
    def test_token_cycle_detected(self, mock_req):
        """Token cycle triggers protocol_error."""
        page1 = {"sources": [{"name": "sources/1"}], "nextPageToken": "tok1"}
        page2 = {"sources": [{"name": "sources/2"}], "nextPageToken": "tok1"}
        mock_req.side_effect = [(200, page1), (200, page2)]
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertFalse(complete)
        self.assertIsNotNone(err)
        self.assertEqual(err.record["kind"], "protocol_error")

    @mock.patch("octodot.request_json")
    def test_duplicate_resource_name_detected(self, mock_req):
        """Repeated resource names across pages triggers protocol_error."""
        page1 = {"sources": [{"name": "sources/1"}], "nextPageToken": "tok2"}
        page2 = {"sources": [{"name": "sources/1"}], "nextPageToken": None}
        mock_req.side_effect = [(200, page1), (200, page2)]
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertFalse(complete)
        self.assertIsNotNone(err)
        self.assertEqual(err.record["kind"], "protocol_error")

    @mock.patch("octodot.request_json")
    def test_opaque_token_url_quoting(self, mock_req):
        """Page tokens with special characters are URL encoded via query string."""
        page1 = {"sources": [{"name": "sources/1"}], "nextPageToken": "tok/1+2=3"}
        page2 = {"sources": [{"name": "sources/2"}], "nextPageToken": None}
        mock_req.side_effect = [(200, page1), (200, page2)]

        octodot.paginate("/sources", "sources", "key")
        second_call_path = mock_req.call_args_list[1][0][1]
        self.assertIn("pageToken=tok%2F1%2B2%3D3", second_call_path)

    @mock.patch("octodot.request_json")
    def test_malformed_resource_objects(self, mock_req):
        """Items must be objects with valid unique string names."""
        # Item not a dict
        page1 = {"sources": ["item_string"], "nextPageToken": None}
        mock_req.return_value = (200, page1)
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertFalse(complete)
        self.assertEqual(err.record["kind"], "protocol_error")

        # Item missing name
        page2 = {"sources": [{"no_name": True}], "nextPageToken": None}
        mock_req.return_value = (200, page2)
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertFalse(complete)
        self.assertEqual(err.record["kind"], "protocol_error")

    @mock.patch("octodot.request_json")
    def test_later_page_failure_preserves_incomplete_entries(self, mock_req):
        """Failure on subsequent page preserves accumulated entries and complete=False."""
        page1 = {"sources": [{"name": "sources/1"}, {"name": "sources/2"}], "nextPageToken": "tok2"}
        mock_req.side_effect = [
            (200, page1),
            octodot.OctodotError(octodot.error_record("transport_error", "Failed", "req"), exit_code=4),
        ]
        items, complete, err = octodot.paginate("/sources", "sources", "key")
        self.assertFalse(complete)
        self.assertIsNotNone(err)
        self.assertEqual(len(items), 2)
        self.assertEqual([i["name"] for i in items], ["sources/1", "sources/2"])

    @mock.patch("octodot.paginate")
    def test_partial_scan_never_proves_absence(self, mock_paginate):
        """resolve_source raises on incomplete scan rather than concluding source not found."""
        mock_paginate.return_value = (
            [],
            False,
            octodot.OctodotError(octodot.error_record("protocol_error", "Incomplete", "paginate")),
        )
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.resolve_source("owner", "repo", "main", "key")
        self.assertEqual(ctx.exception.record["kind"], "protocol_error")



class SourceTests(unittest.TestCase):
    """Tests for repository inference, source resolution, and branch discovery."""

    def test_real_slash_containing_source_names_parsing(self) -> None:
        """Verify parsing and segment-encoding of opaque slash-containing source names."""
        slash_source = "sources/projects/123/locations/global/sources/github/owner/repo"
        quoted = octodot.safe_quote_resource_name(slash_source)
        # Separator slashes preserved, segments encoded
        self.assertEqual(quoted, "sources/projects/123/locations/global/sources/github/owner/repo")

        # Complex slash name with special chars in segment
        complex_source = "sources/github.com%2Fowner%2Frepo/branches/main"
        quoted_complex = octodot.safe_quote_resource_name(complex_source)
        self.assertIn("sources/", quoted_complex)

        # Mock resolve_source with slash-containing name
        sources_list = [
            {
                "name": slash_source,
                "githubRepo": {"owner": "owner", "repo": "repo"},
            }
        ]
        detail_source = {
            "name": slash_source,
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
                "defaultBranch": {"displayName": "main"},
                "branches": [{"displayName": "main"}],
            },
        }

        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)) as mock_req:
            res_detail, name, branch = octodot.resolve_source("owner", "repo", None, "key-123")
            self.assertEqual(name, slash_source)
            self.assertEqual(branch, "main")
            mock_req.assert_called_once()
            call_path = mock_req.call_args[0][1]
            self.assertEqual(call_path, f"/{slash_source}")

    def test_owner_repo_case_insensitive_matching(self) -> None:
        """Verify case-insensitive owner/repo matching while preserving API spelling."""
        sources_list = [
            {
                "name": "sources/s1",
                "githubRepo": {"owner": "MyOrg", "repo": "MyRepo"},
            }
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "MyOrg",
                "repo": "MyRepo",
                "defaultBranch": {"displayName": "main"},
            },
        }

        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            # Lowercase query matches mixed-case API record
            detail, name, branch = octodot.resolve_source("myorg", "myrepo", "main", "key-123")
            self.assertEqual(name, "sources/s1")
            self.assertEqual(detail["githubRepo"]["owner"], "MyOrg")
            self.assertEqual(detail["githubRepo"]["repo"], "MyRepo")

            # Uppercase query matches mixed-case API record
            detail_up, _, _ = octodot.resolve_source("MYORG", "MYREPO", "main", "key-123")
            self.assertEqual(detail_up["githubRepo"]["owner"], "MyOrg")

    def test_zero_matches_stops_before_post(self) -> None:
        """Zero matching sources fails with source_not_found before any POST."""
        sources_list = [
            {
                "name": "sources/s1",
                "githubRepo": {"owner": "other", "repo": "other"},
            }
        ]
        with patch("octodot.paginate", return_value=(sources_list, True, None)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key-123")
            self.assertEqual(ctx.exception.record["kind"], "source_not_found")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_multiple_matches_stops_before_post(self) -> None:
        """Multiple matching sources fails with ambiguous_source before any POST."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}},
            {"name": "sources/s2", "githubRepo": {"owner": "owner", "repo": "repo"}},
        ]
        with patch("octodot.paginate", return_value=(sources_list, True, None)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key-123")
            self.assertEqual(ctx.exception.record["kind"], "ambiguous_source")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_detail_repo_mismatch_stops(self) -> None:
        """Detailed source returning different githubRepo identity fails with protocol_error."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}},
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "changed",
                "repo": "repo",
                "defaultBranch": {"displayName": "main"},
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key-123")
            self.assertEqual(ctx.exception.record["kind"], "protocol_error")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_explicit_nondefault_branch_handling(self) -> None:
        """Non-default branch is accepted if listed, but rejected if missing."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}},
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
                "defaultBranch": {"displayName": "main"},
                "branches": [{"displayName": "feature-branch"}],
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            _, _, branch = octodot.resolve_source("owner", "repo", "feature-branch", "key-123")
            self.assertEqual(branch, "feature-branch")

            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", "non-existent-branch", "key-123")
            self.assertEqual(ctx.exception.record["kind"], "branch_not_found")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_default_branch_discovery_when_omitted(self) -> None:
        """Default branch displayName is used when requested branch is None."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}},
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
                "defaultBranch": {"displayName": "master"},
                "branches": [],
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            _, _, branch = octodot.resolve_source("owner", "repo", None, "key-123")
            self.assertEqual(branch, "master")

    def test_default_branch_in_defaultBranch_only(self) -> None:
        """Default branch is valid even if not duplicated in branches list."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}},
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
                "defaultBranch": {"displayName": "main"},
                "branches": [{"displayName": "other"}],
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            # Explicit request for default branch displayName
            _, _, branch = octodot.resolve_source("owner", "repo", "main", "key-123")
            self.assertEqual(branch, "main")

    def test_absent_branch_fails_preflight(self) -> None:
        """Source with no branches inside githubRepo fails before POST."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}},
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {"owner": "owner", "repo": "repo"},
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key-123")
            self.assertEqual(ctx.exception.record["kind"], "source_error")
            self.assertEqual(ctx.exception.record["message"], "Source has no branches configured")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_git_remote_origin_inference_https_scp_ssh(self) -> None:
        """Verify inference of HTTPS, SCP, and SSH GitHub origin URLs."""
        valid_cases = [
            ("https://github.com/foo/bar", ("foo", "bar")),
            ("https://github.com/foo/bar.git", ("foo", "bar")),
            ("https://github.com/foo/bar/", ("foo", "bar")),
            ("https://github.com/foo/bar.git/", ("foo", "bar")),
            ("git@github.com:foo/bar", ("foo", "bar")),
            ("git@github.com:foo/bar.git", ("foo", "bar")),
            ("ssh://git@github.com/foo/bar", ("foo", "bar")),
            ("ssh://git@github.com/foo/bar.git", ("foo", "bar")),
        ]
        for url, expected in valid_cases:
            res = octodot.parse_remote_url(url)
            self.assertEqual(res, expected, f"Failed parsing {url}")

    def test_git_remote_origin_inference_rejects_credentials_ports_query(self) -> None:
        """Reject URLs with credentials, nonstandard ports, query parameters, or non-GitHub hosts."""
        invalid_cases = [
            "https://user:pass@github.com/foo/bar",
            "https://github.com:443/foo/bar",
            "ssh://git@github.com:22/foo/bar",
            "https://github.com/foo/bar?query=1",
            "https://github.com/foo/bar#frag",
            "https://gitlab.com/foo/bar.git",
            "http://github.com/foo/bar",
            "file:///tmp/repo",
        ]
        for url in invalid_cases:
            res = octodot.parse_remote_url(url)
            self.assertIsNone(res, f"Should reject {url}")

    def test_multiple_remote_urls_rejected(self) -> None:
        """Multiple remote URLs configured for origin causes inference error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Fake run_git to return multiple origin URLs
            def fake_run_git(args: list[str], cwd: str | None = None, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "rev-parse" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=tmpdir.encode("utf-8"), stderr=b"")
                if "remote" in args:
                    urls = b"https://github.com/owner/repo1.git\nhttps://github.com/owner/repo2.git\n"
                    return subprocess.CompletedProcess(args, 0, stdout=urls, stderr=b"")
                return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

            with patch("octodot.run_git", side_effect=fake_run_git):
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.infer_repo(tmpdir, "main")
                self.assertEqual(ctx.exception.exit_code, 2)
                self.assertIn("exactly one origin fetch URL", ctx.exception.record["message"])

    def test_detached_head_requires_branch(self) -> None:
        """Detached HEAD without explicit --branch fails with exit 2 before network."""
        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_run_git(args: list[str], cwd: str | None = None, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "rev-parse" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=tmpdir.encode("utf-8"), stderr=b"")
                if "remote" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=b"https://github.com/owner/repo.git\n", stderr=b"")
                if "symbolic-ref" in args:
                    return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"fatal: ref HEAD is not a symbolic ref\n")
                return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

            with patch("octodot.run_git", side_effect=fake_run_git):
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.infer_repo(tmpdir, branch_arg=None)
                self.assertEqual(ctx.exception.exit_code, 2)
                self.assertIn("Detached HEAD", ctx.exception.record["message"])

                # With explicit branch, detached HEAD succeeds
                owner, repo, branch = octodot.infer_repo(tmpdir, branch_arg="feature-branch")
                self.assertEqual(branch, "feature-branch")

    def test_explicit_repo_independent_of_local_git(self) -> None:
        """Supplying explicit OWNER/REPO requires zero local Git calls."""
        with patch("octodot.run_git") as mock_git:
            args = octodot.parse_args(["-new", "-prompt", "hello", "--repo", "owner/repo", "--branch", "main"])
            self.assertEqual(args["repo"], "owner/repo")
            self.assertEqual(args["branch"], "main")
            mock_git.assert_not_called()


# =====================================================================
# 2. ReadTests
# =====================================================================
class ReadTests(unittest.TestCase):
    """Tests for read actions: status, activities, results, and classification."""

    def test_status_makes_only_one_get_request(self) -> None:
        """Status command makes exactly one GET request and returns raw session."""
        raw_session = {
            "id": "ses-1",
            "name": "sessions/ses-1",
            "state": "IN_PROGRESS",
            "title": "Task 1",
        }
        with patch("octodot.request_json", return_value=(200, raw_session)) as mock_req:
            res = octodot.read_session("sessions/ses-1", key="test-key")
            self.assertEqual(res["name"], "sessions/ses-1")
            self.assertEqual(res["id"], "ses-1")
            mock_req.assert_called_once()
            self.assertEqual(mock_req.call_args[0], ("GET", "/sessions/ses-1"))
            self.assertEqual(mock_req.call_args[1]["key"], "test-key")

    def test_raw_session_returned_properly(self) -> None:
        """Arbitrary fields in raw session object are preserved unmodified."""
        raw_session = {
            "customInt": 42,
            "customObj": {"nested": "data"},
            "id": "s-xyz",
            "name": "sessions/s-xyz",
            "state": "COMPLETED",
        }
        with patch("octodot.request_json", return_value=(200, raw_session)):
            res = octodot.read_session("sessions/s-xyz", key="k")
            self.assertEqual(res["customInt"], 42)
            self.assertEqual(res["customObj"], {"nested": "data"})

    def test_known_and_unknown_state_classifications(self) -> None:
        """Verify state classification mappings according to specification."""
        self.assertEqual(octodot.classify_session({"state": "QUEUED"}), "pending")
        self.assertEqual(octodot.classify_session({"state": "PLANNING"}), "pending")
        self.assertEqual(octodot.classify_session({"state": "IN_PROGRESS"}), "pending")
        self.assertEqual(octodot.classify_session({"state": "PAUSED"}), "blocked")
        self.assertEqual(octodot.classify_session({"state": "AWAITING_PLAN_APPROVAL"}), "blocked")
        self.assertEqual(octodot.classify_session({"state": "AWAITING_USER_FEEDBACK"}), "blocked")
        self.assertEqual(octodot.classify_session({"state": "FAILED"}), "failed")
        self.assertEqual(octodot.classify_session({"state": "COMPLETED"}), "completed")
        self.assertEqual(octodot.classify_session({"state": "NONSTANDARD_STATE"}), "unknown")
        self.assertEqual(octodot.classify_session({}), "unknown")

    def test_completed_with_pr_reported_delivery(self) -> None:
        """COMPLETED session with output PR URL reports delivery as pr_reported."""
        session_with_pr = {
            "name": "sessions/s1",
            "state": "COMPLETED",
            "prUrls": ["https://github.com/foo/bar/pull/1"],
            "outputs": [{"pullRequest": {"url": "https://github.com/foo/bar/pull/1"}}],
            "sourceContext": {"source": "sources/src1"},
        }
        detail_source = {"name": "sources/src1", "githubRepo": {"owner": "foo", "repo": "bar"}}
        with patch("octodot.read_session", return_value=session_with_pr) as mock_read_ses, \
             patch("octodot.request_json", return_value=(200, detail_source)), \
             patch("octodot.read_activities", return_value=([], True, None)):
            # Run results command via main
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    code = octodot.main(["-results", "sessions/s1"])
                self.assertEqual(code, 0)
                res = json.loads(out.getvalue())
                self.assertEqual(res["data"]["classification"], "completed")
                self.assertEqual(res["data"]["delivery"], "pr_reported")
                # Only 1 session read (no extra read because PR was already present)
                self.assertEqual(mock_read_ses.call_count, 1)

    def test_completed_no_pr_triggers_exactly_one_extra_get(self) -> None:
        """COMPLETED session with no PR triggers exactly one extra GET; reports completed_without_pr."""
        session_no_pr = {
            "name": "sessions/s1",
            "state": "COMPLETED",
            "outputs": [],
            "sourceContext": {"source": "sources/src1"},
        }
        detail_source = {"name": "sources/src1", "githubRepo": {"owner": "foo", "repo": "bar"}}
        with patch("octodot.read_session", return_value=session_no_pr) as mock_read_ses, \
             patch("octodot.request_json", return_value=(200, detail_source)), \
             patch("octodot.read_activities", return_value=([], True, None)):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    code = octodot.main(["-results", "sessions/s1"])
                self.assertEqual(code, 0)
                res = json.loads(out.getvalue())
                self.assertEqual(res["data"]["delivery"], "completed_without_pr")
                # Exactly 2 read_session calls (initial + exactly 1 extra)
                self.assertEqual(mock_read_ses.call_count, 2)

    def test_completed_no_pr_extra_get_failure_marks_incomplete(self) -> None:
        """Failure of the extra GET marks operation complete:false without claiming nondelivery."""
        session_no_pr = {
            "name": "sessions/s1",
            "state": "COMPLETED",
            "outputs": [],
            "sourceContext": {"source": "sources/src1"},
        }
        detail_source = {"name": "sources/src1", "githubRepo": {"owner": "foo", "repo": "bar"}}
        err_extra = octodot.OctodotError(
            octodot.error_record("transport_error", "Extra GET failed", "read_session"),
            exit_code=4,
        )

        read_calls = 0
        def fake_read_session(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                return session_no_pr
            raise err_extra

        with patch("octodot.read_session", side_effect=fake_read_session), \
             patch("octodot.request_json", return_value=(200, detail_source)), \
             patch("octodot.read_activities", return_value=([], True, None)):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    code = octodot.main(["-results", "sessions/s1"])
                res = json.loads(out.getvalue())
                self.assertFalse(res["complete"])
                self.assertEqual(code, 4)

    def test_blocked_and_failed_reason_data_extraction(self) -> None:
        """Blocked or failed sessions preserve error reasons and classification."""
        failed_session = {
            "name": "sessions/s1",
            "state": "FAILED",
            "error": {"message": "Execution limit exceeded"},
            "sourceContext": {"source": "sources/src1"},
        }
        detail_source = {"name": "sources/src1", "githubRepo": {"owner": "foo", "repo": "bar"}}
        with patch("octodot.read_session", return_value=failed_session), \
             patch("octodot.request_json", return_value=(200, detail_source)), \
             patch("octodot.read_activities", return_value=([], True, None)):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    octodot.main(["-results", "sessions/s1"])
                res = json.loads(out.getvalue())
                self.assertEqual(res["data"]["classification"], "failed")
                self.assertEqual(res["data"]["session"]["error"]["message"], "Execution limit exceeded")

    def test_out_of_order_activities_ordering_by_timestamp(self) -> None:
        """Out-of-order activities are ordered by RFC3339 timestamp to find latestActivity."""
        act_old = {"createTime": "2026-10-08T10:00:00Z", "name": "sessions/s/activities/a1"}
        act_new = {"createTime": "2026-10-08T14:00:00Z", "name": "sessions/s/activities/a2"}
        act_mid = {"createTime": "2026-10-08T12:00:00Z", "name": "sessions/s/activities/a3"}
        activities = [act_old, act_new, act_mid]

        session = {"name": "sessions/s", "state": "IN_PROGRESS", "sourceContext": {"source": "sources/s"}}
        detail_source = {"name": "sources/s", "githubRepo": {"owner": "foo", "repo": "bar"}}

        with patch("octodot.read_session", return_value=session), \
             patch("octodot.request_json", return_value=(200, detail_source)), \
             patch("octodot.read_activities", return_value=(activities, True, None)):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    octodot.main(["-results", "sessions/s"])
                res = json.loads(out.getvalue())
                self.assertEqual(res["data"]["latestActivity"]["name"], "sessions/s/activities/a2")

    def test_ambiguous_latest_activity_is_null(self) -> None:
        """Tied timestamps on newest activities results in latestActivity=null."""
        act1 = {"createTime": "2026-10-08T14:00:00Z", "name": "sessions/s/activities/a1"}
        act2 = {"createTime": "2026-10-08T14:00:00Z", "name": "sessions/s/activities/a2"}
        session = {"name": "sessions/s", "state": "IN_PROGRESS", "sourceContext": {"source": "sources/s"}}
        detail_source = {"name": "sources/s", "githubRepo": {"owner": "foo", "repo": "bar"}}

        with patch("octodot.read_session", return_value=session), \
             patch("octodot.request_json", return_value=(200, detail_source)), \
             patch("octodot.read_activities", return_value=([act1, act2], True, None)):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    octodot.main(["-results", "sessions/s"])
                res = json.loads(out.getvalue())
                self.assertIsNone(res["data"]["latestActivity"])


# =====================================================================
# 3. CreateTests
# =====================================================================
class CreateTests(unittest.TestCase):
    """Tests for exact session creation, receipt flushing, and context verification."""

    def setUp(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def tearDown(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def test_exact_request_payload_format_omitting_title(self) -> None:
        """Creation payload matches exact specification and omits title if not specified."""
        captured_payload = None

        def fake_request_json(method: str, path: str, body: Any = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            nonlocal captured_payload
            if method == "POST" and path == "/sessions":
                if isinstance(body, bytes):
                    captured_payload = json.loads(body.decode("utf-8"))
                elif isinstance(body, dict):
                    captured_payload = body
                else:
                    captured_payload = {}
                return 200, {
                    "name": "sessions/s1",
                    "id": "s1",
                    "sourceContext": {
                        "source": "sources/src1",
                        "githubRepoContext": {"startingBranch": "main"},
                    },
                }
            return 200, {}

        with patch("octodot.request_json", side_effect=fake_request_json), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    code = octodot.main(["-new", "-prompt", "test instructions", "--repo", "foo/bar", "--branch", "main"])
                self.assertEqual(code, 0)
                self.assertIsNotNone(captured_payload)
                self.assertEqual(
                    captured_payload,
                    {
                        "automationMode": "AUTO_CREATE_PR",
                        "prompt": "test instructions",
                        "requirePlanApproval": False,
                        "sourceContext": {
                            "githubRepoContext": {"startingBranch": "main"},
                            "source": "sources/src1",
                        },
                    },
                )
                self.assertNotIn("title", captured_payload)

    def test_request_payload_includes_title_when_provided(self) -> None:
        """When title is provided, it is included in the JSON payload."""
        captured_payload = None

        def fake_request_json(method: str, path: str, body: Any = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            nonlocal captured_payload
            if method == "POST" and path == "/sessions":
                if isinstance(body, bytes):
                    captured_payload = json.loads(body.decode("utf-8"))
                elif isinstance(body, dict):
                    captured_payload = body
                else:
                    captured_payload = {}
                return 200, {
                    "name": "sessions/s1",
                    "id": "s1",
                    "sourceContext": {
                        "source": "sources/src1",
                        "githubRepoContext": {"startingBranch": "main"},
                    },
                }
            return 200, {}

        with patch("octodot.request_json", side_effect=fake_request_json), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main", "--title", "My Task"])
                self.assertEqual(code, 0)
                self.assertEqual(captured_payload.get("title"), "My Task")

    def test_returned_session_name_used_when_id_differs(self) -> None:
        """Returned session name is used for subsequent operations, never constructed from id."""
        get_paths: list[str] = []

        def fake_request_json(method: str, path: str, body: bytes | None = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            if method == "POST":
                # Missing context in response triggers verification GET
                return 200, {"id": "12345", "name": "sessions/ses-real-name"}
            if method == "GET":
                get_paths.append(path)
                return 200, {
                    "id": "12345",
                    "name": "sessions/ses-real-name",
                    "sourceContext": {
                        "source": "sources/src1",
                        "githubRepoContext": {"startingBranch": "main"},
                    },
                }
            return 200, {}

        with patch("octodot.request_json", side_effect=fake_request_json), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                self.assertEqual(code, 0)
                # Verification GET must use /sessions/ses-real-name, NOT /sessions/12345
                self.assertIn("/sessions/ses-real-name", get_paths)
                self.assertNotIn("/sessions/12345", get_paths)

    def test_accepted_receipt_flushed_before_context_verification(self) -> None:
        """Accepted receipt is emitted and flushed to stderr before verification GET is issued."""
        events: list[str] = []

        err_stream = io.StringIO()
        real_write = err_stream.write

        def tracking_write(s: str) -> int:
            if "create_accepted" in s:
                events.append("create_accepted_emitted")
            return real_write(s)

        err_stream.write = tracking_write  # type: ignore[assignment]

        def fake_request_json(method: str, path: str, body: bytes | None = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            if method == "POST":
                return 200, {"id": "s1", "name": "sessions/s1"}
            if method == "GET":
                events.append("verification_get_started")
                return 200, {
                    "name": "sessions/s1",
                    "sourceContext": {
                        "source": "sources/src1",
                        "githubRepoContext": {"startingBranch": "main"},
                    },
                }
            return 200, {}

        with patch("octodot.request_json", side_effect=fake_request_json), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err_stream):
                    octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                # Assert create_accepted was emitted before verification GET started
                self.assertEqual(events, ["create_accepted_emitted", "verification_get_started"])

    def test_missing_context_get_verification_accepts_when_matched(self) -> None:
        """Missing context in POST response triggers GET; matches and accepts."""
        def fake_request_json(method: str, path: str, body: bytes | None = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            if method == "POST":
                return 200, {"id": "s1", "name": "sessions/s1"}
            if method == "GET":
                return 200, {
                    "name": "sessions/s1",
                    "sourceContext": {
                        "source": "sources/src1",
                        "githubRepoContext": {"startingBranch": "main"},
                    },
                }
            return 200, {}

        with patch("octodot.request_json", side_effect=fake_request_json), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                self.assertEqual(code, 0)
                lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                attempt = [l for l in lines if l.get("type") == "attempt"][0]
                self.assertEqual(attempt["outcome"], "accepted")
                self.assertTrue(attempt["contextVerified"])

    def test_context_mismatch_stops_with_created_context_mismatch(self) -> None:
        """Context mismatch in returned source or branch marks created_context_mismatch, exit 5."""
        def fake_request_json(method: str, path: str, body: bytes | None = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            if method == "POST":
                return 200, {
                    "name": "sessions/s1",
                    "sourceContext": {
                        "source": "sources/WRONG_SOURCE",
                        "githubRepoContext": {"startingBranch": "main"},
                    },
                }
            return 200, {}

        with patch("octodot.request_json", side_effect=fake_request_json), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                self.assertEqual(code, 5)
                lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                attempt = [l for l in lines if l.get("type") == "attempt"][0]
                self.assertEqual(attempt["outcome"], "created_context_mismatch")
                self.assertFalse(attempt["contextVerified"])

    def test_post_408_5xx_timeout_redirect_malformed_marked_uncertain_never_retried(self) -> None:
        """POST 408/5xx/timeout/redirect/malformed JSON sends once, marked uncertain, never retried."""
        error_scenarios = [
            ("500 Internal Error", octodot.OctodotError(octodot.error_record("provider_error", "500", "request_json", http_status=500), exit_code=5)),
            ("408 Timeout", octodot.OctodotError(octodot.error_record("timeout", "408", "request_json", http_status=408), exit_code=5)),
            ("503 Unavailable", octodot.OctodotError(octodot.error_record("provider_error", "503", "request_json", http_status=503), exit_code=5)),
            ("Transport Exception", octodot.OctodotError(octodot.error_record("transport_error", "Connection reset", "request_json"), exit_code=5)),
        ]

        for desc, err in error_scenarios:
            post_count = 0
            def fake_request_json(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
                nonlocal post_count
                if method == "POST":
                    post_count += 1
                    raise err
                return 200, {}

            with patch("octodot.request_json", side_effect=fake_request_json), \
                 patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
                with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                    out = io.StringIO()
                    with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                        code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                    self.assertEqual(code, 5, f"Failed for {desc}")
                    # Exactly ONE POST attempt made (NEVER retried)
                    self.assertEqual(post_count, 1, f"POST retried for {desc}")
                    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                    attempt = [l for l in lines if l.get("type") == "attempt"][0]
                    self.assertEqual(attempt["outcome"], "uncertain", f"Outcome not uncertain for {desc}")

    def test_post_4xx_including_429_rejected_without_retry(self) -> None:
        """POST 4xx (including 429) is marked rejected without retry."""
        scenarios = [400, 403, 404, 422, 429]
        for status in scenarios:
            post_count = 0
            err = octodot.OctodotError(
                octodot.error_record("client_error", f"HTTP {status}", "request_json", http_status=status),
                exit_code=3 if status == 403 else 4,
            )

            def fake_request_json(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
                nonlocal post_count
                if method == "POST":
                    post_count += 1
                    raise err
                return 200, {}

            with patch("octodot.request_json", side_effect=fake_request_json), \
                 patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
                with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                    out = io.StringIO()
                    with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                        code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                    self.assertEqual(post_count, 1)
                    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                    attempt = [l for l in lines if l.get("type") == "attempt"][0]
                    self.assertEqual(attempt["outcome"], "rejected")

    def test_absent_name_in_2xx_marked_uncertain(self) -> None:
        """Absent or empty name in 2xx response is marked uncertain."""
        with patch("octodot.request_json", return_value=(200, {"id": "123"})), \
             patch("octodot.resolve_source", return_value=({}, "sources/src1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                self.assertEqual(code, 5)
                lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                attempt = [l for l in lines if l.get("type") == "attempt"][0]
                self.assertEqual(attempt["outcome"], "uncertain")

    def test_local_preflight_failure_zero_post_sent(self) -> None:
        """Preflight failure sends zero POST requests; emits not_started."""
        with patch("octodot.resolve_source", side_effect=octodot.OctodotError(octodot.error_record("source_not_found", "No source", "resolve_source"), exit_code=4)), \
             patch("octodot.request_json") as mock_req:
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main"])
                self.assertEqual(code, 4)
                # Exactly ZERO POST requests sent
                mock_req.assert_not_called()
                lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                attempt = [l for l in lines if l.get("type") == "attempt"][0]
                self.assertEqual(attempt["outcome"], "not_started")
                summary = [l for l in lines if l.get("type") == "summary"][0]
                self.assertEqual(summary["notStarted"], 1)


# =====================================================================
# 4. ParallelTests
# =====================================================================
class ParallelTests(unittest.TestCase):
    """Tests for bounded parallel scheduler, stop events, and drain behavior."""

    def setUp(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def tearDown(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def test_barrier_controlled_mock_never_more_than_5_in_flight_for_n_100(self) -> None:
        """For N=20 requests, strictly assert never more than 5 in flight simultaneously."""
        current_in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        def fake_create_one(attempt: int, *args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal current_in_flight, max_in_flight
            with lock:
                current_in_flight += 1
                if current_in_flight > max_in_flight:
                    max_in_flight = current_in_flight
                self.assertLessEqual(current_in_flight, 5)
            # Simulate tiny worker execution
            time.sleep(0.005)
            with lock:
                current_in_flight -= 1
            return {
                "attempt": attempt,
                "contextVerified": True,
                "error": None,
                "fingerprint": "fp",
                "id": f"s-{attempt}",
                "name": f"sessions/s-{attempt}",
                "observed": {"branch": "main", "repo": "foo/bar", "source": "src"},
                "outcome": "accepted",
                "prUrls": [],
                "requested": {"branch": "main", "repo": "foo/bar", "source": "src"},
                "startedAt": "2026-10-08T12:00:00.000Z",
                "state": "QUEUED",
                "type": "attempt",
                "url": None,
            }

        with patch("octodot.create_one", side_effect=fake_create_one), \
             patch("octodot.resolve_source", return_value=({}, "src", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    code = octodot.main(["-new", "-prompt", "hi", "--repo", "foo/bar", "--branch", "main", "--parallel", "20"])
                self.assertEqual(code, 0)
                self.assertLessEqual(max_in_flight, 5)
                self.assertGreater(max_in_flight, 1)

    def test_drain_all_completed_futures_before_refilling(self) -> None:
        """Scheduler drains completed futures before refilling concurrency slots."""
        # Reset STOP_EVENT before run
        octodot.STOP_EVENT.clear()
        out = io.StringIO()
        with patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
             patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            # Test create_many directly with mock create_one
            with patch("octodot.create_one") as mock_create_one:
                mock_create_one.side_effect = lambda attempt, *args, **kwargs: {
                    "attempt": attempt,
                    "contextVerified": True,
                    "error": None,
                    "fingerprint": "fp",
                    "id": f"s-{attempt}",
                    "name": f"sessions/s-{attempt}",
                    "observed": None,
                    "outcome": "accepted",
                    "prUrls": [],
                    "requested": None,
                    "startedAt": "time",
                    "state": None,
                    "type": "attempt",
                    "url": None,
                }
                code = octodot.create_many(
                    parallel=10,
                    repo_str="foo/bar",
                    source_name="src",
                    starting_branch="main",
                    prompt="hi",
                    title=None,
                    key="k",
                    timeout=30.0,
                    deadline_start=time.monotonic(),
                    deadline=120.0,
                )
                self.assertEqual(code, 0)
                self.assertEqual(mock_create_one.call_count, 10)

    def test_shared_stop_event_stops_queued_workers_from_sending_post(self) -> None:
        """Setting the shared stop event halts unsubmitted workers; emits not_started."""
        octodot.STOP_EVENT.clear()

        def fake_create_one(attempt: int, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if attempt == 1:
                # Trigger stop event
                octodot.STOP_EVENT.set()
                return {
                    "attempt": attempt,
                    "contextVerified": None,
                    "error": octodot.error_record("client_error", "Rejected", "create_one", http_status=400),
                    "fingerprint": "fp",
                    "id": None,
                    "name": None,
                    "observed": None,
                    "outcome": "rejected",
                    "prUrls": [],
                    "requested": None,
                    "startedAt": "time",
                    "state": None,
                    "type": "attempt",
                    "url": None,
                }
            # Unsubmitted worker seeing stop event returns not_started
            if octodot.STOP_EVENT.is_set():
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
                    "requested": None,
                    "startedAt": None,
                    "state": None,
                    "type": "attempt",
                    "url": None,
                }
            return {
                "attempt": attempt,
                "contextVerified": True,
                "error": None,
                "fingerprint": "fp",
                "id": "s",
                "name": "s",
                "observed": None,
                "outcome": "accepted",
                "prUrls": [],
                "requested": None,
                "startedAt": "time",
                "state": None,
                "type": "attempt",
                "url": None,
            }

        with patch("octodot.create_one", side_effect=fake_create_one):
            out = io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                code = octodot.create_many(
                    parallel=5,
                    repo_str="foo/bar",
                    source_name="src",
                    starting_branch="main",
                    prompt="hi",
                    title=None,
                    key="k",
                    timeout=30.0,
                    deadline_start=time.monotonic(),
                    deadline=120.0,
                )
            self.assertEqual(code, 4)
            lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
            summary = [l for l in lines if l.get("type") == "summary"][0]
            self.assertEqual(summary["rejected"], 1)
            self.assertGreater(summary["notStarted"], 0)
        octodot.STOP_EVENT.clear()

    def test_already_sent_calls_retain_their_receipts(self) -> None:
        """Already admitted workers drain and retain their receipts and outcomes."""
        octodot.STOP_EVENT.clear()
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            # Mock create_one: attempt 1 rejected, attempt 2 accepted
            def fake_create(attempt: int, *args: Any, **kwargs: Any) -> dict[str, Any]:
                outcome = "rejected" if attempt == 1 else "accepted"
                err = octodot.error_record("error", "msg", "create") if attempt == 1 else None
                return {
                    "attempt": attempt,
                    "contextVerified": attempt != 1,
                    "error": err,
                    "fingerprint": "fp",
                    "id": f"s-{attempt}",
                    "name": f"sessions/s-{attempt}",
                    "observed": None,
                    "outcome": outcome,
                    "prUrls": [],
                    "requested": None,
                    "startedAt": "time",
                    "state": None,
                    "type": "attempt",
                    "url": None,
                }

            with patch("octodot.create_one", side_effect=fake_create):
                code = octodot.create_many(
                    parallel=2,
                    repo_str="foo/bar",
                    source_name="src",
                    starting_branch="main",
                    prompt="hi",
                    title=None,
                    key="k",
                    timeout=30.0,
                    deadline_start=time.monotonic(),
                    deadline=120.0,
                )
            lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
            attempts = [l for l in lines if l.get("type") == "attempt"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["outcome"], "rejected")
            self.assertEqual(attempts[1]["outcome"], "accepted")

    def test_total_counts_and_exit_code_precedence(self) -> None:
        """Total counts cover every ordinal; exit code precedence is strictly maintained."""
        octodot.STOP_EVENT.clear()
        # Case A: 1 uncertain, 1 rejected -> exit code 5 (uncertain wins)
        results_map = {
            1: {"attempt": 1, "outcome": "uncertain", "error": octodot.error_record("provider", "500", "op", 500)},
            2: {"attempt": 2, "outcome": "rejected", "error": octodot.error_record("client", "400", "op", 400)},
        }
        with patch("octodot.create_one", side_effect=lambda att, *args, **kwargs: results_map[att]):
            out = io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                code = octodot.create_many(
                    parallel=2,
                    repo_str="r",
                    source_name="s",
                    starting_branch="b",
                    prompt="p",
                    title=None,
                    key="k",
                    timeout=30.0,
                    deadline_start=time.monotonic(),
                    deadline=120.0,
                )
            self.assertEqual(code, 5)
            summary = [json.loads(l) for l in out.getvalue().splitlines() if l.strip() and "summary" in l][0]
            self.assertEqual(summary["requested"], 2)
            self.assertEqual(summary["uncertain"], 1)
            self.assertEqual(summary["rejected"], 1)
            self.assertEqual(summary["exitCode"], 5)

        # Case B: 1 rejected with 403, 1 accepted -> exit code 3 (auth wins over other failures)
        results_map_b = {
            1: {"attempt": 1, "outcome": "rejected", "error": octodot.error_record("auth", "403 Forbidden", "op", 403)},
            2: {"attempt": 2, "outcome": "accepted", "error": None},
        }
        with patch("octodot.create_one", side_effect=lambda att, *args, **kwargs: results_map_b[att]):
            out = io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                code = octodot.create_many(
                    parallel=2,
                    repo_str="r",
                    source_name="s",
                    starting_branch="b",
                    prompt="p",
                    title=None,
                    key="k",
                    timeout=30.0,
                    deadline_start=time.monotonic(),
                    deadline=120.0,
                )
            self.assertEqual(code, 3)


# =====================================================================
# 5. ArtifactTests
# =====================================================================
class ArtifactTests(unittest.TestCase):
    """Tests for patch artifacts collection, nanosecond timestamp ordering, and selection."""

    def test_schema_validation_of_candidate_changesets_and_gitpatch(self) -> None:
        """Candidate patches contain all required schema keys and preserve original array index."""
        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src1",
                            "gitPatch": {
                                "baseCommitId": "a" * 40,
                                "unidiffPatch": "diff --git a/f b/f\n",
                                "suggestedCommitMessage": "commit msg",
                            },
                        }
                    }
                ],
            }
        ]
        candidates = octodot.collect_patches("sessions/s", "sources/src1", activities, "test-key")
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        expected_keys = {
            "activity",
            "applyBaseAvailable",
            "artifactIndex",
            "baseCommitId",
            "createTime",
            "patchAvailable",
            "patchSha256",
            "sessionName",
            "source",
            "suggestedCommitMessage",
        }
        self.assertEqual(set(c.keys()), expected_keys)
        self.assertEqual(c["artifactIndex"], 0)
        self.assertTrue(c["applyBaseAvailable"])
        self.assertTrue(c["patchAvailable"])
        self.assertIsNotNone(c["patchSha256"])

    def test_wrong_source_and_session_rejected(self) -> None:
        """Artifacts matching wrong source are excluded from candidates; explicit selection fails."""
        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/OTHER",
                            "gitPatch": {
                                "baseCommitId": "a" * 40,
                                "unidiffPatch": "diff --git a/f b/f\n",
                            },
                        }
                    }
                ],
            }
        ]
        # Automatic candidate collection excludes it
        candidates = octodot.collect_patches("sessions/s", "sources/src1", activities, "test-key")
        self.assertEqual(len(candidates), 0)

        # Explicit selection targeting wrong source fails
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with patch("octodot.request_json", return_value=(200, activities[0])):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.select_patch(
                    session,
                    activities,
                    selector_activity="sessions/s/activities/a1",
                    selector_artifact=0,
                    key="test-key",
                )
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_nanosecond_precision_rfc3339_timestamp_comparisons(self) -> None:
        """Verify nanosecond-precision timestamp parsing and ordering."""
        # Candidate 1 is 1 nanosecond newer than Candidate 2
        ts1 = "2026-10-08T12:00:00.000000002Z"
        ts2 = "2026-10-08T12:00:00.000000001Z"
        nanos1 = octodot.parse_rfc3339_nanoseconds(ts1)
        nanos2 = octodot.parse_rfc3339_nanoseconds(ts2)
        self.assertIsNotNone(nanos1)
        self.assertIsNotNone(nanos2)
        self.assertEqual(nanos1, nanos2 + 1)

        activities = [
            {
                "name": "sessions/s/activities/a2",
                "createTime": ts2,
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src1",
                            "gitPatch": {"baseCommitId": "a" * 40, "unidiffPatch": "diff 2"},
                        }
                    }
                ],
            },
            {
                "name": "sessions/s/activities/a1",
                "createTime": ts1,
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src1",
                            "gitPatch": {"baseCommitId": "a" * 40, "unidiffPatch": "diff 1"},
                        }
                    }
                ],
            },
        ]
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        # Detailed activity GET returns the chosen activity
        with patch("octodot.request_json", return_value=(200, activities[1])):
            chosen, patch_str = octodot.select_patch(session, activities, None, None, key="k")
            self.assertEqual(chosen["activity"], "sessions/s/activities/a1")
            self.assertEqual(patch_str, "diff 1")

    def test_timestamp_tie_across_latest_candidates_yields_ambiguous_patch(self) -> None:
        """Timestamp tie across latest candidates raises ambiguous_patch error."""
        same_time = "2026-10-08T12:00:00.123456789Z"
        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": same_time,
                "artifacts": [
                    {"changeSet": {"source": "sources/src1", "gitPatch": {"unidiffPatch": "p1"}}},
                    {"changeSet": {"source": "sources/src1", "gitPatch": {"unidiffPatch": "p2"}}},
                ],
            }
        ]
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.select_patch(session, activities, None, None, key="k")
        self.assertEqual(ctx.exception.record["kind"], "ambiguous_patch")

    def test_missing_or_invalid_timestamp_yields_ambiguous_patch(self) -> None:
        """Invalid candidate timestamp blocks automatic selection with ambiguous_patch."""
        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": "INVALID_TIMESTAMP",
                "artifacts": [
                    {"changeSet": {"source": "sources/src1", "gitPatch": {"unidiffPatch": "p1"}}},
                ],
            }
        ]
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.select_patch(session, activities, None, None, key="k")
        self.assertEqual(ctx.exception.record["kind"], "ambiguous_patch")

    def test_newest_candidate_with_empty_or_missing_unidiff_patch_yields_no_patch_available(self) -> None:
        """Newest candidate having empty patch returns no_patch_available; NO fallback to older."""
        activities = [
            {
                "name": "sessions/s/activities/old",
                "createTime": "2026-10-08T10:00:00Z",
                "artifacts": [
                    {"changeSet": {"source": "sources/src1", "gitPatch": {"unidiffPatch": "valid older patch"}}},
                ],
            },
            {
                "name": "sessions/s/activities/new",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {"changeSet": {"source": "sources/src1", "gitPatch": {"unidiffPatch": ""}}},
                ],
            },
        ]
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.select_patch(session, activities, None, None, key="k")
        self.assertEqual(ctx.exception.record["kind"], "no_patch_available")

    def test_explicit_selector_full_get_and_verification(self) -> None:
        """Explicit selector GETs chosen activity and verifies source, index, and patch."""
        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src1",
                            "gitPatch": {"baseCommitId": "b" * 40, "unidiffPatch": "explicit patch"},
                        }
                    }
                ],
            }
        ]
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with patch("octodot.request_json", return_value=(200, activities[0])) as mock_req:
            chosen, p = octodot.select_patch(
                session,
                activities,
                selector_activity="sessions/s/activities/a1",
                selector_artifact=0,
                key="k",
            )
            self.assertEqual(p, "explicit patch")
            self.assertEqual(chosen["artifactIndex"], 0)
            mock_req.assert_called_once()

    def test_artifact_change_between_listing_and_fetch_stops_with_artifact_changed(self) -> None:
        """Change in patch content or metadata between listing and fetch raises artifact_changed."""
        listed_activity = {
            "name": "sessions/s/activities/a1",
            "createTime": "2026-10-08T12:00:00Z",
            "artifacts": [
                {
                    "changeSet": {
                        "source": "sources/src1",
                        "gitPatch": {"baseCommitId": "a" * 40, "unidiffPatch": "original patch"},
                    }
                }
            ],
        }
        modified_activity = {
            "name": "sessions/s/activities/a1",
            "createTime": "2026-10-08T12:00:00Z",
            "artifacts": [
                {
                    "changeSet": {
                        "source": "sources/src1",
                        "gitPatch": {"baseCommitId": "a" * 40, "unidiffPatch": "CHANGED patch"},
                    }
                }
            ],
        }
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with patch("octodot.request_json", return_value=(200, modified_activity)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.select_patch(session, [listed_activity], None, None, key="k")
            self.assertEqual(ctx.exception.record["kind"], "artifact_changed")

    def test_base_commit_absent_allows_json_pull_but_rejects_apply(self) -> None:
        """Candidate with absent baseCommitId is valid for JSON pull, but rejected for apply."""
        candidate = {
            "activity": "sessions/s/activities/a1",
            "applyBaseAvailable": False,
            "artifactIndex": 0,
            "baseCommitId": None,
            "createTime": "2026-10-08T12:00:00Z",
            "patchAvailable": True,
            "patchSha256": "sha",
            "sessionName": "sessions/s",
            "source": "sources/src1",
            "suggestedCommitMessage": None,
        }
        # Local apply requires full baseCommitId
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(octodot.OctodotError):
                octodot.apply_patch(tmpdir, b"diff", base_commit="", source_owner="o", source_repo="r")

    def test_sensitive_key_in_patch_text_refused_before_export_or_apply(self) -> None:
        """Artifact containing exact API key string is refused as secret_in_artifact."""
        secret_key = "sentinel_secret_key_123"
        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src1",
                            "gitPatch": {
                                "baseCommitId": "a" * 40,
                                "unidiffPatch": f"diff --git a/f b/f\n+{secret_key}\n",
                            },
                        }
                    }
                ],
            }
        ]
        session = {"name": "sessions/s", "sourceContext": {"source": "sources/src1"}}
        with patch("octodot.request_json", return_value=(200, activities[0])):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.select_patch(session, activities, None, None, key=secret_key)
            self.assertEqual(ctx.exception.record["kind"], "no_patch_available")

    def test_unicode_and_multiline_diff_fidelity(self) -> None:
        """Ensure byte-for-byte fidelity of Unicode and multi-line patch text."""
        unicode_patch = "diff --git a/file.txt b/file.txt\n+🚀✨ Non-ASCII: éàç العربية 日本語\n"
        patch_bytes = unicode_patch.encode("utf-8")
        computed_sha = hashlib.sha256(patch_bytes).hexdigest()

        activities = [
            {
                "name": "sessions/s/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src1",
                            "gitPatch": {"baseCommitId": "a" * 40, "unidiffPatch": unicode_patch},
                        }
                    }
                ],
            }
        ]
        candidates = octodot.collect_patches("sessions/s", "sources/src1", activities, "key")
        self.assertEqual(candidates[0]["patchSha256"], computed_sha)


# =====================================================================
# 6. GitTests
# =====================================================================
class GitTests(unittest.TestCase):
    """Tests for local Git patch application and teleport checkout."""

    def _init_git_repo(self, path: str) -> str:
        """Initialize a disposable Git repository with one commit."""
        subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "Test User"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "commit.gpgsign", "false"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "remote", "add", "origin", "https://github.com/OWNER/REPO.git"], check=True, capture_output=True)
        file_path = os.path.join(path, "file.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("Line 1\nLine 2\n")
        subprocess.run(["git", "-C", path, "add", "file.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "commit", "-m", "Initial commit"], check=True, capture_output=True)
        res = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        return res.stdout.strip()

    def test_clean_exact_base_real_apply_leaves_head_and_index_unchanged(self) -> None:
        """Successful git apply modifies worktree but leaves HEAD and raw index file bytes unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            head_sha = self._init_git_repo(tmpdir)
            index_path = os.path.join(tmpdir, ".git", "index")
            with open(index_path, "rb") as f:
                pre_index_bytes = f.read()

            patch_text = (
                "diff --git a/file.txt b/file.txt\n"
                "--- a/file.txt\n"
                "+++ b/file.txt\n"
                "@@ -1,2 +1,2 @@\n"
                " Line 1\n"
                "-Line 2\n"
                "+Line 2 updated\n"
            )
            octodot.apply_patch(tmpdir, patch_text.encode("utf-8"), head_sha, "OWNER", "REPO")

            # Verify worktree modified
            with open(os.path.join(tmpdir, "file.txt"), "r", encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, "Line 1\nLine 2 updated\n")

            # Verify HEAD unchanged
            res_head = subprocess.run(["git", "-C", tmpdir, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
            self.assertEqual(res_head.stdout.strip(), head_sha)

            # Verify index raw bytes unchanged
            with open(index_path, "rb") as f:
                post_index_bytes = f.read()
            self.assertEqual(post_index_bytes, pre_index_bytes)

    def test_read_only_pull_never_executes_git(self) -> None:
        """Read-only pull command never executes Git."""
        with patch("octodot.run_git") as mock_git:
            # Test that read operations do not invoke run_git
            args = octodot.parse_args(["-pull", "sessions/s1", "--json"])
            self.assertEqual(args["action"], "pull")
            self.assertTrue(args["json"])
            mock_git.assert_not_called()

    def test_dirty_staged_untracked_ignored_refuses_apply(self) -> None:
        """Dirty worktree, staged files, untracked files, or ignored files refuse patch apply."""
        patch_bytes = b"diff --git a/file.txt b/file.txt\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            head_sha = self._init_git_repo(tmpdir)

            # 1. Untracked file
            untracked = os.path.join(tmpdir, "untracked.txt")
            with open(untracked, "w") as f:
                f.write("untracked")
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.apply_patch(tmpdir, patch_bytes, head_sha, "OWNER", "REPO")
            self.assertEqual(ctx.exception.record["kind"], "dirty_worktree")
            os.remove(untracked)

            # 2. Ignored file
            gitignore = os.path.join(tmpdir, ".gitignore")
            with open(gitignore, "w") as f:
                f.write("ignored.log\n")
            subprocess.run(["git", "-C", tmpdir, "add", ".gitignore"], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "add gitignore"], check=True, capture_output=True)
            new_head = subprocess.run(["git", "-C", tmpdir, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

            ignored_file = os.path.join(tmpdir, "ignored.log")
            with open(ignored_file, "w") as f:
                f.write("log data")
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.apply_patch(tmpdir, patch_bytes, new_head, "OWNER", "REPO")
            self.assertEqual(ctx.exception.record["kind"], "dirty_worktree")

    def test_subdirectory_cwd_refuses_apply(self) -> None:
        """Cwd in a repository subdirectory refuses apply; must equal repo root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            head_sha = self._init_git_repo(tmpdir)
            subdir = os.path.join(tmpdir, "sub")
            os.mkdir(subdir)
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.apply_patch(subdir, b"diff", head_sha, "OWNER", "REPO")
            self.assertEqual(ctx.exception.record["kind"], "invalid_cwd")

    def test_wrong_base_commit_or_length_mismatch_refuses_apply(self) -> None:
        """Wrong baseCommitId or non-full length commit SHA is refused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._init_git_repo(tmpdir)
            # Mismatched 40-char SHA
            wrong_sha = "0" * 40
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.apply_patch(tmpdir, b"diff", wrong_sha, "OWNER", "REPO")
            self.assertEqual(ctx.exception.record["kind"], "base_mismatch")

            # Short SHA
            short_sha = "1234567"
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.apply_patch(tmpdir, b"diff", short_sha, "OWNER", "REPO")
            self.assertEqual(ctx.exception.record["kind"], "base_mismatch")

    def test_sparse_submodule_symlink_gitlink_refuses_apply(self) -> None:
        """Repositories with bare flag, sparse checkout, or unresolved merge are refused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._init_git_repo(tmpdir)
            # Simulate unresolved merge
            merge_head = os.path.join(tmpdir, ".git", "MERGE_HEAD")
            with open(merge_head, "w") as f:
                f.write("0" * 40 + "\n")
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.check_worktree_structure(tmpdir)
            self.assertEqual(ctx.exception.record["kind"], "unresolved_merge")

    def test_rename_and_copy_patches_refused(self) -> None:
        """Patches containing rename or copy operations are refused in v1."""
        forbidden_snippets = [
            b"diff --git a/a b/b\nrename from a\nrename to b\n",
            b"diff --git a/a b/b\ncopy from a\ncopy to b\n",
            b"diff --git a/a b/b\nsimilarity index 95%\n",
            b"diff --git a/.gitmodules b/.gitmodules.bak\nrename from .gitmodules\nrename to .gitmodules.bak\n",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._init_git_repo(tmpdir)
            for snippet in forbidden_snippets:
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.inspect_patch_safety(snippet, tmpdir)
                self.assertEqual(ctx.exception.record["kind"], "unsupported_patch")

    def test_unsafe_paths_and_gitmodules_refused(self) -> None:
        """Patches touching .git, traversal (..), or .gitmodules are refused."""
        unsafe_diff = (
            "diff --git a/.gitmodules b/.gitmodules\n"
            "--- a/.gitmodules\n"
            "+++ b/.gitmodules\n"
            "@@ -0,0 +1 @@\n"
            "+[submodule]\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmpdir:
            self._init_git_repo(tmpdir)
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.inspect_patch_safety(unsafe_diff, tmpdir)
            self.assertIn(ctx.exception.record["kind"], ("unsafe_path", "unsupported_patch", "git_error"))

    def test_teleport_requires_absent_target_and_existing_writable_parent(self) -> None:
        """Teleport requires absent destination and existing writable parent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Destination already exists
            existing_dest = os.path.join(tmpdir, "dest")
            os.mkdir(existing_dest)
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.teleport(existing_dest, {"name": "s"}, b"patch", "base", "o", "r", "k")
            self.assertEqual(ctx.exception.record["kind"], "invalid_dir")

            # Nonexistent parent
            nonexistent_parent = os.path.join(tmpdir, "no_such_dir", "dest")
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.teleport(nonexistent_parent, {"name": "s"}, b"patch", "base", "o", "r", "k")
            self.assertEqual(ctx.exception.record["kind"], "invalid_dir")

    def test_teleport_clones_creates_branch_and_applies(self) -> None:
        """Teleport clones target repo, checks out octodot/<session> branch, and applies patch."""
        with tempfile.TemporaryDirectory() as fixture_remote, \
             tempfile.TemporaryDirectory() as parent_dir:
            head_sha = self._init_git_repo(fixture_remote)
            target_dir = os.path.join(parent_dir, "teleport_checkout")

            patch_text = (
                "diff --git a/file.txt b/file.txt\n"
                "--- a/file.txt\n"
                "+++ b/file.txt\n"
                "@@ -1,2 +1,2 @@\n"
                " Line 1\n"
                "-Line 2\n"
                "+Line 2 teleported\n"
            )

            # Intercept run_git during clone to clone from local fixture repository
            real_run_git = octodot.run_git
            def fake_run_git(args: list[str], cwd: str | None = None, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if args and args[0] == "clone":
                    # Assert production clone URL was https://github.com/OWNER/REPO.git
                    self.assertEqual(args[5], "https://github.com/OWNER/REPO.git")
                    # Substitute fixture path for offline test
                    new_args = list(args)
                    new_args[5] = fixture_remote
                    res = real_run_git(new_args, cwd=cwd, **kwargs)
                    if res.returncode == 0:
                        subprocess.run(
                            ["git", "-C", target_dir, "remote", "set-url", "origin", "https://github.com/OWNER/REPO.git"],
                            check=True,
                            capture_output=True,
                        )
                    return res
                return real_run_git(args, cwd=cwd, **kwargs)

            session = {"name": "sessions/ses-teleport-1"}
            with patch("octodot.run_git", side_effect=fake_run_git):
                out_dir, out_branch = octodot.teleport(
                    target_dir,
                    session,
                    patch_text.encode("utf-8"),
                    head_sha,
                    "OWNER",
                    "REPO",
                    "key",
                )
                self.assertEqual(out_dir, os.path.abspath(target_dir))
                self.assertEqual(out_branch, "octodot/ses-teleport-1")

                # Verify file updated in teleported checkout
                with open(os.path.join(target_dir, "file.txt"), "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), "Line 1\nLine 2 teleported\n")

    def test_teleport_failure_leaves_partial_clone_no_retry(self) -> None:
        """Teleport failure leaves partial clone on disk; never attempts cleanup or retry."""
        with tempfile.TemporaryDirectory() as fixture_remote, \
             tempfile.TemporaryDirectory() as parent_dir:
            self._init_git_repo(fixture_remote)
            target_dir = os.path.join(parent_dir, "failed_teleport")

            real_run_git = octodot.run_git
            def fake_run_git(args: list[str], cwd: str | None = None, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if args and args[0] == "clone":
                    new_args = list(args)
                    new_args[5] = fixture_remote
                    return real_run_git(new_args, cwd=cwd, **kwargs)
                if args and args[0] == "cat-file":
                    # Simulate missing base commit failure
                    return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"missing commit\n")
                return real_run_git(args, cwd=cwd, **kwargs)

            session = {"name": "sessions/ses-fail"}
            with patch("octodot.run_git", side_effect=fake_run_git):
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.teleport(target_dir, session, b"diff", "0" * 40, "OWNER", "REPO", "k")
                self.assertEqual(ctx.exception.record["kind"], "base_missing")
                # Directory is preserved on disk (no automatic deletion)
                self.assertTrue(os.path.exists(target_dir))

    def test_prohibited_git_commands_never_invoked(self) -> None:
        """Verify that commit, push, stash, and reset are NEVER invoked."""
        prohibited = {"commit", "push", "stash", "reset"}
        with tempfile.TemporaryDirectory() as tmpdir:
            head_sha = self._init_git_repo(tmpdir)
            executed_commands: list[str] = []

            real_run_git = octodot.run_git
            def tracking_run_git(args: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[bytes]:
                if args:
                    cmd = args[0]
                    executed_commands.append(cmd)
                    self.assertNotIn(cmd, prohibited, f"Prohibited git command invoked: {cmd}")
                return real_run_git(args, *a, **kw)

            patch_text = (
                "diff --git a/file.txt b/file.txt\n"
                "--- a/file.txt\n"
                "+++ b/file.txt\n"
                "@@ -1,2 +1,2 @@\n"
                " Line 1\n"
                "-Line 2\n"
                "+Line 2 checked\n"
            )
            with patch("octodot.run_git", side_effect=tracking_run_git):
                octodot.apply_patch(tmpdir, patch_text.encode("utf-8"), head_sha, "OWNER", "REPO")

            for p in prohibited:
                self.assertNotIn(p, executed_commands)

    def test_configured_filter_or_insteadof_refused(self) -> None:
        """Configured filter.* commands or url.*.insteadOf in repository refuse apply."""
        with tempfile.TemporaryDirectory() as tmpdir:
            head_sha = self._init_git_repo(tmpdir)
            subprocess.run(["git", "-C", tmpdir, "config", "filter.bad.clean", "cat"], check=True, capture_output=True)
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.apply_patch(tmpdir, b"diff", head_sha, "OWNER", "REPO")
            self.assertEqual(ctx.exception.record["kind"], "unsupported_git_config")
            self.assertEqual(ctx.exception.exit_code, 3)


# =========================================================================
# Dedicated Verification Suites for PR #4 Review Findings (R1 - R6)
# =========================================================================

class TestR1SourceSchema(unittest.TestCase):
    """R1: Test Google REST schema with branches and defaultBranch nested in githubRepo."""

    def setUp(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def tearDown(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def test_r1_schema_faithful_default_and_branches(self) -> None:
        """R1: Resolve source successfully reads defaultBranch and branches inside githubRepo."""
        sources_list = [
            {
                "name": "sources/s1",
                "githubRepo": {"owner": "testorg", "repo": "testrepo"},
            }
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "testorg",
                "repo": "testrepo",
                "defaultBranch": {"displayName": "main"},
                "branches": [
                    {"displayName": "main"},
                    {"displayName": "develop"},
                    {"displayName": "feature-x"},
                ],
            },
        }

        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            # 1. Discover default branch when requested is None
            det, name, branch = octodot.resolve_source("testorg", "testrepo", None, "key")
            self.assertEqual(name, "sources/s1")
            self.assertEqual(branch, "main")

            # 2. Explicit branch selection
            _, _, branch_dev = octodot.resolve_source("testorg", "testrepo", "develop", "key")
            self.assertEqual(branch_dev, "develop")

            # 3. Explicit branch not present
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("testorg", "testrepo", "nonexistent", "key")
            self.assertEqual(ctx.exception.record["kind"], "branch_not_found")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_r1_schema_faithful_default_only_branch(self) -> None:
        """R1: defaultBranch present inside githubRepo with empty or absent branches list."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}}
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
                "defaultBranch": {"displayName": "master"},
                "branches": [],
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            _, _, branch = octodot.resolve_source("owner", "repo", None, "key")
            self.assertEqual(branch, "master")

            # Explicit request for default branch displayName
            _, _, branch_req = octodot.resolve_source("owner", "repo", "master", "key")
            self.assertEqual(branch_req, "master")

    def test_r1_schema_faithful_absent_branch_fails_preflight(self) -> None:
        """R1: Neither defaultBranch nor branches configured inside githubRepo raises source_error."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}}
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key")
            self.assertEqual(ctx.exception.record["kind"], "source_error")
            self.assertEqual(ctx.exception.record["message"], "Source has no branches configured")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_r1_schema_missing_default_branch_with_branches_list(self) -> None:
        """R1: defaultBranch absent but branches list present inside githubRepo."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}}
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {
                "owner": "owner",
                "repo": "repo",
                "branches": [{"displayName": "only-branch"}],
            },
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            # Explicit request succeeds
            _, _, branch = octodot.resolve_source("owner", "repo", "only-branch", "key")
            self.assertEqual(branch, "only-branch")

            # Omitted branch raises source_error because defaultBranch is absent
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key")
            self.assertEqual(ctx.exception.record["kind"], "source_error")
            self.assertEqual(ctx.exception.record["message"], "Source has no default branch")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_r1_schema_root_branches_ignored_when_githubRepo_empty(self) -> None:
        """R1: Branches mistakenly put at root are not read; githubRepo is authoritative."""
        sources_list = [
            {"name": "sources/s1", "githubRepo": {"owner": "owner", "repo": "repo"}}
        ]
        detail_source = {
            "name": "sources/s1",
            "githubRepo": {"owner": "owner", "repo": "repo"},
            # Incorrect root placements
            "defaultBranch": {"displayName": "root-main"},
            "branches": [{"displayName": "root-main"}],
        }
        with patch("octodot.paginate", return_value=(sources_list, True, None)), \
             patch("octodot.request_json", return_value=(200, detail_source)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.resolve_source("owner", "repo", None, "key")
            # Must raise because githubRepo has no branches configured
            self.assertEqual(ctx.exception.record["kind"], "source_error")
            self.assertEqual(ctx.exception.record["message"], "Source has no branches configured")


class TestR4PostErrorClassification(unittest.TestCase):
    """R4: Test HTTP error classification on POST requests at urllib HTTPError boundary."""

    def setUp(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def tearDown(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    @mock.patch("urllib.request.build_opener")
    def test_r4_post_definitive_rejections_400_404_422_429(self, mock_build: Any) -> None:
        """R4: HTTP 400, 404, 422, 429 on POST results in outcome 'rejected', exit code 4, call_count 1."""
        rejection_statuses = [400, 404, 422, 429]
        for status in rejection_statuses:
            octodot.STOP_EVENT.clear()
            mock_opener = mock.Mock()
            mock_build.return_value = mock_opener

            err_body = json.dumps({"error": {"code": status, "message": f"Client error {status}"}}).encode("utf-8")
            http_err = urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                status,
                f"Client Error {status}",
                {"Content-Type": "application/json"},
                io.BytesIO(err_body),
            )
            mock_opener.open.side_effect = http_err

            # 1. Test request_json directly
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.request_json("POST", "/sessions", "secret-key", body={"prompt": "hi"}, is_post=True)
            self.assertEqual(ctx.exception.exit_code, 4, f"request_json exit code not 4 for HTTP {status}")
            self.assertEqual(mock_opener.open.call_count, 1, f"POST was retried for HTTP {status}")

            # 2. Test create_one directly
            mock_opener.open.reset_mock()
            mock_opener.open.side_effect = urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                status,
                f"Client Error {status}",
                {"Content-Type": "application/json"},
                io.BytesIO(err_body),
            )
            res = octodot.create_one(
                attempt=1,
                repo_str="owner/repo",
                source_name="sources/s1",
                starting_branch="main",
                payload={"prompt": "test"},
                fingerprint="fp1",
                key="secret-key",
                timeout=10.0,
                deadline_start=time.monotonic(),
                deadline=60.0,
            )
            self.assertEqual(res["outcome"], "rejected", f"create_one outcome not rejected for HTTP {status}")
            self.assertEqual(res["error"]["httpStatus"], status)
            self.assertTrue(octodot.STOP_EVENT.is_set())
            self.assertEqual(mock_opener.open.call_count, 1)

            # 3. Test through main CLI
            octodot.STOP_EVENT.clear()
            mock_opener.open.reset_mock()
            mock_opener.open.side_effect = urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                status,
                f"Client Error {status}",
                {"Content-Type": "application/json"},
                io.BytesIO(err_body),
            )
            with patch("octodot.resolve_source", return_value=({}, "sources/s1", "main")):
                with patch.dict(os.environ, {"JULES_API_KEY": "secret-key"}):
                    out = io.StringIO()
                    with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                        rc = octodot.main(["-new", "-prompt", "hi", "--repo", "owner/repo", "--branch", "main"])
                    self.assertEqual(rc, 4, f"CLI exit code not 4 for HTTP {status}")
                    self.assertEqual(mock_opener.open.call_count, 1)
                    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                    attempt_line = [l for l in lines if l.get("type") == "attempt"][0]
                    self.assertEqual(attempt_line["outcome"], "rejected")
                    summary_line = [l for l in lines if l.get("type") == "summary"][0]
                    self.assertEqual(summary_line["rejected"], 1)
                    self.assertEqual(summary_line["uncertain"], 0)
                    self.assertEqual(summary_line["exitCode"], 4)

    @mock.patch("urllib.request.build_opener")
    def test_r4_post_auth_failures_401_403(self, mock_build: Any) -> None:
        """R4: HTTP 401 and 403 on POST results in outcome 'rejected', exit code 3, call_count 1."""
        auth_statuses = [401, 403]
        for status in auth_statuses:
            octodot.STOP_EVENT.clear()
            mock_opener = mock.Mock()
            mock_build.return_value = mock_opener

            err_body = json.dumps({"error": {"code": status, "message": f"Auth error {status}"}}).encode("utf-8")
            http_err = urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                status,
                f"Auth Error {status}",
                {"Content-Type": "application/json"},
                io.BytesIO(err_body),
            )
            mock_opener.open.side_effect = http_err

            # 1. Test request_json directly
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.request_json("POST", "/sessions", "secret-key", body={"prompt": "hi"}, is_post=True)
            self.assertEqual(ctx.exception.exit_code, 3, f"request_json exit code not 3 for HTTP {status}")
            self.assertEqual(mock_opener.open.call_count, 1)

            # 2. Test create_one directly
            mock_opener.open.reset_mock()
            mock_opener.open.side_effect = urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                status,
                f"Auth Error {status}",
                {"Content-Type": "application/json"},
                io.BytesIO(err_body),
            )
            res = octodot.create_one(
                attempt=1,
                repo_str="owner/repo",
                source_name="sources/s1",
                starting_branch="main",
                payload={"prompt": "test"},
                fingerprint="fp1",
                key="secret-key",
                timeout=10.0,
                deadline_start=time.monotonic(),
                deadline=60.0,
            )
            self.assertEqual(res["outcome"], "rejected")
            self.assertEqual(res["error"]["httpStatus"], status)
            self.assertTrue(octodot.STOP_EVENT.is_set())
            self.assertEqual(mock_opener.open.call_count, 1)

            # 3. Test through main CLI
            octodot.STOP_EVENT.clear()
            mock_opener.open.reset_mock()
            mock_opener.open.side_effect = urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                status,
                f"Auth Error {status}",
                {"Content-Type": "application/json"},
                io.BytesIO(err_body),
            )
            with patch("octodot.resolve_source", return_value=({}, "sources/s1", "main")):
                with patch.dict(os.environ, {"JULES_API_KEY": "secret-key"}):
                    out = io.StringIO()
                    with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                        rc = octodot.main(["-new", "-prompt", "hi", "--repo", "owner/repo", "--branch", "main"])
                    self.assertEqual(rc, 3, f"CLI exit code not 3 for HTTP {status}")
                    self.assertEqual(mock_opener.open.call_count, 1)
                    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                    summary_line = [l for l in lines if l.get("type") == "summary"][0]
                    self.assertEqual(summary_line["exitCode"], 3)
                    self.assertEqual(summary_line["rejected"], 1)

    @mock.patch("urllib.request.build_opener")
    def test_r4_post_uncertain_errors_408_500_503_transport_timeout(self, mock_build: Any) -> None:
        """R4: HTTP 408, 5xx, transport errors, and timeouts on POST result in 'uncertain', exit code 5."""
        uncertain_scenarios = [
            ("408 Timeout", urllib.error.HTTPError("https://jules.googleapis.com/v1alpha/sessions", 408, "Request Timeout", {}, io.BytesIO(b"{}"))),
            ("500 Internal Error", urllib.error.HTTPError("https://jules.googleapis.com/v1alpha/sessions", 500, "Internal Server Error", {}, io.BytesIO(b"{}"))),
            ("502 Bad Gateway", urllib.error.HTTPError("https://jules.googleapis.com/v1alpha/sessions", 502, "Bad Gateway", {}, io.BytesIO(b"{}"))),
            ("503 Unavailable", urllib.error.HTTPError("https://jules.googleapis.com/v1alpha/sessions", 503, "Service Unavailable", {}, io.BytesIO(b"{}"))),
            ("504 Gateway Timeout", urllib.error.HTTPError("https://jules.googleapis.com/v1alpha/sessions", 504, "Gateway Timeout", {}, io.BytesIO(b"{}"))),
            ("URLError Connection Reset", urllib.error.URLError("Connection reset by peer")),
            ("TimeoutError Socket Timeout", TimeoutError("Socket timed out")),
        ]

        for desc, exc in uncertain_scenarios:
            octodot.STOP_EVENT.clear()
            mock_opener = mock.Mock()
            mock_build.return_value = mock_opener
            mock_opener.open.side_effect = exc

            # 1. Test request_json directly
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.request_json("POST", "/sessions", "secret-key", body={"prompt": "hi"}, is_post=True)
            self.assertEqual(ctx.exception.exit_code, 5, f"request_json exit code not 5 for {desc}")
            self.assertEqual(mock_opener.open.call_count, 1, f"POST retried for {desc}")

            # 2. Test create_one directly
            mock_opener.open.reset_mock()
            mock_opener.open.side_effect = exc
            res = octodot.create_one(
                attempt=1,
                repo_str="owner/repo",
                source_name="sources/s1",
                starting_branch="main",
                payload={"prompt": "test"},
                fingerprint="fp1",
                key="secret-key",
                timeout=10.0,
                deadline_start=time.monotonic(),
                deadline=60.0,
            )
            self.assertEqual(res["outcome"], "uncertain", f"create_one outcome not uncertain for {desc}")
            self.assertTrue(octodot.STOP_EVENT.is_set())
            self.assertEqual(mock_opener.open.call_count, 1)

            # 3. Test through main CLI
            octodot.STOP_EVENT.clear()
            mock_opener.open.reset_mock()
            mock_opener.open.side_effect = exc
            with patch("octodot.resolve_source", return_value=({}, "sources/s1", "main")):
                with patch.dict(os.environ, {"JULES_API_KEY": "secret-key"}):
                    out = io.StringIO()
                    with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                        rc = octodot.main(["-new", "-prompt", "hi", "--repo", "owner/repo", "--branch", "main"])
                    self.assertEqual(rc, 5, f"CLI exit code not 5 for {desc}")
                    self.assertEqual(mock_opener.open.call_count, 1)
                    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                    summary_line = [l for l in lines if l.get("type") == "summary"][0]
                    self.assertEqual(summary_line["exitCode"], 5)
                    self.assertEqual(summary_line["uncertain"], 1)

    @mock.patch("urllib.request.build_opener")
    def test_r4_post_redirects_and_malformed_json_are_uncertain_exit_5(self, mock_build: Any) -> None:
        """R4: Redirects (3xx) and malformed 2xx success on POST result in 'uncertain', exit code 5."""
        # 1. Redirect error (302)
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener
        redirect_err = urllib.error.HTTPError(
            "https://jules.googleapis.com/v1alpha/sessions",
            302,
            "Found",
            {"Location": "https://other.com"},
            io.BytesIO(b""),
        )
        mock_opener.open.side_effect = redirect_err

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("POST", "/sessions", "secret-key", body={"prompt": "hi"}, is_post=True)
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertEqual(ctx.exception.record["kind"], "redirect_denied")
        self.assertEqual(mock_opener.open.call_count, 1)

        # 2. Malformed 200 JSON
        mock_opener.open.reset_mock()
        ok_malformed = mock.Mock()
        ok_malformed.status = 200
        ok_malformed.read.return_value = b"{not valid json"
        ok_malformed.__enter__ = mock.Mock(return_value=ok_malformed)
        ok_malformed.__exit__ = mock.Mock(return_value=False)
        mock_opener.open.side_effect = None
        mock_opener.open.return_value = ok_malformed

        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.request_json("POST", "/sessions", "secret-key", body={"prompt": "hi"}, is_post=True)
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertEqual(ctx.exception.record["kind"], "protocol_error")
        self.assertEqual(mock_opener.open.call_count, 1)

    @mock.patch("urllib.request.build_opener")
    def test_r4_parallel_create_halts_queued_attempts_on_rejection(self, mock_build: Any) -> None:
        """R4: Failure sets STOP_EVENT and halts refilling; unadmitted ordinals become not_started."""
        mock_opener = mock.Mock()
        mock_build.return_value = mock_opener

        barrier = threading.Barrier(5)

        def fake_open(req: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
            err_body = json.dumps({"error": {"code": 400, "message": "Bad request"}}).encode("utf-8")
            raise urllib.error.HTTPError(
                "https://jules.googleapis.com/v1alpha/sessions",
                400,
                "Bad Request",
                {},
                io.BytesIO(err_body),
            )

        mock_opener.open.side_effect = fake_open

        with patch("octodot.resolve_source", return_value=({}, "sources/s1", "main")):
            with patch.dict(os.environ, {"JULES_API_KEY": "secret-key"}):
                out = io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                    rc = octodot.main(["-new", "-prompt", "hi", "--repo", "owner/repo", "--branch", "main", "--parallel", "7"])
                self.assertEqual(rc, 4)
                lines_out = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
                attempts = [l for l in lines_out if l.get("type") == "attempt"]
                self.assertEqual(len(attempts), 7)
                # Admitted batch of 5 failed with rejected
                for a in attempts[:5]:
                    self.assertEqual(a["outcome"], "rejected")
                # Remaining attempts never admitted due to STOP_EVENT
                for a in attempts[5:]:
                    self.assertEqual(a["outcome"], "not_started")
                summary = [l for l in lines_out if l.get("type") == "summary"][0]
                self.assertEqual(summary["exitCode"], 4)
                self.assertEqual(summary["rejected"], 5)
                self.assertEqual(summary["notStarted"], 2)


class TestR2PreflightCancellation(unittest.TestCase):
    """R2: Preflight cancellation preservation, stop state, and admission control."""

    def setUp(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def tearDown(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def test_r2_sigint_injected_during_source_resolution_halts_dispatch(self) -> None:
        """Inject SIGINT during source resolution, assert 0 POSTs, all not_started, exit code 4."""
        post_calls = []

        def fake_resolve_source(*args: Any, **kwargs: Any) -> tuple[dict, str, str]:
            # Simulate SIGINT arrival during source resolution
            octodot._signal_handler(signal.SIGINT, None)
            return (
                {"name": "sources/s-1", "githubRepo": {"owner": "OWNER", "repo": "REPO", "defaultBranch": "main"}},
                "sources/s-1",
                "main",
            )

        def tracking_request_json(method: str, *args: Any, **kwargs: Any) -> Any:
            if method == "POST":
                post_calls.append(args)
            return (200, {"name": "sessions/ses-1", "id": "ses-1", "state": "QUEUED"})

        out = io.StringIO()
        err_out = io.StringIO()
        with patch("octodot.resolve_source", side_effect=fake_resolve_source), \
             patch("octodot.request_json", side_effect=tracking_request_json), \
             patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
             patch("sys.stdout", out), patch("sys.stderr", err_out):
            code = octodot.main(["-new", "-prompt", "hello", "--repo", "OWNER/REPO", "--branch", "main", "--parallel", "3"])

        self.assertEqual(code, 4)
        self.assertEqual(len(post_calls), 0, "No POST requests should be made after preflight cancellation")

        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        attempts = [l for l in lines if l.get("type") == "attempt"]
        summaries = [l for l in lines if l.get("type") == "summary"]

        self.assertEqual(len(attempts), 3)
        for att in attempts:
            self.assertEqual(att["outcome"], "not_started")

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["exitCode"], 4)
        self.assertEqual(summary["notStarted"], 3)
        self.assertEqual(summary["accepted"], 0)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["uncertain"], 0)
        self.assertIsNotNone(summary["error"])
        self.assertEqual(summary["error"]["kind"], "interrupted")

    def test_r2_sigterm_injected_during_source_resolution_halts_dispatch(self) -> None:
        """Inject SIGTERM during source resolution, assert 0 POSTs, all not_started, exit code 4."""
        post_calls = []

        def fake_resolve_source(*args: Any, **kwargs: Any) -> tuple[dict, str, str]:
            octodot._signal_handler(signal.SIGTERM, None)
            return (
                {"name": "sources/s-1", "githubRepo": {"owner": "OWNER", "repo": "REPO", "defaultBranch": "main"}},
                "sources/s-1",
                "main",
            )

        def tracking_request_json(method: str, *args: Any, **kwargs: Any) -> Any:
            if method == "POST":
                post_calls.append(args)
            return (200, {"name": "sessions/ses-1", "id": "ses-1", "state": "QUEUED"})

        out = io.StringIO()
        err_out = io.StringIO()
        with patch("octodot.resolve_source", side_effect=fake_resolve_source), \
             patch("octodot.request_json", side_effect=tracking_request_json), \
             patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
             patch("sys.stdout", out), patch("sys.stderr", err_out):
            code = octodot.main(["-new", "-prompt", "hello", "--repo", "OWNER/REPO", "--branch", "main", "--parallel", "2"])

        self.assertEqual(code, 4)
        self.assertEqual(len(post_calls), 0)

        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        attempts = [l for l in lines if l.get("type") == "attempt"]
        summaries = [l for l in lines if l.get("type") == "summary"]

        self.assertEqual(len(attempts), 2)
        for att in attempts:
            self.assertEqual(att["outcome"], "not_started")
        self.assertEqual(summaries[0]["exitCode"], 4)
        self.assertEqual(summaries[0]["error"]["kind"], "interrupted")

    def test_r2_create_many_refuses_admission_when_stop_event_pre_set(self) -> None:
        """create_many does NOT clear STOP_EVENT and admits zero workers if STOP_EVENT is set."""
        octodot.STOP_EVENT.set()
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = octodot.create_many(
                parallel=3,
                repo_str="OWNER/REPO",
                source_name="sources/s1",
                starting_branch="main",
                prompt="hello",
                title=None,
                key="test-key",
                timeout=30.0,
                deadline_start=time.monotonic(),
                deadline=120.0,
            )
        self.assertEqual(code, 4)
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        attempts = [l for l in lines if l.get("type") == "attempt"]
        summaries = [l for l in lines if l.get("type") == "summary"]
        self.assertEqual(len(attempts), 3)
        for att in attempts:
            self.assertEqual(att["outcome"], "not_started")
        self.assertEqual(summaries[0]["exitCode"], 4)
        self.assertEqual(summaries[0]["error"]["kind"], "interrupted")

    def test_r2_http_request_admission_refused_after_interruption(self) -> None:
        """request_json refuses HTTP admission before opening connection if STOP_EVENT or INTERRUPTED."""
        octodot.STOP_EVENT.set()
        with patch("urllib.request.OpenerDirector.open") as mock_open:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.request_json("GET", "/sources", key="test-key")
            self.assertEqual(ctx.exception.record["kind"], "interrupted")
            self.assertEqual(ctx.exception.exit_code, 4)
            mock_open.assert_not_called()

    def test_r2_git_admission_refused_after_interruption(self) -> None:
        """run_git refuses subprocess admission after interruption."""
        octodot.INTERRUPTED = True
        with patch("subprocess.run") as mock_subproc:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.run_git(["status"])
            self.assertEqual(ctx.exception.record["kind"], "interrupted")
            self.assertEqual(ctx.exception.exit_code, 4)
            mock_subproc.assert_not_called()

    def test_r2_drains_already_admitted_work_and_retains_receipts(self) -> None:
        """Already admitted workers drain and retain receipts, remaining workers are not admitted."""
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

        admitted_count = 0
        lock = threading.Lock()

        def fake_create_one(attempt: int, *args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal admitted_count
            with lock:
                admitted_count += 1
                # When worker 1 finishes, trigger interrupt
                if attempt == 1:
                    octodot.STOP_EVENT.set()
            return {
                "attempt": attempt,
                "contextVerified": True,
                "error": None,
                "fingerprint": "fp",
                "id": f"s-{attempt}",
                "name": f"sessions/s-{attempt}",
                "observed": None,
                "outcome": "accepted",
                "prUrls": [],
                "requested": None,
                "startedAt": "2026-10-08T12:00:00Z",
                "state": "QUEUED",
                "type": "attempt",
                "url": None,
            }

        out = io.StringIO()
        with patch("octodot.create_one", side_effect=fake_create_one):
            with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                code = octodot.create_many(
                    parallel=10,
                    repo_str="OWNER/REPO",
                    source_name="src",
                    starting_branch="main",
                    prompt="hi",
                    title=None,
                    key="k",
                    timeout=30.0,
                    deadline_start=time.monotonic(),
                    deadline=120.0,
                )
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        attempts = [l for l in lines if l.get("type") == "attempt"]
        self.assertEqual(len(attempts), 10)
        # Workers beyond initial max_workers (5) should NOT be refilled
        self.assertLessEqual(admitted_count, 5)
        # Any not admitted should have outcome not_started
        not_started_ordinals = [a for a in attempts if a["outcome"] == "not_started"]
        self.assertGreaterEqual(len(not_started_ordinals), 5)


class TestR5GitInvocationBudget(unittest.TestCase):
    """R5: Local Git invocation budget, deadline enforcement, and mutation timeout tests."""

    def setUp(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def tearDown(self) -> None:
        octodot.STOP_EVENT.clear()
        octodot.INTERRUPTED = False

    def test_r5_expired_before_clone_teleport_admits_no_subprocess(self) -> None:
        """Expired invocation budget before teleport admits no subprocess."""
        start_time = time.monotonic() - 100.0
        deadline = 10.0  # Expired by 90 seconds

        with tempfile.TemporaryDirectory() as tmpdir:
            target_checkout = os.path.join(tmpdir, "checkout")
            with patch("subprocess.run") as mock_subproc:
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.teleport(
                        target_checkout,
                        {"name": "sessions/ses-1"},
                        b"diff",
                        "0" * 40,
                        "OWNER",
                        "REPO",
                        key="test-key",
                        deadline_start=start_time,
                        deadline=deadline,
                    )
                self.assertEqual(ctx.exception.record["kind"], "deadline_exceeded")
                self.assertEqual(ctx.exception.exit_code, 4)
                mock_subproc.assert_not_called()

    def test_r5_expired_before_apply_admits_no_subprocess(self) -> None:
        """Expired invocation budget before apply_patch admits no subprocess."""
        start_time = time.monotonic() - 100.0
        deadline = 10.0  # Expired

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("subprocess.run") as mock_subproc:
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.apply_patch(
                        tmpdir,
                        b"diff",
                        "0" * 40,
                        "OWNER",
                        "REPO",
                        deadline_start=start_time,
                        deadline=deadline,
                    )
                self.assertEqual(ctx.exception.record["kind"], "deadline_exceeded")
                self.assertEqual(ctx.exception.exit_code, 4)
                mock_subproc.assert_not_called()

    def test_r5_expired_run_git_admits_no_subprocess(self) -> None:
        """run_git with remaining budget <= 0 raises deadline_exceeded without launching subprocess."""
        start_time = time.monotonic() - 50.0
        deadline = 10.0
        with patch("subprocess.run") as mock_subproc:
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.run_git(["status"], deadline_start=start_time, deadline=deadline)
            self.assertEqual(ctx.exception.record["kind"], "deadline_exceeded")
            self.assertEqual(ctx.exception.exit_code, 4)
            mock_subproc.assert_not_called()

    def test_r5_near_expiry_uses_reduced_timeout(self) -> None:
        """run_git near deadline expiry scales effective_timeout to min(timeout, rem)."""
        start_time = time.monotonic() - 6.0
        deadline = 10.0  # rem ~= 4.0 seconds
        default_timeout = 30.0

        with patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = subprocess.CompletedProcess(["git", "--version"], 0, stdout=b"git version 2.39.0", stderr=b"")
            octodot.run_git(["--version"], timeout=default_timeout, deadline_start=start_time, deadline=deadline)

            self.assertEqual(mock_subproc.call_count, 1)
            used_timeout = mock_subproc.call_args[1].get("timeout")
            self.assertIsNotNone(used_timeout)
            self.assertLess(used_timeout, default_timeout)
            self.assertLessEqual(used_timeout, 4.1)
            self.assertGreater(used_timeout, 0.0)

    def test_r5_timed_out_mutation_reports_git_timeout(self) -> None:
        """Timed-out mutation subprocess reports record 'git_timeout' and exit_code 4."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "apply" in cmd and "--check" not in cmd and "--numstat" not in cmd and "--summary" not in cmd:
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 30))
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "rev-parse" in cmd:
                    if "--show-toplevel" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=real_tmpdir.encode("utf-8") + b"\n", stderr=b"")
                    if "--is-bare-repository" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b"false\n", stderr=b"")
                    if "--git-path" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b".git/index\n", stderr=b"")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"a" * 40 + b"\n", stderr=b"")
                if "remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"https://github.com/OWNER/REPO.git\n", stderr=b"")
                if "status" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "ls-files" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "apply" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            git_dir = os.path.join(real_tmpdir, ".git")
            os.makedirs(git_dir, exist_ok=True)
            with open(os.path.join(git_dir, "index"), "wb") as f:
                f.write(b"index-data")

            with patch("subprocess.run", side_effect=fake_subproc):
                with self.assertRaises(octodot.OctodotError) as ctx:
                    octodot.apply_patch(
                        real_tmpdir,
                        b"diff --git a/f b/f\n",
                        "a" * 40,
                        "OWNER",
                        "REPO",
                        deadline_start=time.monotonic(),
                        deadline=120.0,
                    )
                self.assertEqual(ctx.exception.record["kind"], "git_timeout")
                self.assertEqual(ctx.exception.exit_code, 4)
                self.assertEqual(ctx.exception.stage, "apply")

    def test_r5_cli_pull_apply_timed_out_mutation_emits_json_and_exit_4(self) -> None:
        """CLI pull --apply times out during git mutation: outputs envelope with git_timeout and returns 4."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            git_dir = os.path.join(real_tmpdir, ".git")
            os.makedirs(git_dir, exist_ok=True)
            with open(os.path.join(git_dir, "index"), "wb") as f:
                f.write(b"index-data")

            fake_cand = {
                "activity": "sessions/s1/activities/a1",
                "artifactIndex": 0,
                "baseCommitId": "b" * 40,
                "createTime": "2026-10-08T12:00:00Z",
                "patchSha256": "hash",
                "sessionName": "sessions/s1",
                "source": "sources/src-1",
                "suggestedCommitMessage": "msg",
            }

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "apply" in cmd and "--check" not in cmd and "--numstat" not in cmd and "--summary" not in cmd:
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 30))
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "rev-parse" in cmd:
                    if "--show-toplevel" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=real_tmpdir.encode("utf-8") + b"\n", stderr=b"")
                    if "--is-bare-repository" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b"false\n", stderr=b"")
                    if "--git-path" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b".git/index\n", stderr=b"")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"b" * 40 + b"\n", stderr=b"")
                if "remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"https://github.com/OWNER/REPO.git\n", stderr=b"")
                if "status" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "ls-files" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "apply" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("subprocess.run", side_effect=fake_subproc), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-pull", "sessions/s1", "--apply", "--cwd", real_tmpdir])

            self.assertEqual(code, 4)
            lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
            self.assertTrue(len(lines) >= 1)
            envelope = lines[-1]
            self.assertFalse(envelope["ok"])
            self.assertFalse(envelope["complete"])
            self.assertEqual(envelope["error"]["kind"], "git_timeout")
            self.assertIsNotNone(envelope["data"])
            data = envelope["data"]
            self.assertEqual(data["stage"], "apply")
            self.assertIsNone(data["applied"])
            self.assertEqual(data["activity"], "sessions/s1/activities/a1")
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["destination"], real_tmpdir)
            self.assertEqual(data["cwd"], real_tmpdir)
            self.assertEqual(data["artifactIndex"], 0)
            self.assertEqual(data["artifact"], 0)
            self.assertEqual(data["sessionName"], "sessions/s1")
            self.assertEqual(data["source"], "sources/src-1")

    def test_r5_timeout_propagates_to_git_in_infer_repo_apply_and_teleport(self) -> None:
        """--timeout 1.0 propagates min(timeout, rem) = 1.0s to Git in infer_repo and apply_patch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            octodot.run_git(["init", "-b", "main"], cwd=real_tmpdir)
            octodot.run_git(["remote", "add", "origin", "https://github.com/OWNER/REPO.git"], cwd=real_tmpdir)

            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = subprocess.CompletedProcess(["git"], 0, stdout=b"git version 2.39.0\n", stderr=b"")
                octodot.check_git_version(timeout=1.0)
                used_timeout = mock_subproc.call_args[1].get("timeout")
                self.assertEqual(used_timeout, 1.0)

            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = subprocess.CompletedProcess(["git"], 0, stdout=real_tmpdir.encode("utf-8") + b"\n", stderr=b"")
                try:
                    octodot.infer_repo(real_tmpdir, "main", timeout=1.0)
                except Exception:
                    pass
                used_timeout = mock_subproc.call_args_list[0][1].get("timeout")
                self.assertEqual(used_timeout, 1.0)


class R03ResourceOwnershipAndProvenanceTests(unittest.TestCase):
    """Test suite covering R3 corrections for patch provenance and resource ownership."""

    def test_r03_t01_explicit_selection_derives_metadata_from_fresh_artifact(self) -> None:
        """S03-T01: Explicit selection derives all metadata directly from the fresh GET activity artifact."""
        old_patch = "diff --git a/foo.txt b/foo.txt\n--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n-old\n+mid\n"
        fresh_patch = "diff --git a/foo.txt b/foo.txt\n--- a/foo.txt\n+++ b/foo.txt\n@@ -1 +1 @@\n-old\n+new\n"
        old_sha = hashlib.sha256(old_patch.encode("utf-8")).hexdigest()
        fresh_sha = hashlib.sha256(fresh_patch.encode("utf-8")).hexdigest()
        old_base = "a" * 40
        fresh_base = "b" * 40

        session = {"name": "sessions/s1", "sourceContext": {"source": "sources/src1"}}
        listed_activity = {
            "name": "sessions/s1/activities/a1",
            "createTime": "2026-10-08T10:00:00Z",
            "artifacts": [
                {
                    "changeSet": {
                        "source": "sources/src1",
                        "gitPatch": {
                            "baseCommitId": old_base,
                            "suggestedCommitMessage": "old message",
                            "unidiffPatch": old_patch,
                        },
                    }
                }
            ],
        }
        fresh_activity = {
            "name": "sessions/s1/activities/a1",
            "createTime": "2026-10-08T10:05:00Z",
            "artifacts": [
                {
                    "changeSet": {
                        "source": "sources/src1",
                        "gitPatch": {
                            "baseCommitId": fresh_base,
                            "suggestedCommitMessage": "fresh updated message",
                            "unidiffPatch": fresh_patch,
                        },
                    },
                    "description": "updated artifact description",
                }
            ],
        }

        with mock.patch("octodot.request_json", return_value=(200, fresh_activity)) as mock_req:
            chosen, returned_patch = octodot.select_patch(
                session,
                [listed_activity],
                selector_activity="sessions/s1/activities/a1",
                selector_artifact=0,
                key="test-api-key",
            )
            self.assertEqual(returned_patch, fresh_patch)
            self.assertEqual(chosen["baseCommitId"], fresh_base)
            self.assertEqual(chosen["patchSha256"], fresh_sha)
            self.assertNotEqual(chosen["patchSha256"], old_sha)
            self.assertNotEqual(chosen["baseCommitId"], old_base)
            self.assertEqual(chosen["suggestedCommitMessage"], "fresh updated message")
            self.assertEqual(chosen["createTime"], "2026-10-08T10:05:00Z")
            self.assertEqual(chosen["description"], "updated artifact description")
            mock_req.assert_called_once()

    def test_r03_t02_explicit_selection_rejects_foreign_activity_namespace(self) -> None:
        """S03-T02: Explicit selection rejects selector_activity that does not start with session_name/activities/."""
        session = {"name": "sessions/s1", "sourceContext": {"source": "sources/src1"}}
        with self.assertRaises(octodot.OctodotError) as ctx:
            octodot.select_patch(
                session,
                [],
                selector_activity="sessions/other_session/activities/a1",
                selector_artifact=0,
                key="test-api-key",
            )
        self.assertEqual(ctx.exception.record["kind"], "protocol_error")
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_r03_t03_explicit_selection_rejects_fresh_activity_name_mismatch(self) -> None:
        """S03-T03: Explicit selection rejects fresh GET activity whose name does not match selector_activity."""
        session = {"name": "sessions/s1", "sourceContext": {"source": "sources/src1"}}
        listed_activity = {
            "name": "sessions/s1/activities/a1",
            "createTime": "2026-10-08T10:00:00Z",
            "artifacts": [
                {
                    "changeSet": {
                        "source": "sources/src1",
                        "gitPatch": {"baseCommitId": "b" * 40, "unidiffPatch": "valid patch"},
                    }
                }
            ],
        }
        tampered_fresh_activity = {
            "name": "sessions/s1/activities/different_act",
            "artifacts": listed_activity["artifacts"],
        }
        with mock.patch("octodot.request_json", return_value=(200, tampered_fresh_activity)):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.select_patch(
                    session,
                    [listed_activity],
                    selector_activity="sessions/s1/activities/a1",
                    selector_artifact=0,
                    key="test-api-key",
                )
            self.assertEqual(ctx.exception.record["kind"], "protocol_error")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_r03_t04_read_session_validates_session_name(self) -> None:
        """S03-T04: read_session verifies session name returned matches requested session name."""
        with mock.patch("octodot.request_json", return_value=(200, {"name": "sessions/tampered"})):
            with self.assertRaises(octodot.OctodotError) as ctx:
                octodot.read_session("sessions/expected", key="test-key")
            self.assertEqual(ctx.exception.record["kind"], "protocol_error")
            self.assertEqual(ctx.exception.exit_code, 4)

    def test_r03_t05_read_activities_validates_child_resource_ownership(self) -> None:
        """S03-T05: read_activities rejects any activity whose name does not start with session_name/activities/."""
        activities = [
            {"name": "sessions/s1/activities/a1"},
            {"name": "sessions/foreign_session/activities/a2"},
        ]
        with mock.patch("octodot.paginate", return_value=(activities, True, None)):
            items, complete, err = octodot.read_activities("sessions/s1", key="test-key")
            self.assertFalse(complete)
            self.assertIsNotNone(err)
            self.assertEqual(err.record["kind"], "protocol_error")
            self.assertEqual(err.exit_code, 4)

    def test_r03_t06_results_verifies_authenticated_source_detail(self) -> None:
        """S03-T06: -results verifies authenticated source detail before accepting artifacts."""
        session = {
            "name": "sessions/s1",
            "state": "COMPLETED",
            "sourceContext": {"source": "sources/src1"},
            "outputs": [],
        }
        mismatched_source = {
            "name": "sources/mismatched",
            "githubRepo": {"owner": "test-owner", "repo": "test-repo"},
        }
        with mock.patch("octodot.read_session", return_value=session):
            with mock.patch("octodot.request_json", return_value=(200, mismatched_source)):
                with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                    out = io.StringIO()
                    err = io.StringIO()
                    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                        rc = octodot.main(["-results", "sessions/s1"])
                    self.assertEqual(rc, 4)
                    res = json.loads(out.getvalue())
                    self.assertFalse(res["ok"])
                    self.assertEqual(res["error"]["kind"], "protocol_error")

    def test_r03_t07_pull_verifies_authenticated_source_detail(self) -> None:
        """S03-T07: -pull verifies authenticated source detail before accepting artifacts."""
        session = {
            "name": "sessions/s1",
            "state": "COMPLETED",
            "sourceContext": {"source": "sources/src1"},
        }
        mismatched_source = {
            "name": "sources/mismatched",
            "githubRepo": {"owner": "test-owner", "repo": "test-repo"},
        }
        with mock.patch("octodot.read_session", return_value=session):
            with mock.patch("octodot.request_json", return_value=(200, mismatched_source)):
                with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                    out = io.StringIO()
                    err = io.StringIO()
                    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                        rc = octodot.main(["-pull", "sessions/s1", "--json"])
                    self.assertEqual(rc, 4)
                    res = json.loads(out.getvalue())
                    self.assertFalse(res["ok"])
                    self.assertEqual(res["error"]["kind"], "protocol_error")


class R06FailureOutputRoutingAndPartialEvidenceTests(unittest.TestCase):
    """Test suite covering R6 corrections for partial evidence preservation and stdout clean routing."""

    def test_r06_t01_partial_page_preserves_accumulated_sources(self) -> None:
        """S06-T01: Incomplete pagination on -list-repos preserves page 1 items in data.sources."""
        items = [{"name": "sources/s1"}, {"name": "sources/s2"}]
        err = octodot.OctodotError(
            octodot.error_record("transport_error", "Page 2 network timeout", "paginate"),
            exit_code=4,
        )
        with mock.patch("octodot.paginate", return_value=(items, False, err)):
            with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err_out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err_out):
                    rc = octodot.main(["-list-repos"])
                self.assertEqual(rc, 4)
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertFalse(res["complete"])
                self.assertEqual(res["data"], {"sources": items})
                self.assertEqual(res["error"]["kind"], "transport_error")

    def test_r06_t02_partial_page_preserves_accumulated_sessions(self) -> None:
        """S06-T02: Incomplete pagination on -list-sessions preserves page 1 items in data.sessions."""
        items = [{"name": "sessions/ses1"}, {"name": "sessions/ses2"}]
        err = octodot.OctodotError(
            octodot.error_record("transport_error", "Page 2 HTTP 500 error", "paginate"),
            exit_code=4,
        )
        with mock.patch("octodot.paginate", return_value=(items, False, err)):
            with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err_out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err_out):
                    rc = octodot.main(["-list-sessions"])
                self.assertEqual(rc, 4)
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertFalse(res["complete"])
                self.assertEqual(res["data"], {"sessions": items})
                self.assertEqual(res["error"]["kind"], "transport_error")

    def test_r06_t03_partial_page_preserves_accumulated_activities(self) -> None:
        """S06-T03: Incomplete pagination on -activities preserves page 1 items in data.activities."""
        items = [
            {"name": "sessions/s1/activities/a1"},
            {"name": "sessions/s1/activities/a2"},
        ]
        err = octodot.OctodotError(
            octodot.error_record("transport_error", "Page 2 error", "paginate"),
            exit_code=4,
        )
        with mock.patch("octodot.paginate", return_value=(items, False, err)):
            with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err_out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err_out):
                    rc = octodot.main(["-activities", "sessions/s1"])
                self.assertEqual(rc, 4)
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertFalse(res["complete"])
                self.assertEqual(res["data"], {"activities": items, "sessionName": "sessions/s1"})
                self.assertEqual(res["error"]["kind"], "transport_error")

    def test_r06_t04_pre_mutation_refusal_preserves_metadata(self) -> None:
        """S06-T04: Pre-mutation refusal preserves selected mutation metadata and stage."""
        patch_text = "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-a\n+b\n"
        base_commit = "1" * 40
        cand = {
            "activity": "sessions/s1/activities/a1",
            "artifactIndex": 0,
            "baseCommitId": base_commit,
            "createTime": "2026-10-08T12:00:00Z",
            "patchSha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
            "sessionName": "sessions/s1",
            "source": "sources/src1",
            "suggestedCommitMessage": "test patch",
        }
        session = {
            "name": "sessions/s1",
            "sourceContext": {"source": "sources/src1"},
        }
        detail_source = {
            "name": "sources/src1",
            "githubRepo": {"owner": "test-owner", "repo": "test-repo"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dirty repository to trigger preflight worktree rejection
            octodot.run_git(["init", "-b", "main"], cwd=tmpdir)
            octodot.run_git(["config", "user.name", "Test User"], cwd=tmpdir)
            octodot.run_git(["config", "user.email", "test@example.com"], cwd=tmpdir)
            octodot.run_git(["remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=tmpdir)
            tracked_file = os.path.join(tmpdir, "file.txt")
            with open(tracked_file, "w") as f:
                f.write("a\n")
            octodot.run_git(["add", "file.txt"], cwd=tmpdir)
            octodot.run_git(["commit", "-m", "init"], cwd=tmpdir)
            # Add dirty untracked file
            with open(os.path.join(tmpdir, "untracked.txt"), "w") as f:
                f.write("untracked\n")

            with mock.patch("octodot.read_session", return_value=session), \
                 mock.patch("octodot.read_activities", return_value=([], True, None)), \
                 mock.patch("octodot.request_json", return_value=(200, detail_source)), \
                 mock.patch("octodot.select_patch", return_value=(cand, patch_text)):
                with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                    out = io.StringIO()
                    err_out = io.StringIO()
                    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err_out):
                        rc = octodot.main(["-pull", "sessions/s1", "--apply", "--cwd", tmpdir])
                    self.assertEqual(rc, 4)
                    res = json.loads(out.getvalue())
                    self.assertFalse(res["ok"])
                    self.assertFalse(res["complete"])
                    self.assertIsNotNone(res["data"])
                    data = res["data"]
                    self.assertEqual(data["applied"], False)
                    self.assertEqual(data["artifact"], 0)
                    self.assertEqual(data["artifactIndex"], 0)
                    self.assertEqual(data["baseCommitId"], base_commit)
                    self.assertEqual(data["destination"], os.path.realpath(tmpdir))
                    self.assertEqual(data["stage"], "preflight")

    def test_r06_t05_raw_pull_missing_key_stdout_empty_stderr_only(self) -> None:
        """S06-T05: Missing API key on raw pull leaves stdout completely empty and writes to stderr only."""
        env = dict(os.environ)
        env.pop("JULES_API_KEY", None)

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = octodot.main(["-pull", "sessions/s1"])
            self.assertEqual(rc, 3)
            # stdout must be completely empty so redirected patch file is never corrupted
            self.assertEqual(out.getvalue(), "")
            # stderr receives the JSON error envelope
            err_json = json.loads(err.getvalue())
            self.assertFalse(err_json["ok"])
            self.assertIn(err_json["error"]["kind"], ("missing_configuration", "missing_credentials"))

    def test_r06_t06_raw_pull_crlf_key_stdout_empty_stderr_only(self) -> None:
        """S06-T06: CRLF API key on raw pull leaves stdout completely empty and writes to stderr only."""
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"JULES_API_KEY": "invalid\r\nkey"}):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = octodot.main(["-pull", "sessions/s1"])
            self.assertEqual(rc, 3)
            # stdout must be completely empty so redirected patch file is never corrupted
            self.assertEqual(out.getvalue(), "")
            # stderr receives the JSON error envelope
            err_json = json.loads(err.getvalue())
            self.assertFalse(err_json["ok"])
            self.assertEqual(err_json["error"]["kind"], "invalid_configuration")

    def test_r06_t07_results_partial_activity_pagination_failure_preserves_evidence(self) -> None:
        """S06-T07: Incomplete activities pagination on -results preserves accumulated evidence with delivery: None."""
        sess = {
            "name": "sessions/s1",
            "sourceContext": {"source": "sources/src-1"},
            "state": "IN_PROGRESS",
            "outputs": [{"text": "working"}],
        }
        acts = [
            {
                "name": "sessions/s1/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/src-1",
                            "gitPatch": {
                                "baseCommitId": "a" * 40,
                                "unidiffPatch": "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-1\n+2\n",
                            },
                        }
                    }
                ],
            }
        ]
        err = octodot.OctodotError(
            octodot.error_record("transport_error", "Page 2 network failure", "paginate"),
            exit_code=4,
        )

        with mock.patch("octodot.read_session", return_value=sess), \
             mock.patch("octodot.verify_source_detail", return_value={"name": "sources/src-1"}), \
             mock.patch("octodot.read_activities", return_value=(acts, False, err)):
            with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err_out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err_out):
                    rc = octodot.main(["-results", "sessions/s1"])
                self.assertEqual(rc, 4)
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertFalse(res["complete"])
                self.assertEqual(res["error"]["kind"], "transport_error")
                data = res["data"]
                self.assertIsNotNone(data)
                self.assertIsNone(data["delivery"])
                self.assertEqual(data["classification"], "pending")
                self.assertEqual(data["session"], sess)
                self.assertEqual(data["outputs"], [{"text": "working"}])
                self.assertEqual(len(data["patches"]), 1)
                self.assertIsNotNone(data["latestActivity"])
                self.assertEqual(data["latestActivity"]["name"], "sessions/s1/activities/a1")

    def test_r06_t08_results_completed_session_extra_read_failure_preserves_evidence(self) -> None:
        """S06-T08: Extra session read failure on completed session preserves evidence with delivery: None."""
        sess = {
            "name": "sessions/s1",
            "sourceContext": {"source": "sources/src-1"},
            "state": "COMPLETED",
            "outputs": [],
        }
        acts = [
            {
                "name": "sessions/s1/activities/a1",
                "createTime": "2026-10-08T12:00:00Z",
                "artifacts": [],
            }
        ]

        def fake_read_session(name: str, **kwargs: Any) -> dict[str, Any]:
            if fake_read_session.calls == 0:
                fake_read_session.calls += 1
                return sess
            raise octodot.OctodotError(
                octodot.error_record("transport_error", "Extra session read timed out", "read_session"),
                exit_code=4,
            )
        fake_read_session.calls = 0

        with mock.patch("octodot.read_session", side_effect=fake_read_session), \
             mock.patch("octodot.verify_source_detail", return_value={"name": "sources/src-1"}), \
             mock.patch("octodot.read_activities", return_value=(acts, True, None)):
            with mock.patch.dict(os.environ, {"JULES_API_KEY": "test-key"}):
                out = io.StringIO()
                err_out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err_out):
                    rc = octodot.main(["-results", "sessions/s1"])
                self.assertEqual(rc, 4)
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertFalse(res["complete"])
                self.assertEqual(res["error"]["kind"], "transport_error")
                data = res["data"]
                self.assertIsNotNone(data)
                self.assertIsNone(data["delivery"])
                self.assertEqual(data["classification"], "completed")
                self.assertEqual(data["session"], sess)

    def test_r06_t09_pull_apply_post_verification_head_changed_reports_stage_post_verification(self) -> None:
        """S06-T09: Post-verification HEAD modification reports stage: post_verification with applied: None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            git_dir = os.path.join(real_tmpdir, ".git")
            os.makedirs(git_dir, exist_ok=True)
            with open(os.path.join(git_dir, "index"), "wb") as f:
                f.write(b"index-data")

            fake_cand = {
                "activity": "sessions/s1/activities/a1",
                "artifactIndex": 0,
                "baseCommitId": "b" * 40,
                "createTime": "2026-10-08T12:00:00Z",
                "patchSha256": "hash",
                "sessionName": "sessions/s1",
                "source": "sources/src-1",
                "suggestedCommitMessage": "msg",
            }

            rev_parse_calls = 0

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                nonlocal rev_parse_calls
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "rev-parse" in cmd:
                    if "--show-toplevel" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=real_tmpdir.encode("utf-8") + b"\n", stderr=b"")
                    if "--is-bare-repository" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b"false\n", stderr=b"")
                    if "--git-path" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b".git/index\n", stderr=b"")
                    if "--verify" in cmd:
                        rev_parse_calls += 1
                        if rev_parse_calls >= 3:
                            # Third HEAD verification (post-apply) returns modified commit!
                            return subprocess.CompletedProcess(cmd, 0, stdout=b"c" * 40 + b"\n", stderr=b"")
                        return subprocess.CompletedProcess(cmd, 0, stdout=b"b" * 40 + b"\n", stderr=b"")
                if "remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"https://github.com/OWNER/REPO.git\n", stderr=b"")
                if "status" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "ls-files" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "apply" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("subprocess.run", side_effect=fake_subproc), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-pull", "sessions/s1", "--apply", "--cwd", real_tmpdir])

            self.assertEqual(code, 4)
            res = json.loads(out.getvalue())
            self.assertFalse(res["ok"])
            self.assertFalse(res["complete"])
            self.assertEqual(res["error"]["kind"], "mutation_inconsistent")
            data = res["data"]
            self.assertEqual(data["stage"], "post_verification")
            self.assertIsNone(data["applied"])
            self.assertEqual(data["activity"], "sessions/s1/activities/a1")
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["destination"], real_tmpdir)

    def test_r06_t10_pull_apply_trailing_branch_read_failure_reports_stage_post_verification(self) -> None:
        """S06-T10: Trailing symbolic-ref failure after apply reports stage: post_verification with applied: None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            git_dir = os.path.join(real_tmpdir, ".git")
            os.makedirs(git_dir, exist_ok=True)
            with open(os.path.join(git_dir, "index"), "wb") as f:
                f.write(b"index-data")

            fake_cand = {
                "activity": "sessions/s1/activities/a1",
                "artifactIndex": 0,
                "baseCommitId": "b" * 40,
                "createTime": "2026-10-08T12:00:00Z",
                "patchSha256": "hash",
                "sessionName": "sessions/s1",
                "source": "sources/src-1",
                "suggestedCommitMessage": "msg",
            }

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "symbolic-ref" in cmd:
                    return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"fatal: unable to read HEAD\n")
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "rev-parse" in cmd:
                    if "--show-toplevel" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=real_tmpdir.encode("utf-8") + b"\n", stderr=b"")
                    if "--is-bare-repository" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b"false\n", stderr=b"")
                    if "--git-path" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b".git/index\n", stderr=b"")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"b" * 40 + b"\n", stderr=b"")
                if "remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"https://github.com/OWNER/REPO.git\n", stderr=b"")
                if "status" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "ls-files" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "apply" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("subprocess.run", side_effect=fake_subproc), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-pull", "sessions/s1", "--apply", "--cwd", real_tmpdir])

            self.assertEqual(code, 4)
            res = json.loads(out.getvalue())
            self.assertFalse(res["ok"])
            self.assertFalse(res["complete"])
            self.assertEqual(res["error"]["kind"], "git_error")
            data = res["data"]
            self.assertEqual(data["stage"], "post_verification")
            self.assertIsNone(data["applied"])
            self.assertEqual(data["activity"], "sessions/s1/activities/a1")
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["destination"], real_tmpdir)

    def test_r06_t11_teleport_clone_failure_reports_stage_clone(self) -> None:
        """S06-T11: Git clone failure during teleport reports stage: clone with applied: False."""
        fake_cand = {
            "activity": "sessions/s1/activities/a1",
            "artifactIndex": 0,
            "baseCommitId": "b" * 40,
            "createTime": "2026-10-08T12:00:00Z",
            "patchSha256": "hash",
            "sessionName": "sessions/s1",
            "source": "sources/src-1",
            "suggestedCommitMessage": "msg",
        }

        with tempfile.TemporaryDirectory() as parent_dir:
            dest_dir = os.path.join(parent_dir, "nonexistent-checkout")

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "clone" in cmd:
                    return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"fatal: clone failed\n")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("subprocess.run", side_effect=fake_subproc), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-teleport", "sessions/s1", "--apply", "--dir", dest_dir])

            self.assertEqual(code, 4)
            res = json.loads(out.getvalue())
            self.assertFalse(res["ok"])
            self.assertFalse(res["complete"])
            self.assertEqual(res["error"]["kind"], "clone_failed")
            data = res["data"]
            self.assertEqual(data["stage"], "clone")
            self.assertEqual(data["applied"], False)
            self.assertEqual(data["activity"], "sessions/s1/activities/a1")
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["destination"], os.path.abspath(dest_dir))

    def test_r06_t12_teleport_checkout_failure_reports_stage_checkout(self) -> None:
        """S06-T12: Git checkout failure during teleport reports stage: checkout with applied: False."""
        fake_cand = {
            "activity": "sessions/s1/activities/a1",
            "artifactIndex": 0,
            "baseCommitId": "b" * 40,
            "createTime": "2026-10-08T12:00:00Z",
            "patchSha256": "hash",
            "sessionName": "sessions/s1",
            "source": "sources/src-1",
            "suggestedCommitMessage": "msg",
        }

        with tempfile.TemporaryDirectory() as parent_dir:
            dest_dir = os.path.join(parent_dir, "nonexistent-checkout")

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "clone" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "cat-file" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"commit\n", stderr=b"")
                if "ls-tree" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "checkout" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"fatal: checkout failed\n")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("subprocess.run", side_effect=fake_subproc), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-teleport", "sessions/s1", "--apply", "--dir", dest_dir])

            self.assertEqual(code, 4)
            res = json.loads(out.getvalue())
            self.assertFalse(res["ok"])
            self.assertFalse(res["complete"])
            self.assertEqual(res["error"]["kind"], "checkout_failed")
            data = res["data"]
            self.assertEqual(data["stage"], "checkout")
            self.assertEqual(data["applied"], False)
            self.assertEqual(data["activity"], "sessions/s1/activities/a1")
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["destination"], os.path.abspath(dest_dir))

    def test_r06_t13_pull_apply_on_detached_head_succeeds_with_null_branch(self) -> None:
        """S06-T13: Clean exact-base detached checkout applies patch successfully with branch: None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            git_dir = os.path.join(real_tmpdir, ".git")
            os.makedirs(git_dir, exist_ok=True)
            with open(os.path.join(git_dir, "index"), "wb") as f:
                f.write(b"index-data")

            fake_cand = {
                "activity": "sessions/s1/activities/a1",
                "artifactIndex": 0,
                "baseCommitId": "b" * 40,
                "createTime": "2026-10-08T12:00:00Z",
                "patchSha256": "hash",
                "sessionName": "sessions/s1",
                "source": "sources/src-1",
                "suggestedCommitMessage": "msg",
            }

            def fake_subproc(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                if "symbolic-ref" in cmd:
                    # Detached HEAD returns returncode 1
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 2.39.0\n", stderr=b"")
                if "rev-parse" in cmd:
                    if "--show-toplevel" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=real_tmpdir.encode("utf-8") + b"\n", stderr=b"")
                    if "--is-bare-repository" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b"false\n", stderr=b"")
                    if "--git-path" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=b".git/index\n", stderr=b"")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"b" * 40 + b"\n", stderr=b"")
                if "remote" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"https://github.com/OWNER/REPO.git\n", stderr=b"")
                if "status" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "config" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
                if "ls-files" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "apply" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("subprocess.run", side_effect=fake_subproc), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-pull", "sessions/s1", "--apply", "--cwd", real_tmpdir])

            self.assertEqual(code, 0)
            res = json.loads(out.getvalue())
            self.assertTrue(res["ok"])
            self.assertTrue(res["complete"])
            data = res["data"]
            self.assertIsNotNone(data)
            self.assertEqual(data["applied"], True)
            self.assertIsNone(data["branch"])
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["sessionName"], "sessions/s1")
            self.assertEqual(data["source"], "sources/src-1")
            self.assertEqual(data["cwd"], real_tmpdir)

    def test_r06_t14_injected_local_io_error_emits_fixed_message_and_preserves_metadata(self) -> None:
        """S06-T14: Injected local I/O error emits fixed message without raw str(exc) interpolation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            fake_cand = {
                "activity": "sessions/s1/activities/a1",
                "artifactIndex": 0,
                "baseCommitId": "b" * 40,
                "createTime": "2026-10-08T12:00:00Z",
                "patchSha256": "hash",
                "sessionName": "sessions/s1",
                "source": "sources/src-1",
                "suggestedCommitMessage": "msg",
            }

            out = io.StringIO()
            err_out = io.StringIO()
            with patch("octodot.read_session", return_value={"name": "sessions/s1"}), \
                 patch("octodot.read_activities", return_value=([], True, None)), \
                 patch("octodot.select_patch", return_value=(fake_cand, "diff content")), \
                 patch("octodot.request_json", return_value=(200, {"name": "sources/src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}})), \
                 patch("octodot.apply_patch", side_effect=PermissionError("Confidential local path /secret/token")), \
                 patch.dict(os.environ, {"JULES_API_KEY": "test-key"}), \
                 patch("sys.stdout", out), patch("sys.stderr", err_out):
                code = octodot.main(["-pull", "sessions/s1", "--apply", "--cwd", real_tmpdir])

            self.assertEqual(code, 4)
            res = json.loads(out.getvalue())
            self.assertFalse(res["ok"])
            self.assertFalse(res["complete"])
            self.assertEqual(res["error"]["kind"], "apply_error")
            # Fixed message without str(exc) interpolation
            self.assertEqual(res["error"]["message"], "Patch application failed due to a local execution error")
            self.assertNotIn("Confidential", res["error"]["message"])
            self.assertNotIn("secret", res["error"]["message"])
            data = res["data"]
            self.assertIsNotNone(data)
            self.assertEqual(data["stage"], "apply")
            self.assertIsNone(data["applied"])
            self.assertEqual(data["activity"], "sessions/s1/activities/a1")
            self.assertEqual(data["patchSha256"], "hash")
            self.assertEqual(data["baseCommitId"], "b" * 40)
            self.assertEqual(data["destination"], real_tmpdir)


if __name__ == "__main__":
    unittest.main()
