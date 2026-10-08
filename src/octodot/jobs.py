"""Resumable bounded observation jobs and wait predicates for octodot.

Standard library only. Compatible with Python 3.10+.
Provides:
- Resumable wait predicates: 'attention', 'all_terminal', 'new_events', 'operation_observed'.
- Fixed selection with per-invocation budgets and injected Clock.
- Resumable jobs yielding status 'waiting' with durable job ID in S03 store,
  resuming to the requested condition.
- Workflow lock release between wait iterations and state reload upon reacquisition.
- Read backoff handling 429/Retry-After, transient GET errors, and cancellation
  with finite fake-time traces.
- Max-iteration bounds on every loop.
- Discovery cadence finding new sessions when allowed (fixed selection never silently expands).
- Final terminal scan collecting late artifacts without claiming publication verified.
- WaitActionHandler implementing ActionHandler protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable

from octodot.contracts import (
    ActionHandler,
    Clock,
    Store,
    WaitArgs,
    WaitResult,
    canonical_hash,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_WAITING,
    ErrorCode,
    OctodotError,
    StateStoreError,
    TransportFailureError,
)
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ActivityRecord,
    Binding,
    Event,
    LifecycleBucket,
    OperationRecord,
    OperationState,
    SessionRecord,
)
from octodot.reads import ReadService, SessionInspection
from octodot.transport import SystemClock


class CancellationToken:
    """Cooperative cancellation token for wait loops and backoff delays."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Signal cancellation."""
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._cancelled


def _check_cancellation(
    token_or_callable: CancellationToken | Callable[[], bool] | None,
) -> bool:
    """Evaluate whether cancellation has been requested."""
    if token_or_callable is None:
        return False
    if isinstance(token_or_callable, CancellationToken):
        return token_or_callable.is_cancelled
    if callable(token_or_callable):
        return bool(token_or_callable())
    return False


def _to_binding(
    item: str | Binding | dict[str, Any],
    profile: str = "default",
    epoch: int = 0,
) -> Binding:
    """Normalize item to a Binding."""
    if isinstance(item, Binding):
        return item
    if isinstance(item, SessionRecord):
        from octodot.identity import (
            extract_session_branch,
            extract_session_repository,
            extract_session_source,
        )
        return Binding(
            profile=profile,
            profile_epoch=epoch,
            source=extract_session_source(item) or "",
            repository=extract_session_repository(item) or "",
            starting_branch=extract_session_branch(item),
            session=item.name,
        )
    if isinstance(item, dict):
        return Binding(
            profile=item.get("profile", profile),
            profile_epoch=item.get("profile_epoch", epoch),
            source=item.get("source", ""),
            repository=item.get("repository", ""),
            starting_branch=item.get("starting_branch") or item.get("branch"),
            session=item.get("session") or item.get("name", ""),
        )
    # String session ID / name
    s_str = str(item)
    return Binding(
        profile=profile,
        profile_epoch=epoch,
        source="",
        repository="",
        starting_branch=None,
        session=s_str,
    )


# =============================================================================
# Predicate Check Functions
# =============================================================================


def check_attention_predicate(
    selection: Sequence[Binding],
    read_service: ReadService,
) -> bool:
    """Check if any session in the selection requires human/user attention."""
    from octodot.projections import project_attention, project_lifecycle

    for binding in selection:
        try:
            insp = read_service.inspect(binding, fresh=True)
            lifecycle = (
                insp.lifecycle
                or project_lifecycle(
                    insp.session.state if isinstance(insp.session, SessionRecord) else insp.session
                )
            )
            attention = project_attention(
                lifecycle=lifecycle,
                candidate_bundle=insp.candidate_bundle,
                session=insp.session,
            )
            if attention.needs_attention:
                return True
        except OctodotError:
            raise
        except Exception as e:
            raise OctodotError(ErrorCode.TRANSPORT_ERROR, f"Inspect failed: {e}") from e
    return False


def check_all_terminal_predicate(
    selection: Sequence[Binding],
    read_service: ReadService,
    publication_watch: bool = False,
) -> tuple[bool, list[dict[str, Any]]]:
    """Check if all sessions in selection have reached terminal state (COMPLETED/FAILED).

    Returns:
        (matched, late_artifacts)
    Crucial rules:
    - If selection is empty, returns (False, []).
    - When all sessions are terminal, runs a final scan to pick up late artifacts.
    - 'all_terminal' does NOT imply publication verified.
    - If publication_watch is True, requires explicit publication evidence; otherwise stays waiting.
    """
    if not selection:
        return False, []

    from octodot.projections import project_lifecycle

    inspections: list[SessionInspection] = []
    for binding in selection:
        try:
            insp = read_service.inspect(binding, fresh=True)
            inspections.append(insp)
            lifecycle = (
                insp.lifecycle
                or project_lifecycle(
                    insp.session.state if isinstance(insp.session, SessionRecord) else insp.session
                )
            )
            if not lifecycle.is_terminal:
                return False, []
        except OctodotError:
            raise
        except Exception as e:
            raise OctodotError(ErrorCode.TRANSPORT_ERROR, f"Inspect failed: {e}") from e

    # All sessions are terminal: collect late artifacts
    late_artifacts: list[dict[str, Any]] = []
    has_publication_evidence = False

    for insp in inspections:
        for act in insp.activities:
            # Check for artifacts in activity unknown fields or payload
            for k, v in act.unknown_fields:
                if "artifact" in k.lower() or "patch" in k.lower() or "pr" in k.lower():
                    late_artifacts.append({"activity": act.name, "key": k, "value": v})
            if act.activity_type in ("pullRequestCreated", "pr_created", "publication"):
                has_publication_evidence = True
                late_artifacts.append({"activity": act.name, "type": act.activity_type})

    # A publication watch retains its requested predicate and cannot be satisfied
    # by lifecycle terminal alone
    if publication_watch and not has_publication_evidence:
        return False, late_artifacts

    return True, late_artifacts


def check_new_events_predicate(
    store: Store,
    baseline_event_ids: set[str],
) -> bool:
    """Check if any new unacknowledged events exist in the store beyond baseline."""
    unacked = store.get_events(limit=100, unacked_only=True)
    for ev in unacked:
        if ev.event_id not in baseline_event_ids:
            return True
    return False


def check_operation_observed_predicate(
    store: Store,
    operation_id: str | None,
) -> bool:
    """Read operation state through the S03 store's get_operation."""
    if not operation_id:
        return False
    op_record = store.get_operation(operation_id)
    if op_record is None:
        return False
    # Check affirmative observation
    if op_record.effect_observed:
        return True
    if op_record.state == OperationState.EFFECT_OBSERVED:
        return True
    return False


# =============================================================================
# Core Resumable Wait Execution
# =============================================================================


def execute_wait(
    predicate: str,
    timeout_seconds: float = 30.0,
    job_id: str | None = None,
    selection: Sequence[str | Binding] = (),
    profile: str = "default",
    plan_id: str = "wait_plan",
    store: Store | None = None,
    read_service: ReadService | None = None,
    clock: Clock | None = None,
    poll_interval: float = 1.0,
    max_iterations: int = 100,
    allow_discovery: bool = False,
    discovery_cadence: int = 0,
    discovery_scope: str = "all",
    operation_id: str | None = None,
    publication_watch: bool = False,
    cancellation_token: CancellationToken | Callable[[], bool] | None = None,
    on_iteration_hook: Callable[[int], None] | None = None,
    initial_backoff: float = 1.0,
    max_backoff: float = 30.0,
    lock_timeout: float = 5.0,
) -> WaitResult:
    """Execute a bounded resumable wait for predicates over a fixed selection.

    Predicates supported:
    - 'attention': any session in selection needs attention
    - 'all_terminal': all sessions in selection are completed/failed
    - 'new_events': new unacknowledged events in store
    - 'operation_observed': operation effect observed in store

    Features:
    - Fixed selection: never silently expands unless allow_discovery is True.
    - Yields status 'waiting' with durable job ID in S03 jobs upon timeout/budget.
    - Resume restores state from S03 jobs using the same job ID.
    - Releases store workflow lock between wait iterations, reloading state on reacquire.
    - Bounded backoff for 429/Retry-After and transient GET errors on fake clock.
    - Cooperative cancellation support.
    - Max iteration bounds on every loop.
    """
    clock = clock or SystemClock()
    start_time = clock.now_utc()
    resumed = False

    # 1. Job Setup / Resume from Store
    if store is not None and job_id is not None:
        existing_job = store.load_job(job_id)
        if existing_job is not None:
            resumed = True
            details = existing_job.get("details", {})
            # Restore saved configuration if not explicitly overridden
            predicate = details.get("predicate", predicate)
            allow_discovery = details.get("allow_discovery", allow_discovery)
            discovery_cadence = details.get("discovery_cadence", discovery_cadence)
            operation_id = details.get("operation_id", operation_id)
            publication_watch = details.get("publication_watch", publication_watch)
            saved_sel = details.get("selection", [])
            if saved_sel and not selection:
                selection = saved_sel

    if job_id is None:
        # Create deterministic or unique job ID
        job_id = f"job_{canonical_hash({'plan': plan_id, 'predicate': predicate, 'time': start_time.isoformat()})[:16]}"
        if store is not None:
            init_details = {
                "predicate": predicate,
                "allow_discovery": allow_discovery,
                "discovery_cadence": discovery_cadence,
                "operation_id": operation_id,
                "publication_watch": publication_watch,
                "selection": [
                    s.session if isinstance(s, Binding) else str(s) for s in selection
                ],
            }
            store.create_job(
                job_id=job_id,
                profile=profile,
                plan_id=plan_id,
                status="pending",
                details=init_details,
            )

    # 2. Normalize selection to Bindings
    current_bindings: list[Binding] = [
        _to_binding(s, profile=profile) for s in selection
    ]

    # Baseline for new_events predicate
    baseline_event_ids: set[str] = set()
    if predicate == "new_events" and store is not None:
        initial_events = store.get_events(limit=100, unacked_only=True)
        baseline_event_ids = {ev.event_id for ev in initial_events}

    iteration = 0
    current_backoff = initial_backoff
    late_artifacts: list[dict[str, Any]] = []

    # 3. Main Polling Loop with max_iterations bound
    while iteration < max_iterations:
        # Cancellation check
        if _check_cancellation(cancellation_token):
            if store is not None:
                store.update_job(job_id, status="cancelled")
                store.release_lock()
            raise OctodotError(ErrorCode.CANCELLED, "Wait cancelled by caller")

        # Step A: Lock acquisition and durable state reload
        if store is not None:
            if not store.acquire_lock(timeout=lock_timeout):
                # Lock held by another process: yield waiting without evaluating or mutating
                return WaitResult(
                    resumed=resumed,
                    predicate_matched=False,
                    job_id=job_id,
                )
            # Reacquire reloads durable state: refresh job record
            refreshed_job = store.load_job(job_id)
            if refreshed_job and refreshed_job.get("status") == "cancelled":
                store.release_lock()
                raise OctodotError(ErrorCode.CANCELLED, "Wait cancelled externally")

        # Step B: Discovery cadence (if enabled)
        if (
            allow_discovery
            and discovery_cadence > 0
            and iteration > 0
            and (iteration % discovery_cadence == 0)
            and read_service is not None
        ):
            try:
                scope_arg = (
                    {"scope": discovery_scope}
                    if isinstance(discovery_scope, str)
                    else discovery_scope
                )
                inv_coll, _ = read_service.collect(scope=scope_arg)
                existing_session_names = {b.session for b in current_bindings}
                for s in inv_coll.sessions:
                    s_name = s.name
                    if s_name not in existing_session_names and (s.id is None or s.id not in existing_session_names):
                        current_bindings.append(_to_binding(s, profile=profile))
                        existing_session_names.add(s_name)
                        if s.id:
                            existing_session_names.add(s.id)
            except Exception:
                # Discovery error does not abort wait loop
                pass

        # Step C: Evaluate predicate
        matched = False
        try:
            if predicate == "attention":
                if read_service is None:
                    raise OctodotError(
                        ErrorCode.INVALID_INPUT,
                        "ReadService required for 'attention' predicate",
                    )
                matched = check_attention_predicate(current_bindings, read_service)

            elif predicate == "all_terminal":
                if read_service is None:
                    raise OctodotError(
                        ErrorCode.INVALID_INPUT,
                        "ReadService required for 'all_terminal' predicate",
                    )
                matched, late_artifacts = check_all_terminal_predicate(
                    current_bindings,
                    read_service,
                    publication_watch=publication_watch,
                )

            elif predicate == "new_events":
                if store is None:
                    raise OctodotError(
                        ErrorCode.INVALID_INPUT,
                        "Store required for 'new_events' predicate",
                    )
                matched = check_new_events_predicate(store, baseline_event_ids)

            elif predicate == "operation_observed":
                if store is None:
                    raise OctodotError(
                        ErrorCode.INVALID_INPUT,
                        "Store required for 'operation_observed' predicate",
                    )
                matched = check_operation_observed_predicate(store, operation_id)

            else:
                raise OctodotError(
                    ErrorCode.INVALID_INPUT, f"Unknown wait predicate: {predicate}"
                )

            # Reset backoff on successful read
            current_backoff = initial_backoff

        except OctodotError as err:
            if err.code == ErrorCode.CANCELLED:
                if store is not None:
                    store.update_job(job_id, status="cancelled")
                    store.release_lock()
                raise

            # Step Backoff: Handle 429 / Rate Limited and Transient GET errors
            if err.code in (
                ErrorCode.RATE_LIMITED,
                ErrorCode.TRANSPORT_ERROR,
                ErrorCode.TIMEOUT,
            ):
                # Release store lock before sleeping!
                if store is not None:
                    store.release_lock()

                retry_after_val = getattr(err, "retry_after", None)
                if err.code == ErrorCode.RATE_LIMITED and retry_after_val is not None:
                    sleep_time = min(max(float(retry_after_val), initial_backoff), max_backoff)
                else:
                    sleep_time = min(current_backoff, max_backoff)
                    current_backoff = min(current_backoff * 2.0, max_backoff)

                clock.sleep(sleep_time)

                if _check_cancellation(cancellation_token):
                    if store is not None:
                        store.update_job(job_id, status="cancelled")
                    raise OctodotError(ErrorCode.CANCELLED, "Wait cancelled during backoff")

                iteration += 1
                continue
            else:
                if store is not None:
                    store.release_lock()
                raise

        # Check match
        if matched:
            if store is not None:
                store.update_job(
                    job_id=job_id,
                    status="completed",
                    details={
                        "predicate": predicate,
                        "iteration": iteration,
                        "late_artifacts": late_artifacts,
                    },
                )
                store.release_lock()
            return WaitResult(
                resumed=resumed,
                predicate_matched=True,
                job_id=job_id,
            )

        # Step D: Deadline / budget check
        elapsed = (clock.now_utc() - start_time).total_seconds()
        if elapsed >= timeout_seconds or (iteration + 1 >= max_iterations):
            # Budget yielded: persist status 'waiting' with the SAME durable job ID
            if store is not None:
                store.update_job(
                    job_id=job_id,
                    status="waiting",
                    details={
                        "predicate": predicate,
                        "iteration": iteration,
                        "allow_discovery": allow_discovery,
                        "discovery_cadence": discovery_cadence,
                        "operation_id": operation_id,
                        "publication_watch": publication_watch,
                        "selection": [b.session for b in current_bindings],
                    },
                )
                store.release_lock()
            return WaitResult(
                resumed=resumed,
                predicate_matched=False,
                job_id=job_id,
            )

        # Step E: Release store lock between iterations to permit concurrent ACK/reply
        if store is not None:
            store.release_lock()

        # Step F: Run iteration hook while unlocked (for tests)
        if on_iteration_hook is not None:
            on_iteration_hook(iteration)

        # Step G: Sleep using Clock
        clock.sleep(poll_interval)
        iteration += 1

    # Loop exhausted max_iterations
    if store is not None:
        if store.acquire_lock(timeout=lock_timeout):
            store.update_job(job_id, status="waiting")
            store.release_lock()

    return WaitResult(
        resumed=resumed,
        predicate_matched=False,
        job_id=job_id,
    )


# =============================================================================
# ActionHandler for 'wait'
# =============================================================================


class WaitActionHandler:
    """ActionHandler for 'wait' operation in octodot plans."""

    def __init__(
        self,
        store: Store | None = None,
        read_service: ReadService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.read_service = read_service
        self.clock = clock or SystemClock()

    def can_handle(self, op: str) -> bool:
        return op == "wait"

    def execute(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        return self.handle(action, context)

    def handle(self, action: dict[str, Any], context: dict[str, Any]) -> ActionResult:
        action_id = str(action.get("id") or action.get("action_id", "wait_action"))
        args = action.get("args") or action.get("params") or {}
        predicate = str(args.get("predicate", "all_terminal"))
        timeout = float(args.get("timeout_seconds", 30.0))
        job_id = args.get("job_id")
        selection = args.get("selection", ())
        lock_timeout = float(args.get("lock_timeout", 5.0))
        profile = str(context.get("profile", "default"))
        plan_id = str(context.get("plan_id", "plan"))

        # Context overrides
        store = context.get("store") or self.store
        read_service = context.get("read_service") or self.read_service
        clock = context.get("clock") or self.clock

        allow_discovery = bool(args.get("allow_discovery", False))
        discovery_cadence = int(args.get("discovery_cadence", 0))
        operation_id = args.get("operation_id")
        publication_watch = bool(args.get("publication_watch", False))

        try:
            wait_res = execute_wait(
                predicate=predicate,
                timeout_seconds=timeout,
                job_id=job_id,
                selection=selection,
                profile=profile,
                plan_id=plan_id,
                store=store,
                read_service=read_service,
                clock=clock,
                allow_discovery=allow_discovery,
                discovery_cadence=discovery_cadence,
                operation_id=operation_id,
                publication_watch=publication_watch,
                lock_timeout=lock_timeout,
            )

            data = {
                "resumed": wait_res.resumed,
                "predicate_matched": wait_res.predicate_matched,
                "job_id": wait_res.job_id,
            }

            if wait_res.predicate_matched:
                return ActionResult(
                    action_id=action_id,
                    op="wait",
                    status=ActionResultStatus.OK,
                    exit_code=EXIT_OK,
                    data=tuple(data.items()),
                )
            else:
                # Deadline yielded: status WAITING, exit code 2
                return ActionResult(
                    action_id=action_id,
                    op="wait",
                    status=ActionResultStatus.WAITING,
                    exit_code=EXIT_WAITING,
                    data=tuple(data.items()),
                )

        except OctodotError as err:
            code = err.code
            exit_code = (
                EXIT_INTERRUPTED if code == ErrorCode.CANCELLED else EXIT_FATAL_READ_OR_LOCAL
            )
            status = (
                ActionResultStatus.INTERRUPTED
                if code == ErrorCode.CANCELLED
                else ActionResultStatus.ERROR
            )
            return ActionResult(
                action_id=action_id,
                op="wait",
                status=status,
                exit_code=exit_code,
                error_code=code,
                data=(("error", str(err)),),
            )
