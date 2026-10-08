"""Command-line interface for octodot (jules-controller).

Standard library only. Compatible with Python 3.10+.
Subcommands:
- run --plan PLAN [--result RESULT]
- prepare --validate-only --plan PLAN [--result RESULT]
- prepare --online-preflight --plan PLAN [--result RESULT]
- Shorthand commands: inventory, inspect, chats, healthcheck, wait

Output: machine-readable JSON/JSONL to stdout or result file.
Diagnostics: sanitized messages to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Sequence

from octodot.api import JulesClient
from octodot.authorization import DisabledGrantVerifier
from octodot.compat import (
    SUPPORTED_SHORTHANDS,
    compile_shorthand_plan,
    compile_shorthand_to_plan,
)
from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    check_execution_eligibility,
    compute_plan_hash,
    load_strict_json,
    validate_plan,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_INTERRUPTED,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
    OctodotError,
)
from octodot.journal import Journal
from octodot.preparation import validate_only
from octodot.reads import ReadService
from octodot.reconciliation import Reconciler
from octodot.registry import build_handler_registry
from octodot.runner import ActionRunner, run_plan
from octodot.store import FileRecoveryFence, SQLiteStore
from octodot.transport import (
    BudgetTracker,
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_MAX_HTTP_REQUESTS,
    DEFAULT_MAX_TOTAL_BYTES,
    HttpTransport,
    SystemClock,
)


class EnvCredentialSource:
    """Environment variable credential source implementing CredentialSource."""

    def __init__(self, env_var: str = "JULES_API_KEY") -> None:
        self.env_var = env_var

    def get_credential(self, profile: str) -> str | None:
        """Evaluated lazily upon get_credential(profile)."""
        return os.environ.get(self.env_var)


def log_diagnostic(msg: str) -> None:
    """Print sanitized diagnostic message to stderr."""
    sys.stderr.write(f"jules-controller: {msg}\n")
    sys.stderr.flush()


def _sanitize_for_display(val: str) -> str:
    """Mask potential secret patterns from diagnostic output."""
    if not val:
        return ""
    # Mask obvious long tokens/keys if present
    masked = val
    for prefix in ("gho_", "ghp_", "AIza", "sk-"):
        if prefix in masked:
            import re
            masked = re.sub(rf"{prefix}[A-Za-z0-9_-]{{10,}}", f"{prefix}***", masked)
    return masked


def _write_output(
    data: dict[str, Any],
    output_path: str | None = None,
    output_format: str = "json",
) -> None:
    """Emit formatted JSON or JSONL to destination file or stdout."""
    if output_format == "jsonl":
        lines = []
        action_results = data.get("action_results", [])
        for ar in action_results:
            lines.append(json.dumps(ar, separators=(",", ":")))
        summary_record = {k: v for k, v in data.items() if k != "action_results"}
        lines.append(json.dumps(summary_record, separators=(",", ":")))
        text = "\n".join(lines) + "\n"
    else:
        text = json.dumps(data, indent=2) + "\n"

    if output_path:
        tmp_path = f"{output_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, output_path)
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _compose_runtime(
    args: argparse.Namespace,
    plan: dict[str, Any],
    credential_source: Any = None,
    transport_factory: Any = None,
    transport: Any = None,
) -> tuple[dict[str, Any], Any]:
    """Unified runtime composition helper for execution plans.

    Validation occurs first: no credentials or transport are constructed before plan validation.
    """
    # 1. Whole-plan validation before constructing/accessing any credentials or network
    validate_plan(plan)
    check_execution_eligibility(plan)

    state_dir = getattr(args, "state_dir", None)
    store = None
    fence = None
    if state_dir:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(state_dir, 0o700)
        except OSError:
            pass
        fence_file = os.path.join(state_dir, "recovery.fence")
        if os.path.exists(fence_file) or os.path.isdir(os.path.join(state_dir, "fence")):
            fence = FileRecoveryFence(fence_file if os.path.exists(fence_file) else os.path.join(state_dir, "fence"))
        else:
            fence = FileRecoveryFence(fence_file)
        store = SQLiteStore(state_dir, fence=fence)

    clock = SystemClock()

    limits = plan.get("limits") or LIVE_INVOCATION_DEFAULTS
    budget_tracker = BudgetTracker(
        max_requests=int(limits.get("max_http_requests", DEFAULT_MAX_HTTP_REQUESTS)),
        max_total_bytes=int(limits.get("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES)),
        deadline_seconds=float(limits.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)),
        clock=clock,
    )

    if credential_source is None:
        env_var = getattr(args, "credential_env", None) or "JULES_API_KEY"
        credential_source = EnvCredentialSource(env_var=env_var)

    if transport is None:
        if transport_factory is not None:
            transport = transport_factory()
        else:
            transport = HttpTransport(
                credential_source=credential_source,
                clock=clock,
                profile=plan.get("profile", "default"),
                budget_tracker=budget_tracker,
            )
    elif hasattr(transport, "budget_tracker"):
        transport.budget_tracker = budget_tracker

    verifier = DisabledGrantVerifier()

    journal = None
    if store is not None:
        journal = Journal(store=store, verifier=verifier, fence=fence, clock=clock)

    client = JulesClient(transport=transport, ticket_authority=journal, clock=clock)

    read_service = ReadService(
        api=client,
        store=store,
        clock=clock,
        profile=plan.get("profile", "default"),
    )

    reconciler = None
    if store is not None:
        reconciler = Reconciler(store=store, read_api=client, clock=clock, fence=fence)

    execution = plan.get("execution", {})
    mode = execution.get("mode", "read_only")
    handlers = build_handler_registry(
        mode=mode,
        store=store,
        read_service=read_service,
        clock=clock,
        journal=journal,
        verifier=verifier,
        transport=transport,
        fence=fence,
        api=client,
        reconciler=reconciler,
    )

    result = run_plan(
        plan=plan,
        handlers=handlers,
        store=store,
        read_service=read_service,
        journal=journal,
        verifier=verifier,
        clock=clock,
        transport=transport,
        artifacts_dir=getattr(args, "artifacts_dir", None),
    )
    return result, store


def _handle_run(
    args: argparse.Namespace,
    credential_source: Any = None,
    transport_factory: Any = None,
    transport: Any = None,
) -> int:
    """Execute 'run' subcommand."""
    plan_path = args.plan
    if not plan_path or not os.path.isfile(plan_path):
        log_diagnostic(f"Plan file not found: {plan_path}")
        return EXIT_FATAL_READ_OR_LOCAL

    with open(plan_path, "rb") as f:
        plan_bytes = f.read()

    try:
        plan = load_strict_json(plan_bytes)
    except Exception as exc:
        log_diagnostic(f"Failed to parse plan JSON: {_sanitize_for_display(str(exc))}")
        return EXIT_FATAL_READ_OR_LOCAL

    store = None
    try:
        result, store = _compose_runtime(
            args=args,
            plan=plan,
            credential_source=credential_source,
            transport_factory=transport_factory,
            transport=transport,
        )
    except OctodotError as err:
        log_diagnostic(f"Execution error [{err.code}]: {_sanitize_for_display(err.message)}")
        if err.code == ErrorCode.OPERATION_CONFLICT:
            return EXIT_MUTATION_BLOCKED
        if err.code == ErrorCode.CANCELLED:
            return EXIT_INTERRUPTED
        return EXIT_FATAL_READ_OR_LOCAL
    except Exception as exc:
        log_diagnostic(f"Unexpected error: {_sanitize_for_display(str(exc))}")
        return EXIT_FATAL_READ_OR_LOCAL
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()

    out_format = getattr(args, "format", None) or plan.get("output", {}).get("format", "json")
    out_dest = getattr(args, "result", None) or plan.get("output", {}).get("destination")
    _write_output(result, output_path=out_dest, output_format=out_format)

    return int(result.get("exit_code", EXIT_OK))


def _handle_prepare(args: argparse.Namespace) -> int:
    """Execute 'prepare' subcommand (--validate-only or --online-preflight)."""
    plan_path = args.plan
    if not plan_path or not os.path.isfile(plan_path):
        log_diagnostic(f"Plan file not found: {plan_path}")
        return EXIT_FATAL_READ_OR_LOCAL

    with open(plan_path, "rb") as f:
        plan_bytes = f.read()

    try:
        plan = load_strict_json(plan_bytes)
    except Exception as exc:
        log_diagnostic(f"Failed to parse plan JSON: {_sanitize_for_display(str(exc))}")
        return EXIT_FATAL_READ_OR_LOCAL

    if getattr(args, "validate_only", False):
        try:
            report = validate_only(
                plan=plan,
                current_profile_epoch=0,
                grant_verifier=DisabledGrantVerifier(),
            )
        except OctodotError as err:
            log_diagnostic(f"Validation error [{err.code}]: {_sanitize_for_display(err.message)}")
            return EXIT_FATAL_READ_OR_LOCAL
        except Exception as exc:
            log_diagnostic(f"Validation failure: {_sanitize_for_display(str(exc))}")
            return EXIT_FATAL_READ_OR_LOCAL

        report_dict: dict[str, Any] = {
            "schema_version": "jules-controller.report.v1",
            "mode": "validate_only",
            "plan_id": report.plan_id,
            "plan_hash": report.plan_hash,
            "eligible": report.eligible,
            "is_valid": report.is_valid,
            "prepared_actions_count": len(report.prepared_actions),
            "verified_grants_count": len(report.verified_grants),
            "blockers": [b.code.value if hasattr(b.code, "value") else str(b.code) for b in report.blockers],
        }

        _write_output(report_dict, output_path=getattr(args, "result", None))
        return EXIT_OK if report.is_valid else (EXIT_MUTATION_BLOCKED if report.blockers else EXIT_FATAL_READ_OR_LOCAL)

    elif getattr(args, "online_preflight", False):
        # Online preflight validation
        try:
            validate_plan(plan)
        except OctodotError as err:
            log_diagnostic(f"Plan validation error [{err.code}]: {_sanitize_for_display(err.message)}")
            return EXIT_FATAL_READ_OR_LOCAL

        preflight_dict: dict[str, Any] = {
            "schema_version": "jules-controller.report.v1",
            "mode": "online_preflight",
            "plan_id": plan.get("plan_id"),
            "plan_hash": plan.get("plan_hash"),
            "preflight_complete": False,
            "preconditions_verified": False,
            "error_code": "unsupported_public_api",
            "reason": "Online preflight verification is deferred and not implemented in offline core (planned for Gate G3)",
        }
        _write_output(preflight_dict, output_path=getattr(args, "result", None))
        return EXIT_PARTIAL_OR_UNSUPPORTED

    else:
        log_diagnostic("prepare requires either --validate-only or --online-preflight")
        return EXIT_FATAL_READ_OR_LOCAL


def _handle_shorthand(
    command: str,
    args: argparse.Namespace,
    credential_source: Any = None,
    transport_factory: Any = None,
    transport: Any = None,
) -> int:
    """Compile shorthand into plan and run."""
    try:
        plan = compile_shorthand_to_plan(command, args)
    except Exception as exc:
        log_diagnostic(f"Failed to compile shorthand to plan: {_sanitize_for_display(str(exc))}")
        return EXIT_FATAL_READ_OR_LOCAL

    store = None
    try:
        result, store = _compose_runtime(
            args=args,
            plan=plan,
            credential_source=credential_source,
            transport_factory=transport_factory,
            transport=transport,
        )
    except OctodotError as err:
        log_diagnostic(f"Execution error [{err.code}]: {_sanitize_for_display(err.message)}")
        if err.code == ErrorCode.OPERATION_CONFLICT:
            return EXIT_MUTATION_BLOCKED
        if err.code == ErrorCode.CANCELLED:
            return EXIT_INTERRUPTED
        return EXIT_FATAL_READ_OR_LOCAL
    except Exception as exc:
        log_diagnostic(f"Unexpected error: {_sanitize_for_display(str(exc))}")
        return EXIT_FATAL_READ_OR_LOCAL
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()

    out_format = getattr(args, "format", None) or plan.get("output", {}).get("format", "json")
    _write_output(result, output_path=getattr(args, "result", None), output_format=out_format)
    return int(result.get("exit_code", EXIT_OK))


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for jules-controller."""
    parser = argparse.ArgumentParser(
        prog="jules-controller",
        description="Deterministic orchestrator and safe execution boundary for Google Jules",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # Subcommand: run
    run_parser = subparsers.add_parser("run", help="Run a validated execution plan")
    run_parser.add_argument("--plan", required=True, help="Path to plan JSON file")
    run_parser.add_argument("--result", default=None, help="Path to write result JSON document")
    run_parser.add_argument("--state-dir", default=None, help="Path to durable SQLite state directory")
    run_parser.add_argument("--artifacts-dir", default=None, help="Path to private artifacts directory")
    run_parser.add_argument("--format", choices=["json", "jsonl", "summary"], default="json", help="Output format")

    # Subcommand: prepare
    prepare_parser = subparsers.add_parser("prepare", help="Prepare and validate an execution plan")
    prepare_parser.add_argument("--validate-only", action="store_true", help="Offline validation without network")
    prepare_parser.add_argument("--online-preflight", action="store_true", help="Online preflight verification")
    prepare_parser.add_argument("--plan", required=True, help="Path to plan JSON file")
    prepare_parser.add_argument("--result", default=None, help="Path to write report JSON document")
    prepare_parser.add_argument("--state-dir", default=None, help="Path to durable SQLite state directory")
    prepare_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: inventory
    inv_parser = subparsers.add_parser("inventory", help="Collect sources and sessions")
    inv_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    inv_parser.add_argument("--scope", default="all", help="Collection scope")
    inv_parser.add_argument("--result", default=None, help="Path to write result JSON")
    inv_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    inv_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: inspect
    insp_parser = subparsers.add_parser("inspect", help="Inspect session")
    insp_parser.add_argument("--session", required=True, help="Session name (sessions/...)")
    insp_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    insp_parser.add_argument("--result", default=None, help="Path to write result JSON")
    insp_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    insp_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: chats
    chats_parser = subparsers.add_parser("chats", help="Collect conversation activities")
    chats_parser.add_argument("--session", required=True, help="Session name (sessions/...)")
    chats_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    chats_parser.add_argument("--result", default=None, help="Path to write result JSON")
    chats_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    chats_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: healthcheck
    hc_parser = subparsers.add_parser("healthcheck", help="Check system health")
    hc_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    hc_parser.add_argument("--result", default=None, help="Path to write result JSON")
    hc_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    hc_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: status
    status_parser = subparsers.add_parser("status", help="Check system status")
    status_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    status_parser.add_argument("--result", default=None, help="Path to write result JSON")
    status_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    status_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: wait
    wait_parser = subparsers.add_parser("wait", help="Wait for predicate condition")
    wait_parser.add_argument("--predicate", default="all_terminal", help="Predicate condition to wait for")
    wait_parser.add_argument("--timeout", type=float, default=30.0, help="Wait timeout in seconds")
    wait_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    wait_parser.add_argument("--result", default=None, help="Path to write result JSON")
    wait_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    wait_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: events
    events_parser = subparsers.add_parser("events", help="Read durable events")
    events_parser.add_argument("--session", default=None, help="Filter events by session name")
    events_parser.add_argument("--limit", type=int, default=100, help="Maximum events to return")
    events_parser.add_argument("--since", default=None, help="Read events since event ID")
    events_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    events_parser.add_argument("--result", default=None, help="Path to write result JSON")
    events_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    events_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: ack
    ack_parser = subparsers.add_parser("ack", help="Acknowledge durable events")
    ack_parser.add_argument("--event-id", default=None, help="Single event ID to acknowledge")
    ack_parser.add_argument("--event-ids", nargs="*", default=None, help="List of event IDs to acknowledge")
    ack_parser.add_argument("--up-to-seq", type=int, default=None, help="Acknowledge events up to journal sequence")
    ack_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    ack_parser.add_argument("--result", default=None, help="Path to write result JSON")
    ack_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    ack_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: reconcile
    rec_parser = subparsers.add_parser("reconcile", help="Reconcile uncertain operation state")
    rec_parser.add_argument("--operation-id", required=True, help="Operation ID to reconcile")
    rec_parser.add_argument("--scans", type=int, default=1, help="Number of observation scans")
    rec_parser.add_argument("--repo", default="OWNER/REPO", help="Repository in OWNER/REPO format")
    rec_parser.add_argument("--result", default=None, help="Path to write result JSON")
    rec_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    rec_parser.add_argument("--profile", default="default", help="Profile name")

    parser.add_argument("--credential-env", default="JULES_API_KEY", help="Environment variable name providing credentials")
    for p in (run_parser, inv_parser, insp_parser, chats_parser, hc_parser, status_parser, wait_parser, events_parser, ack_parser, rec_parser):
        p.add_argument("--credential-env", default=argparse.SUPPRESS, help="Environment variable name providing credentials")

    return parser


def main(
    argv: Sequence[str] | None = None,
    credential_source: Any = None,
    transport_factory: Any = None,
    transport: Any = None,
) -> int:
    """Main CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    if not argv:
        parser.print_help(sys.stderr)
        return EXIT_FATAL_READ_OR_LOCAL

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_FATAL_READ_OR_LOCAL

    cmd = args.command
    if cmd == "run":
        return _handle_run(
            args,
            credential_source=credential_source,
            transport_factory=transport_factory,
            transport=transport,
        )
    elif cmd == "prepare":
        return _handle_prepare(args)
    elif cmd in SUPPORTED_SHORTHANDS:
        return _handle_shorthand(
            cmd,
            args,
            credential_source=credential_source,
            transport_factory=transport_factory,
            transport=transport,
        )
    else:
        log_diagnostic(f"Unknown command '{cmd}'")
        return EXIT_FATAL_READ_OR_LOCAL



if __name__ == "__main__":
    sys.exit(main())
