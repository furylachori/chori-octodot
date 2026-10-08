"""Versioned ordered action runner for octodot execution plans.

Standard library only. Compatible with Python 3.10+.
Implements whole-plan validation, typed dependency resolution,
mutation suppression, replay binding, artifact spilling, and bounded results.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from octodot.authorization import DisabledGrantVerifier, require_verifier_allowed
from octodot.contracts import (
    ActionHandler,
    Coverage,
    GrantVerifier,
    LIVE_INVOCATION_DEFAULTS,
    OPERATION_INVENTORY,
    OperationClassification,
    ResultBuilder,
    canonical_bytes,
    check_execution_eligibility,
    compute_plan_hash,
    load_strict_json,
    validate_plan,
    validate_result,
)
from octodot.errors import (
    ALL_EXIT_CODES,
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_INTERRUPTED,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    EXIT_WAITING,
    ErrorCode,
    OctodotError,
    combine_exit_codes,
)
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ArtifactManifest,
    Binding,
    OperationRecord,
    OperationState,
)
from octodot.transport import FixtureTransport


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def spill_artifact(
    content: str | bytes,
    artifacts_dir: str,
    media_type: str = "text/plain",
    store: Any = None,
    artifact_id: str | None = None,
) -> ArtifactManifest:
    """Spill content to a checksummed bounded private artifact file.

    Directory permissions: 0700 (owner-only).
    File permissions: 0600 (owner-only).
    """
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content

    os.makedirs(artifacts_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(artifacts_dir, 0o700)
    except OSError:
        pass

    content_hash = "sha256:" + hashlib.sha256(content_bytes).hexdigest()
    if artifact_id is None:
        short_hash = hashlib.sha256(content_bytes).hexdigest()[:16]
        artifact_id = f"art-{short_hash}"

    filename = f"{artifact_id}.txt"
    filepath = os.path.join(artifacts_dir, filename)

    fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "wb") as f:
        f.write(content_bytes)

    now = _utc_now_iso()
    manifest = ArtifactManifest(
        artifact_id=artifact_id,
        path=filepath,
        content_hash=content_hash,
        byte_count=len(content_bytes),
        media_type=media_type,
        created_at=now,
    )

    if store is not None and hasattr(store, "save_manifest"):
        store.save_manifest(manifest)

    return manifest


def _resolve_references(
    obj: Any,
    executed_results: Mapping[str, ActionResult],
) -> tuple[Any, bool]:
    """Recursively resolve selection references against earlier action results.

    Returns (resolved_obj, success). If any dependency is missing, failed,
    or yielded a null value, success is False.
    """
    if isinstance(obj, Mapping):
        if "from" in obj and "select" in obj:
            from_id = str(obj["from"])
            select_key = str(obj["select"])
            if from_id not in executed_results:
                return None, False
            earlier_res = executed_results[from_id]
            if earlier_res.status != ActionResultStatus.OK:
                return None, False
            data_dict = earlier_res.data_dict
            if select_key not in data_dict:
                return None, False
            val = data_dict[select_key]
            if val is None:
                return None, False
            return val, True
        else:
            resolved_dict: dict[str, Any] = {}
            for k, v in obj.items():
                resolved_v, ok = _resolve_references(v, executed_results)
                if not ok:
                    return None, False
                resolved_dict[k] = resolved_v
            return resolved_dict, True
    elif isinstance(obj, (list, tuple)):
        resolved_list: list[Any] = []
        for item in obj:
            resolved_item, ok = _resolve_references(item, executed_results)
            if not ok:
                return None, False
            resolved_list.append(resolved_item)
        return resolved_list, True
    return obj, True


def _spill_large_fields(
    data: Mapping[str, Any],
    artifacts_dir: str,
    store: Any = None,
    threshold_bytes: int = 4096,
) -> dict[str, Any]:
    """Inspect data dictionary and spill values exceeding threshold to private artifacts."""
    updated: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v.encode("utf-8")) > threshold_bytes:
            manifest = spill_artifact(v, artifacts_dir, store=store)
            updated[k] = {
                "artifact_id": manifest.artifact_id,
                "path": manifest.path,
                "content_hash": manifest.content_hash,
                "byte_count": manifest.byte_count,
                "media_type": manifest.media_type,
            }
        elif isinstance(v, Mapping):
            updated[k] = _spill_large_fields(v, artifacts_dir, store=store, threshold_bytes=threshold_bytes)
        else:
            updated[k] = v
    return updated


def get_default_handlers(
    store: Any = None,
    read_service: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Construct default read and wait handlers registry."""
    handlers: dict[str, Any] = {}
    try:
        from octodot.actions.read import (
            CapabilitiesInspectHandler,
            ChatsCollectHandler,
            HealthcheckHandler,
            InventoryCollectHandler,
            SessionInspectHandler,
            SuggestionsCollectHandler,
        )
        handlers["inventory.collect"] = InventoryCollectHandler()
        handlers["session.inspect"] = SessionInspectHandler()
        handlers["chats.collect"] = ChatsCollectHandler()
        handlers["capabilities.inspect"] = CapabilitiesInspectHandler()
        handlers["healthcheck"] = HealthcheckHandler()
        handlers["suggestions.collect"] = SuggestionsCollectHandler()
    except ImportError:
        pass

    try:
        from octodot.jobs import WaitActionHandler
        handlers["wait"] = WaitActionHandler(store=store, read_service=read_service, clock=clock)
    except ImportError:
        pass

    try:
        from octodot.events import EventsAckHandler, EventsReadHandler
        if store is not None:
            handlers["events.read"] = EventsReadHandler(store)
            handlers["events.ack"] = EventsAckHandler(store)
    except ImportError:
        pass

    return handlers


class ActionRunner:
    """Ordered runner for jules-controller plans."""

    def __init__(
        self,
        handlers: Mapping[str, ActionHandler] | None = None,
        store: Any = None,
        read_service: Any = None,
        journal: Any = None,
        verifier: GrantVerifier | None = None,
        credential_source: Any = None,
        clock: Any = None,
        artifacts_dir: str | None = None,
        transport: Any = None,
        transport_factory: Callable[[], Any] | None = None,
        spill_threshold: int = 4096,
    ) -> None:
        self.injected_handlers = dict(handlers or {})
        self.store = store
        self.read_service = read_service
        self.journal = journal
        self.verifier = verifier or DisabledGrantVerifier()
        self.credential_source = credential_source
        self.clock = clock
        self.artifacts_dir = artifacts_dir
        self.transport = transport
        self.transport_factory = transport_factory
        self.spill_threshold = spill_threshold

    def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Execute a validated plan and produce a jules-controller.result.v1 document."""
        # 1. Whole-plan validation before constructing/accessing any credentials or network
        validate_plan(plan)
        check_execution_eligibility(plan)

        # 2. Lazy transport construction via factory if transport was not directly provided
        if self.transport is None and self.transport_factory is not None:
            self.transport = self.transport_factory()

        # 3. Composition and Execution Mode Checks
        # live is derived strictly from whether transport is not FixtureTransport
        live = not isinstance(self.transport, FixtureTransport)
        require_verifier_allowed(self.verifier, live=live)

        execution = plan.get("execution", {})
        mode = execution.get("mode", "read_only")

        # 3. Handler Registry Assembly
        handlers: dict[str, Any] = {}
        # Populate default read handlers
        handlers.update(get_default_handlers(store=self.store, read_service=self.read_service, clock=self.clock))
        # Override with injected handlers
        handlers.update(self.injected_handlers)

        # In read_only mode, mutation handlers are not even constructed or retained
        if mode == "read_only":
            for op_name, spec in OPERATION_INVENTORY.items():
                if spec.classification == OperationClassification.MUTATION:
                    handlers.pop(op_name, None)

        # Ensure artifacts directory
        if self.artifacts_dir is None:
            if self.store is not None and hasattr(self.store, "db_path"):
                base_dir = os.path.dirname(self.store.db_path)
            else:
                base_dir = os.getcwd()
            self.artifacts_dir = os.path.join(base_dir, "artifacts")

        plan_id = str(plan.get("plan_id", ""))
        plan_hash = str(plan.get("plan_hash", ""))
        actions = plan.get("actions", [])

        # 4. Replay & Hash Binding Checks (S09-T03 / F3)
        if self.store is not None:
            # 4a. Durable Plan Binding Check:
            # Reusing the same plan_id with a different hash is rejected even if action rows are incomplete
            if hasattr(self.store, "record_plan_binding"):
                self.store.record_plan_binding(plan_id, plan_hash)
            elif hasattr(self.store, "get_plan_binding"):
                binding = self.store.get_plan_binding(plan_id)
                if binding and binding.get("plan_hash") != plan_hash:
                    raise OctodotError(
                        ErrorCode.OPERATION_CONFLICT,
                        f"Plan '{plan_id}' previously executed with hash '{binding.get('plan_hash')}', "
                        f"conflicting with current hash '{plan_hash}'",
                    )
            elif hasattr(self.store, "_conn") and self.store._conn is not None:
                cursor = self.store._conn.cursor()
                cursor.execute(
                    "SELECT action_id, data_json FROM action_results WHERE plan_id = ?",
                    (plan_id,),
                )
                for row in cursor.fetchall():
                    try:
                        dj = json.loads(row["data_json"] or "{}")
                        h = dj.get("_plan_hash") or dj.get("plan_hash")
                        if h and h != plan_hash:
                            raise OctodotError(
                                ErrorCode.OPERATION_CONFLICT,
                                f"Plan '{plan_id}' previously executed with hash '{h}', conflicting with '{plan_hash}'",
                            )
                    except OctodotError:
                        raise
                    except Exception:
                        pass

            # 4b. Plan-Scoped Replay Check:
            # Fetch each action result plan-scoped by (plan_id, action_id)
            if hasattr(self.store, "get_action_result"):
                replayed: list[ActionResult] = []
                for act in actions:
                    aid = act["id"]
                    stored_ar = self.store.get_action_result(aid, plan_id=plan_id)
                    if stored_ar is not None:
                        # Extra check: ensure stored action result plan_hash matches current plan_hash
                        dj = dict(stored_ar.data)
                        row_hash = dj.get("_plan_hash") or dj.get("plan_hash")
                        if row_hash and row_hash != plan_hash:
                            raise OctodotError(
                                ErrorCode.OPERATION_CONFLICT,
                                f"Action result '{aid}' belongs to plan hash '{row_hash}', "
                                f"conflicting with '{plan_hash}'",
                            )
                        replayed.append(stored_ar)

                if len(replayed) == len(actions):
                    rb = ResultBuilder(plan_id=plan_id)
                    for ar in replayed:
                        rb.add_action_result(ar)
                    result = rb.build()
                    validate_result(result)
                    return result

        # 5. Check: A new run cannot repeat a recorded mutation (S09-T03)
        if self.store is not None and hasattr(self.store, "get_operation"):
            for act in actions:
                op_name = act.get("op", "")
                spec = OPERATION_INVENTORY.get(op_name)
                if spec and spec.classification == OperationClassification.MUTATION:
                    op_id = act.get("operation_id")
                    if op_id:
                        op_rec = self.store.get_operation(op_id)
                        if op_rec is not None and op_rec.state in (
                            OperationState.ACCEPTED,
                            OperationState.EFFECT_OBSERVED,
                            OperationState.UNKNOWN,
                            OperationState.REJECTED,
                            OperationState.BLOCKED_BEFORE_DISPATCH,
                        ):
                            raise OctodotError(
                                ErrorCode.OPERATION_CONFLICT,
                                f"Mutation operation '{op_id}' was already recorded in state '{op_rec.state.value}' "
                                "and cannot be repeated in a new run",
                            )

        # 6. Ordered Execution Loop
        result_builder = ResultBuilder(plan_id=plan_id)
        executed_results: dict[str, ActionResult] = {}
        failed_action_ids: set[str] = set()
        mutation_blocked: bool = False
        all_omitted_attention: list[str] = []
        overall_coverage_complete: bool = True
        coverage_reasons: list[str] = []
        coverage_skipped_scope: list[str] = []

        context: dict[str, Any] = {
            "store": self.store,
            "read_service": self.read_service,
            "clock": self.clock,
            "profile": plan.get("profile", "default"),
            "plan_id": plan_id,
            "scope": plan.get("scope", {}),
            "limits": plan.get("limits", LIVE_INVOCATION_DEFAULTS),
            "credential_source": self.credential_source,
            "verifier": self.verifier,
            "journal": self.journal,
            "transport": self.transport,
            "artifacts_dir": self.artifacts_dir,
            "executed_results": executed_results,
            "plan": plan,
        }

        try:
            for act in actions:
                act_id = act["id"]
                op = act["op"]
                spec = OPERATION_INVENTORY.get(op)
                is_mutation = bool(spec and spec.classification == OperationClassification.MUTATION)

                # Check 1: Mutation suppression
                if is_mutation and mutation_blocked:
                    ar = ActionResult.create(
                        action_id=act_id,
                        op=op,
                        status=ActionResultStatus.SKIPPED,
                        exit_code=EXIT_OK,
                        data={
                            "_plan_hash": plan_hash,
                            "reason": "Suppressed due to earlier mutation blocker",
                        },
                    )
                    result_builder.add_action_result(ar)
                    executed_results[act_id] = ar
                    if self.store is not None and hasattr(self.store, "save_action_result"):
                        self.store.save_action_result(ar, plan_id=plan_id)
                    continue

                # Check 2: Typed Selection References Resolution
                params = act.get("params") or {}
                resolved_params, ref_ok = _resolve_references(params, executed_results)
                if not ref_ok:
                    # Failed read dependency skips dependent action
                    ar = ActionResult.create(
                        action_id=act_id,
                        op=op,
                        status=ActionResultStatus.SKIPPED,
                        exit_code=EXIT_OK,
                        data={
                            "_plan_hash": plan_hash,
                            "reason": "Dependency unsatisfied or failed",
                        },
                    )
                    failed_action_ids.add(act_id)
                    result_builder.add_action_result(ar)
                    executed_results[act_id] = ar
                    if self.store is not None and hasattr(self.store, "save_action_result"):
                        self.store.save_action_result(ar, plan_id=plan_id)
                    continue

                # Prepare action copy with resolved parameters
                action_to_execute = dict(act)
                if "params" in act:
                    action_to_execute["params"] = resolved_params

                # Handler lookup
                handler = handlers.get(op)
                if handler is None:
                    for h in handlers.values():
                        if hasattr(h, "can_handle") and h.can_handle(op):
                            handler = h
                            break

                if handler is None:
                    ar = ActionResult.create(
                        action_id=act_id,
                        op=op,
                        status=ActionResultStatus.UNSUPPORTED,
                        exit_code=EXIT_PARTIAL_OR_UNSUPPORTED,
                        error_code=ErrorCode.UNSUPPORTED_PUBLIC_API,
                        data={
                            "_plan_hash": plan_hash,
                            "error": f"No handler registered for operation '{op}'",
                        },
                    )
                else:
                    # Handler invocation
                    try:
                        if hasattr(handler, "execute"):
                            ar = handler.execute(action_to_execute, context)
                        elif hasattr(handler, "handle"):
                            ar = handler.handle(action_to_execute, context)
                        else:
                            raise OctodotError(
                                ErrorCode.INTERNAL_ERROR,
                                f"Handler for '{op}' implements neither execute nor handle",
                            )
                    except OctodotError as err:
                        if err.code == ErrorCode.CANCELLED:
                            st = ActionResultStatus.INTERRUPTED
                            ec = EXIT_INTERRUPTED
                        elif is_mutation:
                            st = ActionResultStatus.BLOCKED
                            ec = EXIT_MUTATION_BLOCKED
                        else:
                            st = ActionResultStatus.ERROR
                            ec = EXIT_FATAL_READ_OR_LOCAL
                        ar = ActionResult.create(
                            action_id=act_id,
                            op=op,
                            status=st,
                            exit_code=ec,
                            error_code=err.code,
                            data={"_plan_hash": plan_hash, "error": err.message},
                        )
                    except Exception as exc:
                        st = ActionResultStatus.BLOCKED if is_mutation else ActionResultStatus.ERROR
                        ec = EXIT_MUTATION_BLOCKED if is_mutation else EXIT_FATAL_READ_OR_LOCAL
                        ar = ActionResult.create(
                            action_id=act_id,
                            op=op,
                            status=st,
                            exit_code=ec,
                            error_code=ErrorCode.INTERNAL_ERROR,
                            data={"_plan_hash": plan_hash, "error": str(exc)},
                        )

                # Process ActionResult data: ensure _plan_hash is recorded
                data_dict = dict(ar.data)
                data_dict["_plan_hash"] = plan_hash

                # Large text spill to checksummed bounded private artifacts (S09-T04)
                data_dict = _spill_large_fields(
                    data_dict,
                    artifacts_dir=self.artifacts_dir,
                    store=self.store,
                    threshold_bytes=self.spill_threshold,
                )

                # Reconstruct ActionResult with spilled data and _plan_hash
                ar = ActionResult(
                    action_id=ar.action_id,
                    op=ar.op,
                    status=ar.status,
                    exit_code=ar.exit_code,
                    error_code=ar.error_code,
                    coverage=ar.coverage,
                    data=tuple(data_dict.items()),
                )

                # Check outcome for mutation blocking and dependency failures
                if is_mutation:
                    if ar.status in (
                        ActionResultStatus.BLOCKED,
                        ActionResultStatus.REJECTED,
                        ActionResultStatus.UNKNOWN,
                        ActionResultStatus.ERROR,
                    ):
                        mutation_blocked = True

                if ar.status not in (ActionResultStatus.OK, ActionResultStatus.WAITING):
                    failed_action_ids.add(act_id)

                # Track coverage and attention items
                if ar.coverage is not None and not ar.coverage.complete:
                    overall_coverage_complete = False
                    coverage_reasons.extend(ar.coverage.reasons)
                    coverage_skipped_scope.extend(ar.coverage.skipped_scope)

                # Collect any attention items from action data or coverage
                if "omitted_attention_items" in data_dict:
                    items = data_dict["omitted_attention_items"]
                    if isinstance(items, (list, tuple)):
                        all_omitted_attention.extend(str(x) for x in items)

                # If session inspection produced attention state that was truncated
                if ar.data_dict.get("truncated"):
                    overall_coverage_complete = False
                    if "output_cap_reached" not in coverage_reasons:
                        coverage_reasons.append("output_cap_reached")

                # Handle waiting resume_ref
                if ar.status == ActionResultStatus.WAITING:
                    job_id = ar.data_dict.get("job_id")
                    if job_id:
                        result_builder.set_resume_ref(str(job_id))

                # Save action result to durable store
                if self.store is not None and hasattr(self.store, "save_action_result"):
                    self.store.save_action_result(ar, plan_id=plan_id)

                result_builder.add_action_result(ar)
                executed_results[act_id] = ar

        except KeyboardInterrupt:
            # Handle user interruption cleanly
            interrupted_ar = ActionResult.create(
                action_id="interrupted",
                op="interrupted",
                status=ActionResultStatus.INTERRUPTED,
                exit_code=EXIT_INTERRUPTED,
                error_code=ErrorCode.INTERRUPTED,
                data={"error": "Execution interrupted by user"},
            )
            result_builder.add_action_result(interrupted_ar)

        # 7. Coverage and Output Capping (S09-T04)
        for att_item in all_omitted_attention:
            result_builder.add_omitted_attention(att_item)

        final_cov = Coverage(
            complete=overall_coverage_complete,
            snapshot_atomic=False,
            reasons=tuple(sorted(set(coverage_reasons))),
            skipped_scope=tuple(sorted(set(coverage_skipped_scope))),
        )
        result_builder.set_coverage(final_cov)

        result_doc = result_builder.build()
        validate_result(result_doc)
        return result_doc


def run_plan(
    plan: dict[str, Any],
    handlers: Mapping[str, ActionHandler] | None = None,
    store: Any = None,
    read_service: Any = None,
    journal: Any = None,
    verifier: GrantVerifier | None = None,
    credential_source: Any = None,
    clock: Any = None,
    artifacts_dir: str | None = None,
    transport: Any = None,
    transport_factory: Callable[[], Any] | None = None,
    spill_threshold: int = 4096,
) -> dict[str, Any]:
    """Execute plan using ActionRunner and return validated result dictionary."""
    runner = ActionRunner(
        handlers=handlers,
        store=store,
        read_service=read_service,
        journal=journal,
        verifier=verifier,
        credential_source=credential_source,
        clock=clock,
        artifacts_dir=artifacts_dir,
        transport=transport,
        transport_factory=transport_factory,
        spill_threshold=spill_threshold,
    )
    return runner.run(plan)
