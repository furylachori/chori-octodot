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
from octodot.compat import SUPPORTED_SHORTHANDS
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
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
)
from octodot.events import save_events
from octodot.models import Binding, Event, OperationRecord, OperationState
from octodot.store import SQLiteStore
from octodot.transport import HttpTransport, SpyCredentialSource as BaseSpyCredentialSource, TransportOutcome


class SpyTransportFactory:
    """Transport factory tracking calls for test assertions."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self) -> Any:
        self.call_count += 1
        return None


class SpyCredentialSource(BaseSpyCredentialSource):
    """Spy credential source exposing both access_count and call_count."""

    @property
    def call_count(self) -> int:
        return self.access_count()


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"name": "sessions/s1", "title": "T", "state": "COMPLETED"}'):
        self.status = status
        self._body = body
        self._read_pos = 0

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def read(self, size: int = 65536) -> bytes:
        if self._read_pos >= len(self._body):
            return b""
        chunk = self._body[self._read_pos : self._read_pos + size]
        self._read_pos += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, response_body: bytes = b'{"name": "sessions/s1", "title": "T", "state": "COMPLETED"}'):
        self.open_calls = 0
        self.response_body = response_body

    def open(self, req: Any, timeout: float = 30.0) -> FakeResponse:
        self.open_calls += 1
        return FakeResponse(200, self.response_body)



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
        """jules-controller prepare --online-preflight returns exit 5 with deferred report."""
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

        self.assertEqual(code, EXIT_PARTIAL_OR_UNSUPPORTED)
        res_data = json.loads(stdout_buf.getvalue())
        self.assertEqual(res_data["mode"], "online_preflight")
        self.assertEqual(res_data["plan_id"], "plan-preflight-test")
        self.assertFalse(res_data["preflight_complete"])
        self.assertFalse(res_data["preconditions_verified"])
        self.assertEqual(res_data["error_code"], "unsupported_public_api")

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

    def test_cli_shorthand_status(self) -> None:
        """Shorthand status executes through main and produces OK status result."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main(["status", "--state-dir", self.state_dir])

        self.assertEqual(code, EXIT_OK)
        out_data = json.loads(stdout_buf.getvalue())
        validate_result(out_data)
        self.assertEqual(out_data["status"], "ok")
        self.assertEqual(len(out_data["action_results"]), 1)
        self.assertEqual(out_data["action_results"][0]["op"], "healthcheck")

    def test_cli_shorthand_events(self) -> None:
        """Shorthand events reads durable events via main."""
        store = SQLiteStore(self.state_dir)
        ev = Event.create(
            event_id="evt-cli-test-01",
            event_type="test.event",
            resource_id="sessions/S1",
            payload={"message": "hello"},
        )
        save_events(store, [ev])
        store.close()

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main(["events", "--state-dir", self.state_dir])

        self.assertEqual(code, EXIT_OK)
        out_data = json.loads(stdout_buf.getvalue())
        validate_result(out_data)
        self.assertEqual(out_data["status"], "ok")
        action_results = out_data["action_results"]
        self.assertEqual(len(action_results), 1)
        events = action_results[0]["data"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "evt-cli-test-01")

    def test_cli_shorthand_ack(self) -> None:
        """Shorthand ack acknowledges durable events via main."""
        store = SQLiteStore(self.state_dir)
        ev = Event.create(
            event_id="evt-cli-ack-01",
            event_type="test.event",
            resource_id="sessions/S1",
            payload={"message": "ack-me"},
        )
        save_events(store, [ev])
        store.close()

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "ack",
                "--event-id", "evt-cli-ack-01",
                "--state-dir", self.state_dir,
            ])

        self.assertEqual(code, EXIT_OK)
        out_data = json.loads(stdout_buf.getvalue())
        validate_result(out_data)
        self.assertEqual(out_data["status"], "ok")
        action_results = out_data["action_results"]
        self.assertEqual(len(action_results), 1)
        acked_ids = action_results[0]["data"]["acked_event_ids"]
        self.assertIn("evt-cli-ack-01", acked_ids)

    def test_cli_shorthand_events_session_and_since_filtering(self) -> None:
        """I2: events shorthand with --session and --since filters unacked events correctly."""
        store = SQLiteStore(self.state_dir)
        e1 = Event.create(event_id="E1", event_type="test", resource_id="r1", session_id="session-A")
        e2 = Event.create(event_id="E2", event_type="test", resource_id="r2", session_id="session-B")
        e3 = Event.create(event_id="E3", event_type="test", resource_id="r3", session_id="session-A")
        save_events(store, [e1, e2, e3])
        store.close()

        # 1. events --session session-A -> E1 and E3
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["events", "--session", "session-A", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_OK)
        out = json.loads(buf.getvalue())
        evs = out["action_results"][0]["data"]["events"]
        self.assertEqual([e["event_id"] for e in evs], ["E1", "E3"])

        # 2. events --since E1 -> E2 and E3
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["events", "--since", "E1", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_OK)
        out = json.loads(buf.getvalue())
        evs = out["action_results"][0]["data"]["events"]
        self.assertEqual([e["event_id"] for e in evs], ["E2", "E3"])

        # 3. events --session session-A --since E1 -> only E3
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["events", "--session", "session-A", "--since", "E1", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_OK)
        out = json.loads(buf.getvalue())
        evs = out["action_results"][0]["data"]["events"]
        self.assertEqual([e["event_id"] for e in evs], ["E3"])

        # 4. events --since NONEXISTENT -> empty list
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["events", "--since", "NONEXISTENT", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_OK)
        out = json.loads(buf.getvalue())
        evs = out["action_results"][0]["data"]["events"]
        self.assertEqual(evs, [])

    def test_cli_shorthand_ack_rejections_and_durable_ack(self) -> None:
        """I2: ack shorthand rejects --up-to-seq and empty IDs with exit code 3; accepts valid event ID."""
        store = SQLiteStore(self.state_dir)
        e1 = Event.create(event_id="E-CLI-1", event_type="test", resource_id="r1", session_id="session-A")
        save_events(store, [e1])
        store.close()

        # 1. ack --up-to-seq 5 explicitly refused with exit 3
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with redirect_stderr(err_buf), redirect_stdout(out_buf):
            code = main(["ack", "--up-to-seq", "5", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_FATAL_READ_OR_LOCAL)

        # Verify event remains unacknowledged
        store = SQLiteStore(self.state_dir)
        self.assertFalse(store.is_event_acked("E-CLI-1"))
        store.close()

        # 2. ack with no IDs explicitly refused with exit 3
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with redirect_stderr(err_buf), redirect_stdout(out_buf):
            code = main(["ack", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_FATAL_READ_OR_LOCAL)

        # 3. ack --event-id E-CLI-1 succeeds and persists durably
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["ack", "--event-id", "E-CLI-1", "--state-dir", self.state_dir])
        self.assertEqual(code, EXIT_OK)

        # Verify durable across reopen
        store = SQLiteStore(self.state_dir)
        self.assertTrue(store.is_event_acked("E-CLI-1"))
        store.close()

    def test_cli_shorthand_reconcile(self) -> None:
        """Shorthand reconcile performs uncertain operation reconciliation via main."""
        store = SQLiteStore(self.state_dir)
        op_rec = OperationRecord(
            operation_id="op-cli-rec-01",
            state=OperationState.EFFECT_OBSERVED,
            request_hash="req-hash-01",
            binding=Binding(profile="default", profile_epoch=1, source="sources/github/OWNER/REPO", repository="OWNER/REPO"),
        )
        store.save_operation(op_rec)
        store.close()

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main([
                "reconcile",
                "--operation-id", "op-cli-rec-01",
                "--state-dir", self.state_dir,
            ])

        self.assertEqual(code, EXIT_OK)
        out_data = json.loads(stdout_buf.getvalue())
        validate_result(out_data)
        self.assertEqual(out_data["status"], "ok")
        action_results = out_data["action_results"]
        self.assertEqual(len(action_results), 1)
        rec_data = action_results[0]["data"]
        self.assertEqual(rec_data["reconciled_state"], "effect_observed")

    def test_cli_new_shorthands_validation_rejects_before_credentials(self) -> None:
        """Newly exposed shorthands reject invalid repo before touching credentials or transport."""
        spy_creds = SpyCredentialSource()
        spy_factory = SpyTransportFactory()

        for shorthand_cmd in ("status", "events", "ack"):
            code = main(
                [shorthand_cmd, "--repo", "invalid_no_slash"],
                credential_source=spy_creds,
                transport_factory=spy_factory,
            )
            self.assertEqual(code, EXIT_FATAL_READ_OR_LOCAL)
            self.assertFalse(spy_creds.was_accessed())
            self.assertEqual(spy_factory.call_count, 0)

        # reconcile with invalid repo
        code_rec = main(
            ["reconcile", "--operation-id", "op-1", "--repo", "invalid_no_slash"],
            credential_source=spy_creds,
            transport_factory=spy_factory,
        )
        self.assertEqual(code_rec, EXIT_FATAL_READ_OR_LOCAL)
        self.assertFalse(spy_creds.was_accessed())
        self.assertEqual(spy_factory.call_count, 0)

    def test_cli_credential_env_and_lazy_evaluation(self) -> None:
        """FR1: EnvCredentialSource lazily evaluates env var specified by --credential-env."""
        from octodot.cli import EnvCredentialSource
        source = EnvCredentialSource(env_var="TEST_JULES_CUSTOM_KEY")
        self.assertIsNone(source.get_credential("default"))
        try:
            os.environ["TEST_JULES_CUSTOM_KEY"] = "secret-token-123"
            self.assertEqual(source.get_credential("default"), "secret-token-123")
        finally:
            os.environ.pop("TEST_JULES_CUSTOM_KEY", None)

    def test_cli_compose_runtime_wires_read_service_and_disabled_verifier(self) -> None:
        """FR1: CLI runtime wires ReadService and default DisabledGrantVerifier."""
        from octodot.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["status", "--state-dir", self.state_dir, "--credential-env", "MY_KEY"])
        self.assertEqual(args.credential_env, "MY_KEY")

        # Top-level --credential-env also parses
        args_top = parser.parse_args(["--credential-env", "TOP_KEY", "status", "--state-dir", self.state_dir])
        self.assertEqual(args_top.credential_env, "TOP_KEY")

    def test_cli_plan_http_budget_enforced(self) -> None:
        """Item 2: Real HttpTransport with FakeOpener enforces budget and never makes real network calls."""
        from octodot.cli import _compose_runtime, build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "--plan", "dummy", "--state-dir", self.state_dir])

        # 1. Plan with max_http_requests: 0 and session.inspect action
        plan_0 = self._create_sample_plan(plan_id="plan-budget-0", op="session.inspect")
        plan_0["actions"][0]["params"] = {"session": "sessions/s1"}
        plan_0["limits"]["max_http_requests"] = 0
        plan_0["plan_hash"] = compute_plan_hash(plan_0)

        fake_opener_0 = FakeOpener()
        spy_creds_0 = SpyCredentialSource()
        transport_0 = HttpTransport(credential_source=spy_creds_0, opener=fake_opener_0)

        res_0, store_0 = _compose_runtime(
            args=args,
            plan=plan_0,
            credential_source=spy_creds_0,
            transport=transport_0,
        )
        # Assert fake_opener.open_calls == 0 and credential_spy.call_count == 0
        self.assertEqual(fake_opener_0.open_calls, 0)
        self.assertEqual(spy_creds_0.call_count, 0)
        self.assertFalse(spy_creds_0.was_accessed())
        self.assertEqual(res_0["action_results"][0]["error_code"], ErrorCode.BUDGET_EXHAUSTED.value)

        # 2. Plan with max_http_requests: 1 and multiple actions
        # Action 1 makes 1 HTTP request (inventory.collect sources) and exhausts budget.
        # Action 2 fails with BUDGET_EXHAUSTED.
        plan_1 = self._create_sample_plan(plan_id="plan-budget-1", op="inventory.collect")
        plan_1["actions"].append({
            "id": "act-2",
            "op": "session.inspect",
            "params": {"session": "sessions/s1"},
        })
        plan_1["limits"]["max_http_requests"] = 1
        plan_1["plan_hash"] = compute_plan_hash(plan_1)

        body_multi = b'{"sources": [], "sessions": [], "name": "sessions/s1", "title": "T", "state": "COMPLETED"}'
        fake_opener_1 = FakeOpener(response_body=body_multi)
        spy_creds_1 = SpyCredentialSource()
        transport_1 = HttpTransport(credential_source=spy_creds_1, opener=fake_opener_1)

        res_1, store_1 = _compose_runtime(
            args=args,
            plan=plan_1,
            credential_source=spy_creds_1,
            transport=transport_1,
        )
        # Assert fake_opener.open_calls == 1, second action fails with BUDGET_EXHAUSTED
        self.assertEqual(fake_opener_1.open_calls, 1)
        self.assertEqual(res_1["action_results"][1]["error_code"], ErrorCode.BUDGET_EXHAUSTED.value)

        if store_0 is not None:
            store_0.close()
        if store_1 is not None:
            store_1.close()


if __name__ == "__main__":
    unittest.main()

