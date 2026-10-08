"""Production static handler registry for the octodot action runner.

Standard library only. Compatible with Python 3.10+.
Maps each operation in OPERATION_INVENTORY to its concrete handler:
- Read ops (S06): inventory.collect, session.inspect, chats.collect,
  capabilities.inspect, healthcheck, suggestions.collect
- Events and jobs ops (S07): events.read, events.ack, wait
- Reconciliation ops (S08): operations.reconcile
- Mutation ops (S10, S11, S12): chats.reply, tasks.create, plans.approve
  (built only in mutation execution mode)
- Explicit unsupported ops: artifacts.export_patch, publication.verify
  (always return exit 5, never silently succeeding)

Composition enforces:
- Verifier default is DisabledGrantVerifier (live writes stay disabled)
- require_verifier_allowed(verifier, live=<is real transport>) always checked
- No code path constructs FakeGrantVerifier with a real transport
"""

from __future__ import annotations

from typing import Any, Mapping

from octodot.actions.approve import PlansApproveHandler
from octodot.actions.create import TasksCreateHandler
from octodot.actions.read import (
    CapabilitiesInspectHandler,
    ChatsCollectHandler,
    HealthcheckHandler,
    InventoryCollectHandler,
    SessionInspectHandler,
    SuggestionsCollectHandler,
)
from octodot.actions.reply import ChatsReplyHandler
from octodot.authorization import DisabledGrantVerifier, require_verifier_allowed
from octodot.contracts import (
    ActionHandler,
    Coverage,
    GrantVerifier,
    OPERATION_INVENTORY,
    OperationClassification,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    ErrorCode,
    OctodotError,
)
from octodot.events import EventsAckHandler, EventsReadHandler
from octodot.jobs import WaitActionHandler
from octodot.models import (
    ActionResult,
    ActionResultStatus,
)
from octodot.reconciliation import Reconciler
from octodot.transport import FixtureTransport


class UnsupportedOperationHandler:
    """Explicit unsupported handler for unimplemented optional ops.

    Always returns ActionResultStatus.UNSUPPORTED with exit code 5
    (EXIT_PARTIAL_OR_UNSUPPORTED) and error code UNSUPPORTED_PUBLIC_API.
    Never succeeds silently.
    """

    def __init__(self, op: str, message: str | None = None) -> None:
        self.op = op
        self.message = message or f"Operation '{op}' is not supported in this offline release"

    def can_handle(self, op: str) -> bool:
        return op == self.op

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        action_id = str(action.get("id", f"act-{self.op}"))
        return ActionResult.create(
            action_id=action_id,
            op=self.op,
            status=ActionResultStatus.UNSUPPORTED,
            exit_code=EXIT_PARTIAL_OR_UNSUPPORTED,
            error_code=ErrorCode.UNSUPPORTED_PUBLIC_API,
            coverage=Coverage(
                complete=False,
                snapshot_atomic=False,
                pages=0,
                items=0,
                skipped_scope=(self.op,),
                reasons=("unsupported_operation",),
            ),
            data={
                "unsupported": True,
                "error": self.message,
            },
        )


class _ReconciliationReadApiAdapter:
    """Adapts a JulesReadAPI or JulesClient to return un-nested activity sequences for Reconciler."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def activities_list(self, *args: Any, **kwargs: Any) -> Any:
        res = self._inner.activities_list(*args, **kwargs)
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], (tuple, list)):
            return res[0]
        return res


class OperationsReconcileHandler:
    """ActionHandler for 'operations.reconcile' (S08 read-only reconciliation)."""

    def __init__(
        self,
        store: Any = None,
        read_service: Any = None,
        clock: Any = None,
        fence: Any = None,
        reconciler: Reconciler | Any | None = None,
    ) -> None:
        self.store = store
        self.read_service = read_service
        self.clock = clock
        self.fence = fence
        self.reconciler = reconciler

    def can_handle(self, op: str) -> bool:
        return op == "operations.reconcile"

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        ctx = context or {}
        action_id = str(action.get("id", "act-operations-reconcile"))
        op = "operations.reconcile"
        params = action.get("params") or {}
        operation_id = params.get("operation_id")

        if not operation_id or not isinstance(operation_id, str):
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INVALID_INPUT,
                data={"error": "Missing required parameter 'operation_id'"},
            )

        store = self.store or ctx.get("store")
        if store is None:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "Store unavailable for reconciliation"},
            )

        clock = self.clock or ctx.get("clock")
        fence = self.fence or ctx.get("fence")
        read_service = self.read_service or ctx.get("read_service")
        read_api = ctx.get("api")
        if read_api is None and read_service is not None and hasattr(read_service, "api"):
            read_api = read_service.api
        if read_api is not None and not isinstance(read_api, _ReconciliationReadApiAdapter):
            read_api = _ReconciliationReadApiAdapter(read_api)

        reconciler = self.reconciler or ctx.get("reconciler")
        if reconciler is None:
            reconciler = Reconciler(
                store=store,
                read_api=read_api,
                clock=clock,
                fence=fence,
            )

        scans = int(params.get("scans", 1))
        try:
            rec_result = reconciler.reconcile(operation_id, read_api=read_api, scans=scans)
        except OctodotError as err:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=err.code,
                data={"error": err.message},
            )
        except Exception as exc:
            return ActionResult.create(
                action_id=action_id,
                op=op,
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": str(exc)},
            )

        rec_record_dict = None
        if rec_result.record is not None:
            rec_record_dict = {
                "operation_id": rec_result.record.operation_id,
                "state": rec_result.record.state.value if hasattr(rec_result.record.state, "value") else str(rec_result.record.state),
                "api_accepted": rec_result.record.api_accepted,
                "effect_observed": rec_result.record.effect_observed,
                "attribution": rec_result.record.attribution,
                "ui_verified": rec_result.record.ui_verified,
            }

        return ActionResult.create(
            action_id=action_id,
            op=op,
            status=ActionResultStatus.OK,
            exit_code=EXIT_OK,
            data={
                "reconciled_state": rec_result.reconciled_state,
                "operation_record": rec_record_dict,
            },
        )

class EventsReadHandlerAdapter:
    """Adapts EventsReadHandler to standard ActionHandler interface with params mapping."""

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self.inner = EventsReadHandler(store) if store is not None else None

    def can_handle(self, op: str) -> bool:
        return op == "events.read"

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        act_id = str(action.get("id") or action.get("action_id", "act-events-1"))
        store = self.store or (context or {}).get("store")
        if store is None:
            return ActionResult.create(
                action_id=act_id,
                op="events.read",
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "Store unavailable for events.read"},
            )
        inner = self.inner or EventsReadHandler(store)
        act = dict(action)
        if "action_id" not in act and "id" in act:
            act["action_id"] = act["id"]
        if "args" not in act and "params" in act:
            act["args"] = act["params"]
        res = inner.handle(act, context or {})
        if "id" in action and res.action_id != action["id"]:
            return ActionResult(
                action_id=str(action["id"]),
                op=res.op,
                status=res.status,
                exit_code=res.exit_code,
                error_code=res.error_code,
                coverage=res.coverage,
                data=res.data,
            )
        return res


class EventsAckHandlerAdapter:
    """Adapts EventsAckHandler to standard ActionHandler interface with params mapping."""

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self.inner = EventsAckHandler(store) if store is not None else None

    def can_handle(self, op: str) -> bool:
        return op == "events.ack"

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        act_id = str(action.get("id") or action.get("action_id", "act-ack-1"))
        store = self.store or (context or {}).get("store")
        if store is None:
            return ActionResult.create(
                action_id=act_id,
                op="events.ack",
                status=ActionResultStatus.ERROR,
                exit_code=EXIT_FATAL_READ_OR_LOCAL,
                error_code=ErrorCode.INTERNAL_ERROR,
                data={"error": "Store unavailable for events.ack"},
            )
        inner = self.inner or EventsAckHandler(store)
        act = dict(action)
        if "action_id" not in act and "id" in act:
            act["action_id"] = act["id"]
        if "args" not in act and "params" in act:
            act["args"] = act["params"]
        res = inner.handle(act, context or {})
        if "id" in action and res.action_id != action["id"]:
            return ActionResult(
                action_id=str(action["id"]),
                op=res.op,
                status=res.status,
                exit_code=res.exit_code,
                error_code=res.error_code,
                coverage=res.coverage,
                data=res.data,
            )
        return res


class ChatsReplyHandlerAdapter:
    """Wraps ChatsReplyHandler to supply profile_epoch if missing from context."""

    def __init__(
        self,
        handler: ChatsReplyHandler | None = None,
        fence: Any = None,
        store: Any = None,
    ) -> None:
        self.handler = handler or ChatsReplyHandler()
        self.fence = fence
        self.store = store

    def can_handle(self, op: str) -> bool:
        return self.handler.can_handle(op)

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        ctx = dict(context or {})
        if "profile_epoch" not in ctx:
            profile = str(ctx.get("profile") or (ctx.get("plan") or {}).get("profile") or "default")
            epoch = 0
            if self.fence is not None and hasattr(self.fence, "get_current_epoch"):
                try:
                    epoch = self.fence.get_current_epoch(profile)
                except Exception:
                    pass
            elif self.store is not None and hasattr(self.store, "get_profile_epoch"):
                try:
                    epoch = self.store.get_profile_epoch(profile)
                except Exception:
                    pass
            ctx["profile_epoch"] = epoch
        return self.handler.execute(action, ctx)


class PlansApproveHandlerAdapter:
    """Wraps PlansApproveHandler to supply grant/grants from verifier if missing from context."""

    def __init__(
        self,
        handler: PlansApproveHandler,
        verifier: GrantVerifier | None = None,
    ) -> None:
        self.handler = handler
        self.verifier = verifier

    def can_handle(self, op: str) -> bool:
        return self.handler.can_handle(op)

    def execute(
        self,
        action: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ActionResult:
        ctx = dict(context or {})
        if "grant" not in ctx and "grants" not in ctx:
            v = ctx.get("verifier") or self.verifier
            if hasattr(v, "_grants"):
                ref = action.get("authorization_ref")
                if ref in v._grants:
                    ctx["grant"] = v._grants[ref]
                ctx["grants"] = v._grants
            elif hasattr(v, "grants"):
                ref = action.get("authorization_ref")
                if ref in v.grants:
                    ctx["grant"] = v.grants[ref]
                ctx["grants"] = v.grants
        return self.handler.execute(action, ctx)


def build_handler_registry(
    mode: str = "read_only",
    store: Any = None,
    read_service: Any = None,
    clock: Any = None,
    journal: Any = None,
    verifier: GrantVerifier | None = None,
    transport: Any = None,
    fence: Any = None,
    api: Any = None,
    reconciler: Any = None,
) -> dict[str, ActionHandler]:
    """Assemble static handler registry with execution mode and authorization gating.

    Args:
        mode: "read_only" or "mutation"
        store: Durable SQLiteStore or in-memory store
        read_service: ReadService instance for read actions
        clock: Clock instance (SystemClock or FakeClock)
        journal: MutationJournal instance
        verifier: GrantVerifier (default: DisabledGrantVerifier, fails closed)
        transport: Transport instance (used to detect real vs fixture transport)
        fence: RecoveryFence instance
        api: JulesReadAPI or JulesClient instance
        reconciler: Reconciler instance

    Returns:
        Mapping of op string to ActionHandler.
    """
    if verifier is None:
        verifier = DisabledGrantVerifier()

    # Determine whether transport is live (anything other than FixtureTransport)
    is_live = transport is not None and not isinstance(transport, FixtureTransport)

    # Validate verifier against live boundary
    require_verifier_allowed(verifier, live=is_live)

    handlers: dict[str, ActionHandler] = {}

    # 1. Read operations (S06)
    handlers["inventory.collect"] = InventoryCollectHandler()
    handlers["session.inspect"] = SessionInspectHandler()
    handlers["chats.collect"] = ChatsCollectHandler()
    handlers["capabilities.inspect"] = CapabilitiesInspectHandler()
    handlers["healthcheck"] = HealthcheckHandler()
    handlers["suggestions.collect"] = SuggestionsCollectHandler()

    # 2. Events and Wait operations (S07)
    handlers["events.read"] = EventsReadHandlerAdapter(store)
    handlers["events.ack"] = EventsAckHandlerAdapter(store)

    handlers["wait"] = WaitActionHandler(
        store=store,
        read_service=read_service,
        clock=clock,
    )

    # 3. Reconciliation operation (S08)
    handlers["operations.reconcile"] = OperationsReconcileHandler(
        store=store,
        read_service=read_service,
        clock=clock,
        fence=fence,
        reconciler=reconciler,
    )

    # 4. Optional unimplemented operations (explicit unsupported exit 5)
    handlers["artifacts.export_patch"] = UnsupportedOperationHandler("artifacts.export_patch")
    handlers["publication.verify"] = UnsupportedOperationHandler("publication.verify")

    # 5. Mutation operations (S10, S11, S12)
    # Built ONLY in mutation execution mode
    if mode == "mutation":
        handlers["chats.reply"] = ChatsReplyHandlerAdapter(
            fence=fence,
            store=store,
        )
        handlers["tasks.create"] = TasksCreateHandler(
            api=api,
            read_service=read_service,
            journal=journal,
            verifier=verifier,
            fence=fence,
            clock=clock,
            reconciler=reconciler,
            store=store,
        )
        handlers["plans.approve"] = PlansApproveHandlerAdapter(
            handler=PlansApproveHandler(
                api=api,
                store=store,
                verifier=verifier,
                journal=journal,
                read_service=read_service,
                clock=clock,
                fence=fence,
            ),
            verifier=verifier,
        )

    return handlers


# Convenience alias
get_handler_registry = build_handler_registry
