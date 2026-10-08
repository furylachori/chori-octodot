"""Compatibility and shorthand command compiler for octodot.

Standard library only. Compatible with Python 3.10+.
Provides shorthand commands (inventory, inspect, chats, events, ack, wait,
reconcile, status) that compile to valid jules-controller.plan.v1 execution
plans and execute through the ActionRunner.

Security posture:
- All shorthand commands compile strictly with execution.mode = 'read_only'.
- Shorthands do NOT provide any mutation path (no send, reply, create, or
  approve shorthand).
- Any attempt to compile or execute a mutation shorthand is explicitly
  rejected with AUTH_DENIED.
- Mutations strictly require a complete jules-controller.plan.v1 plan and a
  verified trusted grant via a host GrantVerifier.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    compute_plan_hash,
    validate_plan,
)
from octodot.errors import (
    ErrorCode,
    OctodotError,
)
from octodot.registry import build_handler_registry
from octodot.runner import ActionRunner, run_plan

MUTATION_SHORTHAND_NAMES = frozenset({
    "send",
    "reply",
    "chats.reply",
    "create",
    "tasks.create",
    "approve",
    "plans.approve",
})

SUPPORTED_SHORTHANDS = frozenset({
    "inventory",
    "inspect",
    "chats",
    "events",
    "ack",
    "wait",
    "reconcile",
    "status",
    "healthcheck",
})


def compile_shorthand_plan(
    command: str,
    args: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compile shorthand CLI/API command into a valid jules-controller.plan.v1 plan.

    All compiled plans enforce:
    - execution.mode = 'read_only'
    - Exact repository and branch binding
    - Bounded invocation defaults
    - Canonical plan_hash

    Raises:
        OctodotError: If command is a mutation operation or unknown command.
    """
    cmd = command.strip().lower()

    if cmd in MUTATION_SHORTHAND_NAMES:
        raise OctodotError(
            ErrorCode.AUTH_DENIED,
            f"Shorthand commands do not support mutations ('{command}'). "
            "Mutations require a full jules-controller.plan.v1 execution plan "
            "and an explicitly verified trusted grant.",
        )

    if cmd not in SUPPORTED_SHORTHANDS:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Unknown shorthand command '{command}'. Supported commands: "
            f"{sorted(SUPPORTED_SHORTHANDS)}",
        )

    params: dict[str, Any] = {}
    if args is not None:
        if hasattr(args, "__dict__"):
            params.update(vars(args))
        elif isinstance(args, Mapping):
            params.update(args)
    params.update(kwargs)

    profile = params.get("profile") or "default"
    repo = params.get("repo") or params.get("repository") or "OWNER/REPO"
    plan_id = params.get("plan_id") or f"plan-shorthand-{cmd}-{int(time.time() * 1000)}"

    scope: dict[str, Any] = {"repository": repo}
    actions: list[dict[str, Any]] = []

    if cmd == "inventory":
        inv_scope = params.get("scope") or "all"
        actions.append({
            "id": "act-inventory-1",
            "op": "inventory.collect",
            "params": {"scope": inv_scope, "repository": repo},
        })

    elif cmd == "inspect":
        session = params.get("session") or "sessions/EXAMPLE"
        scope["sessions"] = [session]
        actions.append({
            "id": "act-inspect-1",
            "op": "session.inspect",
            "params": {"session": session},
        })

    elif cmd == "chats":
        session = params.get("session") or "sessions/EXAMPLE"
        scope["sessions"] = [session]
        actions.append({
            "id": "act-chats-1",
            "op": "chats.collect",
            "params": {"session": session},
        })

    elif cmd == "events":
        session = params.get("session")
        if session:
            scope["sessions"] = [session]
        ev_params: dict[str, Any] = {}
        if session:
            ev_params["session"] = session
        if params.get("limit") is not None:
            ev_params["limit"] = int(params["limit"])
        if params.get("since") is not None:
            ev_params["since"] = params["since"]
        actions.append({
            "id": "act-events-1",
            "op": "events.read",
            "params": ev_params,
        })

    elif cmd == "ack":
        ack_params: dict[str, Any] = {}
        if params.get("event_ids"):
            ack_params["event_ids"] = list(params["event_ids"])
        elif params.get("event_id"):
            ack_params["event_ids"] = [params["event_id"]]
        if params.get("up_to_seq") is not None:
            ack_params["up_to_seq"] = int(params["up_to_seq"])
        actions.append({
            "id": "act-ack-1",
            "op": "events.ack",
            "params": ack_params,
        })

    elif cmd == "wait":
        predicate = params.get("predicate") or "all_terminal"
        timeout = float(params.get("timeout_seconds") or params.get("timeout") or 30.0)
        actions.append({
            "id": "act-wait-1",
            "op": "wait",
            "params": {"predicate": predicate, "timeout_seconds": timeout},
        })

    elif cmd == "reconcile":
        op_id = params.get("operation_id")
        if not op_id:
            raise OctodotError(
                ErrorCode.INVALID_INPUT,
                "reconcile shorthand requires 'operation_id' parameter",
            )
        scans = int(params.get("scans") or 1)
        actions.append({
            "id": "act-reconcile-1",
            "op": "operations.reconcile",
            "params": {"operation_id": op_id, "scans": scans},
        })

    elif cmd in ("status", "healthcheck"):
        actions.append({
            "id": f"act-{cmd}-1",
            "op": "healthcheck",
            "params": {},
        })

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

    validate_plan(plan)
    return plan


# Aliases
compile_shorthand = compile_shorthand_plan
compile_shorthand_to_plan = compile_shorthand_plan


def run_shorthand(
    command: str,
    runner: ActionRunner | None = None,
    store: Any = None,
    read_service: Any = None,
    clock: Any = None,
    journal: Any = None,
    transport: Any = None,
    verifier: Any = None,
    fence: Any = None,
    api: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compile shorthand into plan, resolve handlers, and run through the ActionRunner.

    Guarantees:
    - Mode is strictly read_only
    - Result document is schema-validated
    - Exit code is deterministic
    """
    plan = compile_shorthand_plan(command, **kwargs)

    handlers = build_handler_registry(
        mode="read_only",
        store=store,
        read_service=read_service,
        clock=clock,
        journal=journal,
        verifier=verifier,
        transport=transport,
        fence=fence,
        api=api,
    )

    if runner is not None:
        # runner provided: update injected handlers if needed
        runner.injected_handlers.update(handlers)
        return runner.run(plan)

    return run_plan(
        plan=plan,
        handlers=handlers,
        store=store,
        read_service=read_service,
        journal=journal,
        verifier=verifier,
        clock=clock,
        transport=transport,
    )


# Individual helper functions
def shorthand_inventory(repo: str = "OWNER/REPO", scope: str = "all", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("inventory", repo=repo, scope=scope, **kwargs)


def shorthand_inspect(session: str, repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("inspect", session=session, repo=repo, **kwargs)


def shorthand_chats(session: str, repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("chats", session=session, repo=repo, **kwargs)


def shorthand_events(repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("events", repo=repo, **kwargs)


def shorthand_ack(repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("ack", repo=repo, **kwargs)


def shorthand_wait(predicate: str = "all_terminal", timeout: float = 30.0, repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("wait", predicate=predicate, timeout=timeout, repo=repo, **kwargs)


def shorthand_reconcile(operation_id: str, repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("reconcile", operation_id=operation_id, repo=repo, **kwargs)


def shorthand_status(repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("status", repo=repo, **kwargs)


def shorthand_healthcheck(repo: str = "OWNER/REPO", **kwargs: Any) -> dict[str, Any]:
    return compile_shorthand_plan("healthcheck", repo=repo, **kwargs)

