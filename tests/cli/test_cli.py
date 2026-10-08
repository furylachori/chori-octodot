"""Comprehensive tests for octodot CLI (jules-controller).

Covers CLI invocations, subcommands, and shorthands:
- run --plan PLAN --result RESULT
- run --plan PLAN (stdout JSON/JSONL)
- prepare --validate-only --plan PLAN
- prepare --online-preflight --plan PLAN
- Shorthand commands: inventory, inspect, chats, healthcheck, wait
- Output formatting and stderr sanitization
- Exit code propagation matching result exit codes
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import sys
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.cli import compile_shorthand_to_plan, main
from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    compute_plan_hash,
    validate_plan,
    validate_result,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
)
from octodot.transport import SpyCredentialSource


class SpyTransportFactory:
    """Transport factory tracking calls for test assertions."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self) -> Any:
        self.call_count += 1
        return None


class TestCliS09(unittest.TestCase):
    """S09 CLI test cases."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_plan_file(self, plan_dict: dict, filename: str = "plan.json") -> str:
        filepath = os.path.join(self.test_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(plan_dict, f, indent=2)
        return filepath

    def _create_sample_plan(self, plan_id: str = "plan-cli-1", op: str = "healthcheck") -> dict:
        plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": plan_id,
            "profile": "default",
            "execution": {"mode": "read_only"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS),
            "actions": [
                {
                    "id": "act-1",
                    "op": op,
                    "params": {},
                }
            ],
            "output": {"format": "json"},
        }
        plan["plan_hash"] = compute_plan_hash(plan)
        return plan

    def test_cli_run_to_result_file(self) -> None:
        """jules-controller run --plan P --result R produces valid result document file."""
        plan = self._create_sample_plan("plan-file-test")
        plan_path = self._write_plan_file(plan)
        result_path = os.path.join(self.test_dir, "result.json")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "run",
                "--plan", plan_path,
                "--result", result_path,
                "--state-dir", self.state_dir,
            ])

        self.assertEqual(code, EXIT_OK)
        self.assertTrue(os.path.exists(result_path))

        with open(result_path, "r", encoding="utf-8") as f:
            res_data = json.load(f)

        validate_result(res_data)
        self.assertEqual(res_data["plan_id"], "plan-file-test")
        self.assertEqual(res_data["exit_code"], EXIT_OK)
        self.assertEqual(res_data["status"], "ok")

    def test_cli_run_to_stdout_json(self) -> None:
        """jules-controller run --plan P outputs JSON to stdout when --result is omitted."""
        plan = self._create_sample_plan("plan-stdout-test")
        plan_path = self._write_plan_file(plan)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "run",
                "--plan", plan_path,
                "--state-dir", self.state_dir,
            ])

        self.assertEqual(code, EXIT_OK)
        out_text = stdout_buf.getvalue()
        res_data = json.loads(out_text)
        validate_result(res_data)
        self.assertEqual(res_data["plan_id"], "plan-stdout-test")

    def test_cli_run_jsonl_format(self) -> None:
        """jules-controller run --format jsonl outputs newline-delimited JSON."""
        plan = self._create_sample_plan("plan-jsonl-test")
        plan_path = self._write_plan_file(plan)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "run",
                "--plan", plan_path,
                "--format", "jsonl",
                "--state-dir", self.state_dir,
            ])

        self.assertEqual(code, EXIT_OK)
        lines = [line.strip() for line in stdout_buf.getvalue().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)  # At least 1 action_result + 1 summary record
        for line in lines:
            parsed = json.loads(line)
            self.assertIsInstance(parsed, dict)

    def test_cli_run_nonexistent_plan_fails(self) -> None:
        """jules-controller run with missing plan file exits with code 3 and diagnostic."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "run",
                "--plan", os.path.join(self.test_dir, "does_not_exist.json"),
            ])

        self.assertEqual(code, EXIT_FATAL_READ_OR_LOCAL)
        self.assertIn("not found", stderr_buf.getvalue().lower())

    def test_cli_prepare_validate_only(self) -> None:
        """jules-controller prepare --validate-only executes offline validation and writes report."""
        plan = self._create_sample_plan("plan-prep-test")
        plan_path = self._write_plan_file(plan)
        report_path = os.path.join(self.test_dir, "report.json")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "prepare",
                "--validate-only",
                "--plan", plan_path,
                "--result", report_path,
            ])

        self.assertEqual(code, EXIT_OK)
        self.assertTrue(os.path.exists(report_path))
        with open(report_path, "r", encoding="utf-8") as f:
            report_data = json.load(f)

        self.assertEqual(report_data["mode"], "validate_only")
        self.assertEqual(report_data["plan_id"], "plan-prep-test")
        self.assertTrue(report_data["is_valid"])

    def test_cli_prepare_online_preflight(self) -> None:
        """jules-controller prepare --online-preflight performs preflight check."""
        plan = self._create_sample_plan("plan-preflight-test")
        plan_path = self._write_plan_file(plan)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "prepare",
                "--online-preflight",
                "--plan", plan_path,
            ])

        self.assertEqual(code, EXIT_OK)
        res_data = json.loads(stdout_buf.getvalue())
        self.assertEqual(res_data["mode"], "online_preflight")
        self.assertEqual(res_data["plan_id"], "plan-preflight-test")
        self.assertTrue(res_data["preflight_complete"])

    def test_cli_shorthands_compile_and_run(self) -> None:
        """Shorthand commands (inventory, inspect, chats, healthcheck, wait) compile to plans."""
        import argparse
        shorthands = ["inventory", "inspect", "chats", "healthcheck", "wait"]
        for cmd in shorthands:
            ns = argparse.Namespace(
                repo="OWNER/REPO",
                session="sessions/S1",
                profile="default",
                predicate="all_terminal",
                timeout=30.0,
            )
            compiled_plan = compile_shorthand_to_plan(cmd, ns)
            validate_plan(compiled_plan)
            self.assertEqual(compiled_plan["plan_hash"], compute_plan_hash(compiled_plan))

        # Healthcheck shorthand executes through main and produces OK status
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main(["healthcheck", "--state-dir", self.state_dir])

        self.assertEqual(code, EXIT_OK)
        out_data = json.loads(stdout_buf.getvalue())
        validate_result(out_data)
        self.assertEqual(out_data["status"], "ok")

    def test_cli_no_args_prints_help(self) -> None:
        """Running with no arguments outputs help to stderr and returns code 3."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([])

        self.assertEqual(code, EXIT_FATAL_READ_OR_LOCAL)
        self.assertIn("usage: jules-controller", stderr_buf.getvalue())

    def test_cli_whole_plan_validation_rejects_before_credentials_and_network_spy(self) -> None:
        """S09-T01 CLI path: Invalid last action and invalid shorthand reject before credentials or transport."""
        spy_creds = SpyCredentialSource()
        spy_factory = SpyTransportFactory()

        # 1. Invalid last action plan via run
        invalid_plan = {
            "schema_version": "jules-controller.plan.v1",
            "plan_id": "plan-cli-invalid-last",
            "profile": "default",
            "execution": {"mode": "read_only"},
            "scope": {"repository": "OWNER/REPO"},
            "limits": dict(LIVE_INVOCATION_DEFAULTS, max_posts=0),
            "actions": [
                {"id": "act-1", "op": "healthcheck", "params": {}},
                {"id": "act-2", "op": "capabilities.inspect", "params": {}},
                {"id": "act-3", "op": "nonexistent.op", "params": {}},
            ],
            "output": {"format": "json"},
        }
        invalid_plan["plan_hash"] = compute_plan_hash(invalid_plan)
        plan_path = self._write_plan_file(invalid_plan, "invalid_last_plan.json")

        code = main(
            ["run", "--plan", plan_path],
            credential_source=spy_creds,
            transport_factory=spy_factory,
        )
        self.assertEqual(code, EXIT_FATAL_READ_OR_LOCAL)
        self.assertFalse(spy_creds.was_accessed())
        self.assertEqual(spy_factory.call_count, 0)

        # 2. Shorthand with invalid repository format
        code_short = main(
            ["inventory", "--repo", "invalid_no_slash"],
            credential_source=spy_creds,
            transport_factory=spy_factory,
        )
        self.assertEqual(code_short, EXIT_FATAL_READ_OR_LOCAL)
        self.assertFalse(spy_creds.was_accessed())
        self.assertEqual(spy_factory.call_count, 0)


if __name__ == "__main__":
    unittest.main()
