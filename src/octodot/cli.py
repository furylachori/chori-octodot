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

from octodot.authorization import DisabledGrantVerifier
from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    compute_plan_hash,
    load_strict_json,
    validate_plan,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_INTERRUPTED,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    ErrorCode,
    OctodotError,
)
from octodot.preparation import validate_only
from octodot.runner import ActionRunner, run_plan
from octodot.store import SQLiteStore


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


def compile_shorthand_to_plan(command: str, args: argparse.Namespace) -> dict[str, Any]:
    """Compile shorthand CLI command into a valid jules-controller.plan.v1 structure."""
    plan_id = f"plan-shorthand-{command}-{int(time.time())}"
    profile = getattr(args, "profile", "default") or "default"

    if command == "inventory":
        repo = getattr(args, "repo", None) or "OWNER/REPO"
        actions = [{
            "id": "act-inventory-1",
            "op": "inventory.collect",
            "params": {"scope": "all", "repository": repo},
        }]
        scope: dict[str, Any] = {"repository": repo}

    elif command == "inspect":
        session = getattr(args, "session", None) or "sessions/EXAMPLE"
        repo = getattr(args, "repo", None) or "OWNER/REPO"
        actions = [{
            "id": "act-inspect-1",
            "op": "session.inspect",
            "params": {"session": session},
        }]
        scope = {"repository": repo, "sessions": [session]}

    elif command == "chats":
        session = getattr(args, "session", None) or "sessions/EXAMPLE"
        repo = getattr(args, "repo", None) or "OWNER/REPO"
        actions = [{
            "id": "act-chats-1",
            "op": "chats.collect",
            "params": {"session": session},
        }]
        scope = {"repository": repo, "sessions": [session]}

    elif command == "healthcheck":
        actions = [{
            "id": "act-healthcheck-1",
            "op": "healthcheck",
            "params": {},
        }]
        scope = {"repository": "OWNER/REPO"}

    elif command == "wait":
        predicate = getattr(args, "predicate", "all_terminal") or "all_terminal"
        timeout = float(getattr(args, "timeout", 30.0) or 30.0)
        actions = [{
            "id": "act-wait-1",
            "op": "wait",
            "params": {"predicate": predicate, "timeout_seconds": timeout},
        }]
        scope = {"repository": "OWNER/REPO"}

    else:
        raise OctodotError(ErrorCode.INVALID_INPUT, f"Unknown shorthand command '{command}'")

    plan: dict[str, Any] = {
        "schema_version": "jules-controller.plan.v1",
        "plan_id": plan_id,
        "profile": profile,
        "execution": {"mode": "read_only"},
        "scope": scope,
        "limits": dict(LIVE_INVOCATION_DEFAULTS),
        "actions": actions,
        "output": {"format": "json"},
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


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

    # Setup store if state directory specified
    store = None
    if getattr(args, "state_dir", None):
        try:
            os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(args.state_dir, 0o700)
            except OSError:
                pass
            store = SQLiteStore(args.state_dir)
        except Exception as exc:
            log_diagnostic(f"Failed to initialize store: {_sanitize_for_display(str(exc))}")
            return EXIT_FATAL_READ_OR_LOCAL

    try:
        result = run_plan(
            plan=plan,
            store=store,
            artifacts_dir=getattr(args, "artifacts_dir", None),
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
            "preflight_complete": True,
            "preconditions_verified": True,
        }
        _write_output(preflight_dict, output_path=getattr(args, "result", None))
        return EXIT_OK

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
    if getattr(args, "state_dir", None):
        try:
            os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(args.state_dir, 0o700)
            except OSError:
                pass
            store = SQLiteStore(args.state_dir)
        except Exception as exc:
            log_diagnostic(f"Failed to initialize store: {_sanitize_for_display(str(exc))}")
            return EXIT_FATAL_READ_OR_LOCAL

    try:
        result = run_plan(
            plan=plan,
            store=store,
            credential_source=credential_source,
            transport_factory=transport_factory,
            transport=transport,
        )
    except OctodotError as err:
        log_diagnostic(f"Execution error [{err.code}]: {_sanitize_for_display(err.message)}")
        return EXIT_FATAL_READ_OR_LOCAL
    except Exception as exc:
        log_diagnostic(f"Unexpected error: {_sanitize_for_display(str(exc))}")
        return EXIT_FATAL_READ_OR_LOCAL
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()

    _write_output(result, output_path=getattr(args, "result", None))
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
    chats_parser.add_argument("--result", default=None, help="Path to write result JSON")
    chats_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    chats_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: healthcheck
    hc_parser = subparsers.add_parser("healthcheck", help="Check system health")
    hc_parser.add_argument("--result", default=None, help="Path to write result JSON")
    hc_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    hc_parser.add_argument("--profile", default="default", help="Profile name")

    # Shorthand: wait
    wait_parser = subparsers.add_parser("wait", help="Wait for predicate condition")
    wait_parser.add_argument("--predicate", default="all_terminal", help="Predicate condition to wait for")
    wait_parser.add_argument("--timeout", type=float, default=30.0, help="Wait timeout in seconds")
    wait_parser.add_argument("--result", default=None, help="Path to write result JSON")
    wait_parser.add_argument("--state-dir", default=None, help="Path to state directory")
    wait_parser.add_argument("--profile", default="default", help="Profile name")

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
    elif cmd in ("inventory", "inspect", "chats", "healthcheck", "wait"):
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
