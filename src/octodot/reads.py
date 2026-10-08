"""Full-scan read service for bounded inventory, inspection, and chat collection.

Standard library only. Compatible with Python 3.10+.
Implements the frozen ReadService protocol with complete full scans as the default
and only correctness path (no createTime filtering).
Caps produce partial Coverage with skipped scope and resume references; snapshot_atomic is always False.
Independent session failures return good results plus typed failures without hiding failed/unknown items.
Full-scan commit persists activities, projection, checkpoint, and events in one short store transaction;
incomplete scans never advance completeness.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import time
from typing import Any, Callable, Iterable, Sequence

from octodot.contracts import (
    Clock,
    JulesReadAPI,
    ReadService as ReadServiceProtocol,
    Store,
    canonical_hash,
)
from octodot.errors import ErrorCode, OctodotError, StateStoreError
from octodot.identity import (
    bind_session,
    compare_bindings,
    extract_session_branch,
    extract_session_repository,
    extract_session_source,
    resolve_source,
    validate_repository_name,
)
from octodot.models import (
    ActivityRecord,
    Binding,
    CandidateBundle,
    Coverage,
    Event,
    LifecycleBucket,
    Observation,
    SessionRecord,
    SourceRecord,
)
from octodot.projections import (
    LifecycleProjection,
    PlanProjection,
    project_candidate_bundle,
    project_lifecycle,
    project_plan,
)
from octodot.transport import SystemClock


def source_to_dict(s: SourceRecord) -> dict[str, Any]:
    """Convert SourceRecord to serializable dictionary preserving unknown fields."""
    d: dict[str, Any] = {"name": s.name}
    if s.id is not None:
        d["id"] = s.id
    if s.github_repo_owner is not None:
        d["github_repo_owner"] = s.github_repo_owner
    if s.github_repo_name is not None:
        d["github_repo_name"] = s.github_repo_name
    for k, v in s.unknown_fields:
        d[k] = v
    return d


def session_to_dict(s: SessionRecord) -> dict[str, Any]:
    """Convert SessionRecord to serializable dictionary preserving unknown fields."""
    d: dict[str, Any] = {"name": s.name, "state": s.state}
    if s.id is not None:
        d["id"] = s.id
    if s.title is not None:
        d["title"] = s.title
    if s.create_time is not None:
        d["createTime"] = s.create_time
    if s.update_time is not None:
        d["updateTime"] = s.update_time
    if s.require_plan_approval is not None:
        d["requirePlanApproval"] = s.require_plan_approval
    if s.source_context:
        d["sourceContext"] = dict(s.source_context)
    for k, v in s.unknown_fields:
        d[k] = v
    return d


def activity_to_dict(a: ActivityRecord) -> dict[str, Any]:
    """Convert ActivityRecord to serializable dictionary preserving unknown fields."""
    d: dict[str, Any] = {"name": a.name, "type": a.activity_type}
    if a.id is not None:
        d["id"] = a.id
    if a.create_time is not None:
        d["createTime"] = a.create_time
    if a.originator is not None:
        d["originator"] = a.originator
    for k, v in a.unknown_fields:
        d[k] = v
    return d


# =====================================================================
# Typed Result / Collection Models implementing Mapping
# =====================================================================


class InventoryCollection(Mapping):
    """Result of inventory collection implementing Mapping for transparent access."""

    def __init__(
        self,
        sources: tuple[SourceRecord, ...],
        sessions: tuple[SessionRecord, ...],
        coverage: Coverage,
        unbound_sessions: tuple[SessionRecord, ...] = (),
        failures: tuple[tuple[str, str, str], ...] = (),
        metadata: tuple[tuple[str, Any], ...] = (),
    ) -> None:
        self.sources = sources
        self.sessions = sessions
        self.coverage = coverage
        self.unbound_sessions = unbound_sessions
        self.failures = failures
        self.metadata = metadata
        self.candidate_bundle: CandidateBundle | None = None
        self._data: dict[str, Any] = {
            "sources": tuple(source_to_dict(s) for s in sources),
            "sessions": tuple(session_to_dict(s) for s in sessions),
            "unbound_sessions": tuple(session_to_dict(s) for s in unbound_sessions),
            "coverage": coverage,
            "failures": failures,
        }

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def to_observation(self, binding: Binding | None = None) -> Observation:
        """Stage or serialize as durable models.Observation."""
        return Observation(
            binding=binding,
            sources=tuple(source_to_dict(s) for s in self.sources),
            sessions=tuple(session_to_dict(s) for s in self.sessions),
            coverage=self.coverage,
            metadata=self.metadata,
        )


class SessionInspection(Mapping):
    """Result of session inspection implementing Mapping for transparent access."""

    def __init__(
        self,
        session: SessionRecord,
        binding: Binding,
        state: str,
        title: str | None = None,
        lifecycle: LifecycleProjection | None = None,
        latest_plan: Any = None,
        latest_plan_id: str | None = None,
        latest_plan_hash: str | None = None,
        feedback_bundle: CandidateBundle | None = None,
        candidate_bundle: CandidateBundle | None = None,
        coverage: Coverage | None = None,
        activities: tuple[ActivityRecord, ...] = (),
        metadata: tuple[tuple[str, Any], ...] = (),
    ) -> None:
        self.session = session
        self.binding = binding
        self.state = state
        self.title = title
        self.lifecycle = lifecycle
        self.latest_plan = latest_plan
        self.latest_plan_id = latest_plan_id
        self.latest_plan_hash = latest_plan_hash
        self.feedback_bundle = feedback_bundle
        self.candidate_bundle = candidate_bundle or feedback_bundle
        self.coverage = coverage
        self.activities = activities
        self.metadata = metadata
        self._data: dict[str, Any] = {
            "session": session_to_dict(session),
            "binding": binding,
            "state": state,
            "title": title,
            "latest_plan": latest_plan,
            "latest_plan_id": latest_plan_id,
            "latest_plan_hash": latest_plan_hash,
            "feedback_bundle": feedback_bundle,
            "candidate_bundle": self.candidate_bundle,
            "coverage": coverage,
        }

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def to_observation(self) -> Observation:
        """Stage or serialize as durable models.Observation."""
        return Observation(
            binding=self.binding,
            sessions=(session_to_dict(self.session),),
            activities=tuple(activity_to_dict(a) for a in self.activities),
            coverage=self.coverage,
            candidate_bundle=self.candidate_bundle,
            metadata=self.metadata,
        )


class ChatsCollection(Mapping):
    """Result of chats collection implementing Mapping for transparent access."""

    def __init__(
        self,
        activities: tuple[ActivityRecord, ...],
        candidate_bundle: CandidateBundle,
        coverage: Coverage,
        session: SessionRecord | None = None,
        metadata: tuple[tuple[str, Any], ...] = (),
    ) -> None:
        self.activities = activities
        self.candidate_bundle = candidate_bundle
        self.feedback_bundle = candidate_bundle
        self.coverage = coverage
        self.session = session
        self.metadata = metadata
        self.messages = candidate_bundle.messages
        self.last_message_text = candidate_bundle.last_message_text
        self.latest_activity_id = candidate_bundle.selected_activity_id
        self._data: dict[str, Any] = {
            "activities": tuple(activity_to_dict(a) for a in activities),
            "messages": self.messages,
            "candidate_bundle": candidate_bundle,
            "feedback_bundle": candidate_bundle,
            "coverage": coverage,
            "last_message": self.last_message_text,
            "last_message_text": self.last_message_text,
            "latest_activity_id": self.latest_activity_id,
        }

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def to_observation(self, binding: Binding | None = None) -> Observation:
        """Stage or serialize as durable models.Observation."""
        return Observation(
            binding=binding,
            sessions=(session_to_dict(self.session),) if self.session else (),
            activities=tuple(activity_to_dict(a) for a in self.activities),
            coverage=self.coverage,
            candidate_bundle=self.candidate_bundle,
            metadata=self.metadata,
        )


# =====================================================================
# Concrete ReadService Implementation
# =====================================================================


class ReadService:
    """Full-scan read service implementing the ReadService protocol.

    - Full scan is default and only correctness path (no createTime filtering).
    - Caps produce partial Coverage with skipped scope and resume ref.
    - snapshot_atomic is always False.
    - Selection references reused sufficiently fresh within the same run only.
    - Mutation preflight always rescans.
    - Full-scan commit persists activities, projection, checkpoint, events atomically.
    - Incomplete scans never advance completeness.
    """

    def __init__(
        self,
        api: JulesReadAPI,
        store: Store | None = None,
        profile: str = "default",
        profile_epoch: int = 0,
        clock: Clock | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.profile = profile
        self.profile_epoch = profile_epoch
        if self.profile_epoch == 0 and self.store is not None:
            self.profile_epoch = self.store.get_profile_epoch(self.profile)
        self.clock = clock or SystemClock()
        self._cache: dict[str, Any] = {}
        self._staged_observations: dict[str, Observation] = {}

    # -----------------------------------------------------------------
    # Inventory Collection
    # -----------------------------------------------------------------

    def collect(
        self,
        scope: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> tuple[InventoryCollection, Coverage]:
        """Collect connected source and session inventory for scope.

        - scope: repository (OWNER/REPO), branch (optional), sessions (optional filter).
        - Repository scope without branch covers all starting branches.
        - Caps produce partial Coverage with skipped scope and resume reference.
        - snapshot_atomic is always False.
        - Repoless / unbindable entries are tracked without failing the entire collection.
        """
        scope_dict = scope or {}
        limits_dict = limits or {}

        target_repo = scope_dict.get("repository")
        target_branch = scope_dict.get("branch")
        target_sessions = set(scope_dict.get("sessions") or ())

        max_pages = limits_dict.get("max_pages", 100)
        max_sessions = limits_dict.get("max_sessions", 200)
        max_http_requests = limits_dict.get("max_http_requests", 120)
        deadline_seconds = float(limits_dict.get("deadline_seconds", 180.0))
        start_time = time.monotonic()

        session_pages_count = 0
        source_pages_count = 0
        requests_count = 0
        reasons: list[str] = []
        skipped_scope: list[str] = []
        resume_ref: str | None = None
        is_complete = True
        failures: list[tuple[str, str, str]] = []

        # 1. Fetch sources
        sources_list: list[SourceRecord] = []
        source_token: str | None = None
        seen_source_tokens: set[str] = set()

        while True:
            if time.monotonic() - start_time >= deadline_seconds:
                reasons.append("deadline_exceeded")
                skipped_scope.append("sources")
                is_complete = False
                break
            if requests_count >= max_http_requests:
                reasons.append("request_cap_reached")
                skipped_scope.append("sources")
                is_complete = False
                break
            if source_pages_count >= max_pages:
                reasons.append("page_cap_reached")
                skipped_scope.append("sources")
                resume_ref = source_token
                is_complete = False
                break

            if source_token is not None:
                if source_token in seen_source_tokens:
                    reasons.append("token_cycle_detected")
                    skipped_scope.append("sources")
                    is_complete = False
                    raise OctodotError(
                        ErrorCode.MALFORMED_RESPONSE,
                        f"Page token cycle detected in sources: '{source_token}'",
                    )
                seen_source_tokens.add(source_token)

            try:
                page_sources, next_s_token = self.api.sources_list(
                    page_token=source_token, page_size=100
                )
                requests_count += 1
                source_pages_count += 1
                sources_list.extend(page_sources)
            except OctodotError as err:
                if err.code in (ErrorCode.AUTH_DENIED, ErrorCode.UNSAFE_STATE_DIR):
                    raise
                reasons.append(err.code.value if hasattr(err.code, "value") else str(err.code))
                skipped_scope.append("sources")
                is_complete = False
                failures.append(("sources", err.code.value if hasattr(err.code, "value") else str(err.code), str(err)))
                break

            if not next_s_token:
                break
            source_token = next_s_token

        # 2. Fetch sessions paginated (handling empty continuing pages and cycles)
        session_token: str | None = None
        seen_session_tokens: set[str] = set()
        seen_identities: dict[str, SessionRecord] = {}
        all_sessions: list[SessionRecord] = []

        while is_complete or (not reasons and session_token is not None):
            if time.monotonic() - start_time >= deadline_seconds:
                reasons.append("deadline_exceeded")
                skipped_scope.append("sessions")
                resume_ref = session_token
                is_complete = False
                break
            if requests_count >= max_http_requests:
                reasons.append("request_cap_reached")
                skipped_scope.append("sessions")
                resume_ref = session_token
                is_complete = False
                break
            if session_pages_count >= max_pages:
                reasons.append("page_cap_reached")
                skipped_scope.append("sessions")
                resume_ref = session_token
                is_complete = False
                break

            if session_token is not None:
                if session_token in seen_session_tokens:
                    reasons.append("token_cycle_detected")
                    skipped_scope.append("sessions")
                    is_complete = False
                    raise OctodotError(
                        ErrorCode.MALFORMED_RESPONSE,
                        f"Page token cycle detected in sessions: '{session_token}'",
                    )
                seen_session_tokens.add(session_token)

            # Cap fetch page size to remaining sessions budget
            remaining_sessions = max(1, max_sessions - len(all_sessions))
            page_fetch_size = min(100, remaining_sessions)

            try:
                page_sessions, next_sess_token = self.api.sessions_list(
                    page_token=session_token, page_size=page_fetch_size
                )
                requests_count += 1
                session_pages_count += 1
            except OctodotError as err:
                if err.code in (ErrorCode.AUTH_DENIED, ErrorCode.IDENTITY_AMBIGUOUS):
                    raise
                # Expired page token or transport failure produces partial coverage
                reasons.append(err.code.value if hasattr(err.code, "value") else str(err.code))
                skipped_scope.append("sessions")
                resume_ref = session_token
                is_complete = False
                failures.append(("sessions", err.code.value if hasattr(err.code, "value") else str(err.code), str(err)))
                break

            # If page cap reached and more pages remain, record partial coverage and resume ref
            if session_pages_count >= max_pages and next_sess_token:
                reasons.append("page_cap_reached")
                skipped_scope.append("sessions")
                resume_ref = next_sess_token
                is_complete = False

            # Process items on this page (even if page was empty with continuing token)
            for sess in page_sessions:
                if sess.name in seen_identities:
                    prev = seen_identities[sess.name]
                    if prev != sess:
                        reasons.append("conflicting_duplicate_detected")
                        is_complete = False
                        raise OctodotError(
                            ErrorCode.IDENTITY_AMBIGUOUS,
                            f"Conflicting duplicate session identity: '{sess.name}'",
                        )
                else:
                    seen_identities[sess.name] = sess
                    all_sessions.append(sess)

                # Check max_sessions cap
                if len(all_sessions) >= max_sessions and next_sess_token:
                    reasons.append("session_cap_reached")
                    skipped_scope.append("sessions")
                    resume_ref = next_sess_token
                    is_complete = False
                    break

            if not is_complete:
                break

            if not next_sess_token:
                break
            session_token = next_sess_token

        # 3. Filter and categorize sessions according to scope
        matching_sessions: list[SessionRecord] = []
        unbound_sessions: list[SessionRecord] = []

        for s in all_sessions:
            if target_sessions and s.name not in target_sessions:
                continue

            sess_repo = extract_session_repository(s, sources_list)
            if sess_repo is None:
                unbound_sessions.append(s)
                if target_repo is not None:
                    # Specific repo requested: repoless session is excluded from matching
                    continue
                else:
                    matching_sessions.append(s)
                continue

            if target_repo is not None:
                if sess_repo != target_repo:
                    continue

                if target_branch is not None:
                    sess_branch = extract_session_branch(s)
                    if sess_branch != target_branch:
                        continue

            matching_sessions.append(s)

        pages_reported = session_pages_count if (all_sessions or session_pages_count > 0) else source_pages_count

        # Always snapshot_atomic = False!
        coverage = Coverage(
            complete=is_complete,
            snapshot_atomic=False,
            pages=pages_reported,
            items=len(matching_sessions),
            skipped_scope=tuple(sorted(set(skipped_scope))),
            reasons=tuple(sorted(set(reasons))),
            resume_ref=resume_ref,
        )

        collection = InventoryCollection(
            sources=tuple(sources_list),
            sessions=tuple(matching_sessions),
            coverage=coverage,
            unbound_sessions=tuple(unbound_sessions),
            failures=tuple(failures),
        )

        # Cache sufficiently fresh inventory in-memory for the current run
        if target_repo:
            self._cache[f"inventory:{target_repo}"] = collection
        self._cache["inventory:all"] = collection

        return collection, coverage

    # -----------------------------------------------------------------
    # Session Inspection
    # -----------------------------------------------------------------

    def inspect(self, binding: Binding, fresh: bool = True) -> SessionInspection:
        """Inspect specific session binding, state, lifecycle, and latest plan.

        - Verifies exact repository/branch binding.
        - Preserves unknown lifecycle states verbatim.
        - Projects latest plan and candidate feedback bundle.
        - fresh=True always rescans (required for mutation preflight).
        """
        cache_key = f"inspect:{binding.session}"
        if not fresh and cache_key in self._cache:
            return self._cache[cache_key]

        if not binding.session:
            raise OctodotError(ErrorCode.INVALID_INPUT, "Binding must specify a session")

        # 1. Fetch remote session
        session = self.api.sessions_get(binding.session)

        # 2. Verify binding
        sources_list: list[SourceRecord] = []
        try:
            sources_coll, _ = self.collect(scope={"scope": "all"}, limits={"max_pages": 10})
            sources_list = list(sources_coll.sources)
        except Exception:
            pass

        observed_repo = extract_session_repository(session, sources_list)
        if binding.repository and observed_repo != binding.repository:
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Session repository '{observed_repo}' does not match binding repository '{binding.repository}'",
            )

        if binding.starting_branch is not None:
            observed_branch = extract_session_branch(session)
            if observed_branch is None:
                raise OctodotError(
                    ErrorCode.BRANCH_UNVERIFIED,
                    f"Session '{session.name}' starting branch is absent/unverified",
                )
            if observed_branch != binding.starting_branch:
                raise OctodotError(
                    ErrorCode.BINDING_MISMATCH,
                    f"Starting branch mismatch: expected '{binding.starting_branch}', observed '{observed_branch}'",
                )

        # 3. Project lifecycle
        lifecycle = project_lifecycle(session)

        # 4. Fetch activities paginated (full scan)
        activities, act_cov = self.api.paginate_activities(session.name, max_pages=100)

        # 5. Project plan and feedback bundle
        plan_proj = project_plan(activities, session)
        candidate_bundle = project_candidate_bundle(
            activities, coverage=act_cov, current_session=session
        )

        inspection = SessionInspection(
            session=session,
            binding=binding,
            state=session.state,
            title=session.title,
            lifecycle=lifecycle,
            latest_plan=plan_proj.raw_plan,
            latest_plan_id=plan_proj.latest_plan_id,
            latest_plan_hash=plan_proj.latest_plan_hash,
            feedback_bundle=candidate_bundle,
            candidate_bundle=candidate_bundle,
            coverage=act_cov,
            activities=activities,
        )

        self._cache[cache_key] = inspection
        return inspection

    # -----------------------------------------------------------------
    # Chat Collection
    # -----------------------------------------------------------------

    def chats(self, selection: dict[str, Any], fresh: bool = True) -> ChatsCollection:
        """Collect conversation activities and candidate feedback bundle for session.

        - Uses complete full scans first (no createTime filtering).
        - Projects CandidateBundle retaining chronological order and flagging ambiguity.
        """
        session_name = selection.get("session") or selection.get("target")
        if not session_name or not isinstance(session_name, str):
            raise OctodotError(ErrorCode.INVALID_INPUT, "Selection must specify a session")

        cache_key = f"chats:{session_name}"
        if not fresh and cache_key in self._cache:
            return self._cache[cache_key]

        # Fetch session record if possible (to detect drift)
        session_record: SessionRecord | None = None
        try:
            session_record = self.api.sessions_get(session_name)
        except Exception:
            pass

        # Full scan of activities (no createTime filtering)
        activities, act_cov = self.api.paginate_activities(session_name, max_pages=100)

        candidate_bundle = project_candidate_bundle(
            activities, coverage=act_cov, current_session=session_record
        )

        collection = ChatsCollection(
            activities=activities,
            candidate_bundle=candidate_bundle,
            coverage=act_cov,
            session=session_record,
        )

        self._cache[cache_key] = collection
        return collection

    # -----------------------------------------------------------------
    # Multi-Session Batch Reads with Independent Failures
    # -----------------------------------------------------------------

    def inspect_sessions_batch(
        self,
        session_names: Sequence[str],
        binding_template: Binding | None = None,
    ) -> tuple[tuple[SessionInspection, ...], tuple[tuple[str, str, str], ...]]:
        """Inspect multiple sessions independently.

        Per-session failures return good results plus typed failures without
        aborting or hiding failed/unknown/attention items.
        Returns (successful_inspections, failures).
        """
        successful: list[SessionInspection] = []
        failures: list[tuple[str, str, str]] = []

        for s_name in session_names:
            target_binding = binding_template
            if target_binding is None:
                target_binding = Binding(
                    profile=self.profile,
                    profile_epoch=self.profile_epoch,
                    source="",
                    repository="",
                    session=s_name,
                )
            elif target_binding.session != s_name:
                target_binding = Binding(
                    profile=target_binding.profile,
                    profile_epoch=target_binding.profile_epoch,
                    source=target_binding.source,
                    repository=target_binding.repository,
                    starting_branch=target_binding.starting_branch,
                    session=s_name,
                )

            try:
                insp = self.inspect(target_binding, fresh=True)
                successful.append(insp)
            except OctodotError as err:
                code_str = err.code.value if hasattr(err.code, "value") else str(err.code)
                failures.append((s_name, code_str, str(err)))
            except Exception as err:
                failures.append((s_name, ErrorCode.INTERNAL_ERROR.value, f"Internal error inspecting session '{s_name}': {type(err).__name__}"))

        return tuple(successful), tuple(failures)

    # -----------------------------------------------------------------
    # Atomic Full-Scan Persistence
    # -----------------------------------------------------------------

    def commit_full_scan(
        self,
        scan_id: str,
        observation: Observation,
        coverage: Coverage,
        events: Sequence[Event] = (),
        checkpoint_id: str | None = None,
        fault_point: str | None = None,
    ) -> None:
        """Persist activities, projection, checkpoint, and events in one atomic store transaction.

        Incomplete scans never advance completeness or checkpoint sequence.
        Raises StateStoreError if store is not configured.
        """
        if self.store is None:
            raise StateStoreError(
                ErrorCode.INTERNAL_ERROR,
                "Store is not configured; full scan cannot be committed to durable state",
            )

        self.store.commit_scan_bundle(
            scan_id=scan_id,
            profile=self.profile,
            observation=observation,
            events=events,
            coverage=coverage,
            checkpoint_id=checkpoint_id,
            fault_point=fault_point,
        )

    def rescan_for_preflight(self, binding: Binding) -> SessionInspection:
        """Fresh rescan required for mutation preflight (selection reuse forbidden)."""
        return self.inspect(binding, fresh=True)
