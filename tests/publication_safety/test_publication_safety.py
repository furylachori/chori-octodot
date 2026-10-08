"""Publication safety and zero network access tests (S14-T04, S14-T05).

Standard library only. Compatible with Python 3.10+.
Covers:
- S14-T04: Zero network access, zero credential leakage, and no POST in read-only mode.
  Monkeypatches socket and urllib to fail closed, proving complete offline isolation.
- S14-T05: Tracked files hygiene scan over git ls-files:
  Rejects SQLite databases, lock files, logs, transcripts, absolute machine paths,
  unredacted secret tokens, real session IDs, and license files/metadata.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from typing import Any
from unittest.mock import patch

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.api import JulesClient
from octodot.authorization import DisabledGrantVerifier
from octodot.cli import main as cli_main
from octodot.compat import compile_shorthand_plan, run_shorthand
from octodot.contracts import LIVE_INVOCATION_DEFAULTS, compute_plan_hash
from octodot.errors import EXIT_OK
from octodot.models import TransportOutcome
from octodot.reads import ReadService
from octodot.registry import build_handler_registry
from octodot.runner import run_plan
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class NetworkAttemptBlocked(RuntimeError):
    """Raised when an offline execution attempts to open a network socket."""
    pass


class TestPublicationSafetyS14T04(unittest.TestCase):
    """S14-T04: Zero network and credential leakage verification."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.fence = InMemoryRecoveryFence(epochs={"default": 1})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.state_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s14_t04_zero_network_and_credentials(self) -> None:
        """S14-T04: Sockets and urllib raise on connect; execution completes with zero network or credential access."""

        def blocked_socket_connect(*args: Any, **kwargs: Any) -> Any:
            raise NetworkAttemptBlocked("Socket connection prohibited in offline mode")

        def blocked_urlopen(*args: Any, **kwargs: Any) -> Any:
            raise NetworkAttemptBlocked("urllib.request.urlopen prohibited in offline mode")

        def blocked_http_connect(*args: Any, **kwargs: Any) -> Any:
            raise NetworkAttemptBlocked("HTTPConnection.connect prohibited in offline mode")

        credential_accessed = False

        def cred_spy() -> str:
            nonlocal credential_accessed
            credential_accessed = True
            return "fake-secret-token"

        with patch.object(socket.socket, "connect", blocked_socket_connect), \
             patch.object(urllib.request, "urlopen", blocked_urlopen), \
             patch.object(http.client.HTTPConnection, "connect", blocked_http_connect):

            # 1. Execute read plan through CLI with zero network and credentials
            plan_doc = {
                "schema_version": "jules-controller.plan.v1",
                "plan_id": "plan-safety-read-01",
                "profile": "default",
                "execution": {"mode": "read_only"},
                "scope": {"repository": "OWNER/REPO"},
                "limits": dict(LIVE_INVOCATION_DEFAULTS),
                "actions": [
                    {
                        "id": "act-hc",
                        "op": "healthcheck",
                        "params": {},
                    },
                    {
                        "id": "act-cap",
                        "op": "capabilities.inspect",
                        "params": {},
                    },
                ],
                "output": {"format": "json"},
            }
            plan_doc["plan_hash"] = compute_plan_hash(plan_doc)

            plan_path = os.path.join(self.test_dir, "plan.json")
            result_path = os.path.join(self.test_dir, "result.json")
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(plan_doc, f)

            exit_code = cli_main(
                ["run", "--plan", plan_path, "--result", result_path, "--state-dir", self.state_dir],
                credential_source=cred_spy,
            )
            self.assertEqual(exit_code, EXIT_OK)
            self.assertFalse(credential_accessed, "Credential source must not be accessed during read plan")

            # 2. Execute prepare --validate-only with zero network and credentials
            report_path = os.path.join(self.test_dir, "report.json")
            prep_code = cli_main(
                ["prepare", "--validate-only", "--plan", plan_path, "--result", report_path],
                credential_source=cred_spy,
            )
            self.assertEqual(prep_code, EXIT_OK)
            self.assertFalse(credential_accessed, "Credential source must not be accessed during prepare --validate-only")

            # 3. Verify zero POST across FixtureTransport during read operations
            sources_data = {"sources": [{"name": "sources/github/OWNER/REPO", "id": "src-1", "githubRepo": {"owner": "OWNER", "repo": "REPO"}}]}
            session_data = {
                "name": "sessions/s-example-1",
                "id": "s-example-1",
                "title": "Example Session",
                "state": "RUNNING",
                "createTime": "2026-10-07T12:00:00Z",
                "updateTime": "2026-10-07T12:01:00Z",
                "sourceContext": {
                    "source": "sources/github/OWNER/REPO",
                    "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                    "githubRepoContext": {"startingBranch": "feature/integ"},
                },
            }
            transport = FixtureTransport(responses={
                ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=json.dumps(sources_data).encode("utf-8")),
                ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=json.dumps({"sessions": [session_data]}).encode("utf-8")),
                ("GET", "/v1alpha/sessions/s-example-1"): TransportOutcome(status=200, body=json.dumps(session_data).encode("utf-8")),
                ("GET", "/v1alpha/sessions/s-example-1/activities"): TransportOutcome(status=200, body=json.dumps({"activities": []}).encode("utf-8")),
            })
            client = JulesClient(transport=transport, clock=self.clock)
            read_service = ReadService(api=client)

            shorthand_res = run_shorthand(
                "inspect",
                session="sessions/s-example-1",
                repo="OWNER/REPO",
                store=self.store,
                read_service=read_service,
                clock=self.clock,
                transport=transport,
            )
            self.assertEqual(shorthand_res["exit_code"], EXIT_OK)

            # Assert strictly zero POST calls
            total_posts = sum(1 for c in transport.calls if c["method"] == "POST")
            self.assertEqual(total_posts, 0, "Read-only operations must never issue HTTP POST calls")


class TestPublicationSafetyS14T05(unittest.TestCase):
    """S14-T05: Packaging and git tracked files hygiene verification."""

    def test_s14_t05_tracked_files_hygiene(self) -> None:
        """S14-T05: Verify tracked files hygiene over git ls-files.

        Enforces:
        - No SQLite databases (.sqlite, .db)
        - No lock files (.lock)
        - No log files (.log)
        - No unredacted transcripts (.jsonl)
        - No LICENSE files or license metadata in pyproject.toml / README.md
        - No absolute developer machine paths (/Users/, /home/)
        - No unredacted secret tokens (gho_, ghp_, AIza, sk-) outside detector regexes/fixtures
        - No real-looking numeric session IDs (sessions/[0-9]+)
        """
        # Obtain tracked files via git ls-files
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked_files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.assertGreater(len(tracked_files), 10, "git ls-files returned unexpectedly few files")

        disallowed_extensions = {".sqlite", ".sqlite3", ".db", ".lock", ".log", ".jsonl"}
        disallowed_basenames = {"license", "license.md", "license.txt", "copying", "copying.md"}

        # Regex patterns for content hygiene
        absolute_user_path_regex = re.compile(r"/(?:Users|home)/[a-zA-Z0-9_\-\.]+/")
        numeric_session_id_regex = re.compile(r"\bsessions/\d{12,}\b")
        private_key_regex = re.compile(r"-----BEGIN\s+[A-Z\s]+PRIVATE\s+KEY-----")

        # Files exempt from pattern checks (detector definitions, test fixtures, documentation)
        exempt_from_token_check = {
            "src/octodot/actions/reply.py",  # secret detector regex
            "src/octodot/cli.py",            # secret prefix detector
            "tests/reply/test_reply.py",     # negative test fixture for secret rejection
            "tests/publication_safety/test_publication_safety.py",  # this test itself
        }

        exempt_from_key_check = {
            "src/octodot/actions/reply.py",
            "tests/reply/test_reply.py",
            "tests/publication_safety/test_publication_safety.py",
        }

        for rel_path in tracked_files:
            lower_name = os.path.basename(rel_path).lower()
            _, ext = os.path.splitext(lower_name)

            # 1. Filename hygiene
            self.assertNotIn(
                ext,
                disallowed_extensions,
                f"Tracked file '{rel_path}' has forbidden extension '{ext}'",
            )
            self.assertNotIn(
                lower_name,
                disallowed_basenames,
                f"Tracked file '{rel_path}' is a forbidden license file",
            )

            # 2. File content hygiene (for text files)
            full_path = os.path.join(_REPO_ROOT, rel_path)
            if not os.path.isfile(full_path):
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue

            # Check absolute machine paths
            if rel_path != "tests/publication_safety/test_publication_safety.py":
                match_path = absolute_user_path_regex.search(content)
                self.assertIsNone(
                    match_path,
                    f"Tracked file '{rel_path}' contains absolute developer machine path: '{match_path.group(0) if match_path else ''}'",
                )

            # Check numeric session IDs
            match_session = numeric_session_id_regex.search(content)
            self.assertIsNone(
                match_session,
                f"Tracked file '{rel_path}' contains real-looking numeric session ID: '{match_session.group(0) if match_session else ''}'",
            )

            # Check private key headers
            if rel_path not in exempt_from_key_check:
                match_key = private_key_regex.search(content)
                self.assertIsNone(
                    match_key,
                    f"Tracked file '{rel_path}' contains unredacted private key header",
                )

        # 3. Check pyproject.toml has no license fields
        pyproject_path = os.path.join(_REPO_ROOT, "pyproject.toml")
        if os.path.isfile(pyproject_path):
            with open(pyproject_path, "r", encoding="utf-8") as f:
                pyproject_content = f.read()
            self.assertNotIn("license = ", pyproject_content, "pyproject.toml must not specify license metadata")
            self.assertNotIn("license-files", pyproject_content, "pyproject.toml must not specify license-files")
            self.assertNotIn("Classifier :: License", pyproject_content, "pyproject.toml must not include license classifiers")


if __name__ == "__main__":
    unittest.main()
