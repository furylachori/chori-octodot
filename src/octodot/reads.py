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
from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class ResolvedScope:
    """Resolved effective scope combining envelope scope and action narrowing."""

    repository: str | None = None
    branch: str | None = None
    sessions: tuple[str, ...] | None = None
    session: str | None = None

    @property
    def starting_branch(self) -> str | None:
        return self.branch


def resolve_effective_scope(
    envelope_scope: ResolvedScope | Mapping[str, Any] | None = None,
    action: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    target_session: str | None = None,
    *,
    scope: ResolvedScope | Mapping[str, Any] | None = None,
    action_params: Mapping[str, Any] | None = None,
) -> ResolvedScope:
    """Resolve effective read scope from envelope scope and action-level parameters.

    - Inherits the envelope scope (repository, branch, sessions).
    - Allows action-level narrowing (e.g. adding branch or subsetting sessions).
    - Rejects conflicting or broader targets with OctodotError(ErrorCode.BINDING_MISMATCH).
    - Unscoped envelope leaves account-wide reads intact (repository=None).
    """
    if scope is not None and envelope_scope is None:
        envelope_scope = scope
    if action_params is not None and params is None:
        params = action_params

    if isinstance(envelope_scope, ResolvedScope):
        env_repo: str | None = envelope_scope.repository
        env_branch: str | None = envelope_scope.branch
        env_sessions: tuple[str, ...] | None = envelope_scope.sessions
    elif isinstance(envelope_scope, Mapping):
        env = envelope_scope
        env_repo = env.get("repository") or None
        env_branch = env.get("starting_branch") or env.get("branch") or None
        env_sessions = None
        if "sessions" in env and env["sessions"] is not None:
            raw_sess = env["sessions"]
            if not isinstance(raw_sess, (list, tuple, set, frozenset)):
                raise OctodotError(ErrorCode.INVALID_INPUT, "Envelope scope.sessions must be a sequence of strings")
            env_sessions = tuple(str(s) for s in raw_sess)
    else:
        env_repo = None
        env_branch = None
        env_sessions = None

    p = params if isinstance(params, Mapping) else {}
    act = action if isinstance(action, Mapping) else {}
    op = str(act.get("op", ""))

    # 1. Target session
    sess_target = target_session
    if sess_target is None:
        sess_target = p.get("session") or (act.get("target") if op in ("session.inspect", "chats.collect") else None)

    # 2. Action repository
    act_repo = p.get("repository")
    if not act_repo and op == "inventory.collect":
        act_repo = act.get("target")
    if act_repo == "":
        act_repo = None

    # 3. Action branch
    act_branch = p.get("starting_branch") or p.get("branch")
    if act_branch == "":
        act_branch = None

    # 4. Action sessions
    act_sessions_raw = p.get("sessions")
    act_sessions: tuple[str, ...] | None = None
    if act_sessions_raw is not None:
        if not isinstance(act_sessions_raw, (list, tuple, set, frozenset)):
            raise OctodotError(ErrorCode.INVALID_INPUT, "Action params.sessions must be a sequence of strings")
        act_sessions = tuple(str(s) for s in act_sessions_raw)

    act_scope_param = p.get("scope")

    # --- Repository Resolution ---
    if env_repo is not None:
        if act_repo is not None:
            if act_repo != env_repo:
                raise OctodotError(
                    ErrorCode.BINDING_MISMATCH,
                    f"Action repository '{act_repo}' conflicts with envelope scope repository '{env_repo}'",
                )
            eff_repo = env_repo
        elif act_scope_param == "all":
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Action requests scope 'all' which broadens beyond envelope scope repository '{env_repo}'",
            )
        else:
            eff_repo = env_repo
    else:
        eff_repo = act_repo

    # --- Branch Resolution ---
    if env_branch is not None:
        if act_branch is not None:
            if act_branch != env_branch:
                raise OctodotError(
                    ErrorCode.BINDING_MISMATCH,
                    f"Action branch '{act_branch}' conflicts with envelope scope branch '{env_branch}'",
                )
            eff_branch = env_branch
        else:
            eff_branch = env_branch
    else:
        eff_branch = act_branch

    # --- Sessions Resolution ---
    if env_sessions is not None:
        env_sess_set = set(env_sessions)
        if sess_target is not None and sess_target not in env_sess_set:
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Target session '{sess_target}' is outside envelope sessions scope",
            )
        if act_sessions is not None:
            outside = set(act_sessions) - env_sess_set
            if outside:
                raise OctodotError(
                    ErrorCode.BINDING_MISMATCH,
                    f"Action sessions exceed envelope sessions scope: {sorted(outside)}",
                )
            eff_sessions = act_sessions
        else:
            eff_sessions = env_sessions
    else:
        if act_sessions is not None:
            eff_sessions = act_sessions
        elif sess_target is not None:
            eff_sessions = (sess_target,)
        else:
            eff_sessions = None

    return ResolvedScope(
        repository=eff_repo,
        branch=eff_branch,
        sessions=eff_sessions,
        session=sess_target,
    )


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
        self._inspect_cache: dict[str, Any] = self._cache
        self._staged_observations: dict[str, Observation] = {}

    # -----------------------------------------------------------------
    # Inventory Collection
    # -----------------------------------------------------------------

    def collect(
        self,
        scope: ResolvedScope | Mapping[str, Any] | None = None,
        limits: Mapping[str, Any] | None = None,
    ) -> tuple[InventoryCollection, Coverage]:
        """Collect connected source and session inventory for scope.

        - scope: repository (OWNER/REPO), branch (optional), sessions (optional filter).
        - Repository scope without branch covers all starting branches.
        - Caps produce partial Coverage with skipped scope and resume reference.
        - snapshot_atomic is always False.
        - Repoless / unbindable entries are tracked without failing the entire collection.
        """
        target_repo: str | None = None
        target_branch: str | None = None
        target_sessions: set[str] | None = None
        if isinstance(scope, ResolvedScope):
            target_repo = scope.repository
            target_branch = scope.branch
            if scope.sessions is not None:
                target_sessions = set(scope.sessions)
        elif isinstance(scope, Mapping):
            target_repo = scope.get("repository")
            target_branch = scope.get("branch")
            if scope.get("sessions") is not None:
                target_sessions = set(scope["sessions"])

        limits_dict = limits or {}

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

        if target_repo is not None:
            skipped_scope.append("scope_filter:repository")
        if target_branch is not None:
            skipped_scope.append("scope_filter:branch")
        if target_sessions is not None:
            skipped_scope.append("scope_filter:sessions")

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
            if target_sessions is not None and s.name not in target_sessions:
                continue

            sess_repo = extract_session_repository(s, sources_list)
            if sess_repo is None:
                unbound_sessions.append(s)
                if target_repo is not None:
                    # Specific repo requested: repoless session is excluded from matching
                    continue
                if target_branch is not None:
                    sess_branch = extract_session_branch(s)
                    if sess_branch != target_branch:
                        continue
                matching_sessions.append(s)
                continue

            if target_repo is not None and sess_repo != target_repo:
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
        if target_repo or target_branch or target_sessions:
            sess_key = ",".join(sorted(target_sessions)) if target_sessions else ""
            self._cache[f"inventory:{target_repo}:{target_branch}:{sess_key}"] = collection
        else:
            self._cache["inventory:all"] = collection

        return collection, coverage

    # -----------------------------------------------------------------
    # Session Inspection
    # -----------------------------------------------------------------

    def inspect(
        self,
        binding: Binding,
        fresh: bool = True,
        scope: ResolvedScope | Mapping[str, Any] | None = None,
    ) -> SessionInspection:
        """Inspect specific session binding, state, lifecycle, and latest plan.

        - Verifies exact repository/branch binding against effective scope.
        - Preserves unknown lifecycle states verbatim.
        - Projects latest plan and candidate feedback bundle.
        - fresh=True always rescans (required for mutation preflight).
        """
        # Resolve effective scope at the very start of inspect
        resolved_scope = resolve_effective_scope(
            scope=scope,
            action_params={
                "repository": binding.repository,
                "starting_branch": binding.starting_branch,
                "session": binding.session,
            },
        )

        scope_key = (
            f"{resolved_scope.repository or '*'}:"
            f"{resolved_scope.branch or '*'}:"
            f"{','.join(sorted(resolved_scope.sessions)) if resolved_scope.sessions else '*'}"
        )
        cache_key = f"inspect:{binding.session}:{binding.repository}:{binding.starting_branch}:{scope_key}"
        legacy_cache_key = f"inspect:{binding.session}:{binding.repository}:{binding.starting_branch}"

        if not fresh:
            verified_lookup_key = (
                f"inspect:{binding.session}:{resolved_scope.repository}:{binding.starting_branch}:{scope_key}"
                if (not binding.repository and resolved_scope.repository)
                else None
            )
            cached: SessionInspection | None = (
                self._inspect_cache.get(cache_key)
                or self._inspect_cache.get(legacy_cache_key)
                or (self._inspect_cache.get(verified_lookup_key) if verified_lookup_key else None)
            )
            if cached is not None:
                # Validate cached record against resolved_scope:
                if resolved_scope.sessions is not None and cached.session.name not in set(resolved_scope.sessions):
                    raise OctodotError(
                        ErrorCode.BINDING_MISMATCH,
                        f"Session '{cached.session.name}' is outside permitted sessions scope",
                    )
                if resolved_scope.repository is not None:
                    cached_repo = cached.binding.repository if cached.binding else None
                    if not cached_repo:
                        cached_repo = extract_session_repository(cached.session)
                    if cached_repo != resolved_scope.repository:
                        raise OctodotError(
                            ErrorCode.BINDING_MISMATCH,
                            f"Session repository '{cached_repo}' does not match scope repository '{resolved_scope.repository}'",
                        )
                if resolved_scope.starting_branch is not None:
                    cached_branch = cached.binding.starting_branch if cached.binding else None
                    if cached_branch is None:
                        cached_branch = extract_session_branch(cached.session)
                    if cached_branch is None:
                        raise OctodotError(
                            ErrorCode.BRANCH_UNVERIFIED,
                            f"Session '{cached.session.name}' starting branch is absent/unverified",
                        )
                    if cached_branch != resolved_scope.starting_branch:
                        raise OctodotError(
                            ErrorCode.BINDING_MISMATCH,
                            f"Starting branch mismatch: expected '{resolved_scope.starting_branch}', observed '{cached_branch}'",
                        )
                return cached

        if not binding.session:
            raise OctodotError(ErrorCode.INVALID_INPUT, "Binding must specify a session")

        # Verify against scope.sessions if provided
        scope_sessions: Sequence[str] | None = resolved_scope.sessions
        if scope_sessions is not None and binding.session not in set(scope_sessions):
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Session '{binding.session}' is outside permitted sessions scope",
            )

        # 1. Fetch remote session
        session = self.api.sessions_get(binding.session)

        # 2. Verify binding
        sources_list: list[SourceRecord] = []
        try:
            sources_coll, _ = self.collect(scope={"scope": "all"}, limits={"max_pages": 10})
            sources_list = list(sources_coll.sources)
        except Exception:
            pass

        req_repo = binding.repository or ((scope.repository if isinstance(scope, ResolvedScope) else scope.get("repository")) if scope else None)
        observed_repo = extract_session_repository(session, sources_list)
        if req_repo and observed_repo != req_repo:
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Session repository '{observed_repo}' does not match binding repository '{req_repo}'",
            )

        req_branch = binding.starting_branch if binding.starting_branch is not None else ((scope.branch if isinstance(scope, ResolvedScope) else scope.get("branch")) if scope else None)
        if req_branch is not None:
            observed_branch = extract_session_branch(session)
            if observed_branch is None:
                raise OctodotError(
                    ErrorCode.BRANCH_UNVERIFIED,
                    f"Session '{session.name}' starting branch is absent/unverified",
                )
            if observed_branch != req_branch:
                raise OctodotError(
                    ErrorCode.BINDING_MISMATCH,
                    f"Starting branch mismatch: expected '{req_branch}', observed '{observed_branch}'",
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

        effective_binding = binding
        if observed_repo and not binding.repository:
            effective_binding = replace(binding, repository=observed_repo)

        inspection = SessionInspection(
            session=session,
            binding=effective_binding,
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

        self._inspect_cache[cache_key] = inspection
        self._inspect_cache[legacy_cache_key] = inspection
        verified_cache_key = f"inspect:{effective_binding.session}:{effective_binding.repository}:{effective_binding.starting_branch}:{scope_key}"
        verified_legacy_cache_key = f"inspect:{effective_binding.session}:{effective_binding.repository}:{effective_binding.starting_branch}"
        self._inspect_cache[verified_cache_key] = inspection
        self._inspect_cache[verified_legacy_cache_key] = inspection
        return inspection

    # -----------------------------------------------------------------
    # Chat Collection
    # -----------------------------------------------------------------

    def chats(
        self,
        selection: dict[str, Any],
        fresh: bool = True,
        scope: ResolvedScope | Mapping[str, Any] | None = None,
    ) -> ChatsCollection:
        """Collect conversation activities and candidate feedback bundle for session.

        - Uses complete full scans first (no createTime filtering).
        - Projects CandidateBundle retaining chronological order and flagging ambiguity.
        - Verifies binding against effective scope with zero POST calls.
        """
        session_name = selection.get("session") or selection.get("target")
        if not session_name or not isinstance(session_name, str):
            raise OctodotError(ErrorCode.INVALID_INPUT, "Selection must specify a session")

        # Resolve expected repo, branch, and sessions from scope and selection
        scope_repo = (scope.repository if isinstance(scope, ResolvedScope) else scope.get("repository")) if scope else None
        expected_repo = scope_repo or selection.get("repository")

        scope_branch = (scope.branch if isinstance(scope, ResolvedScope) else scope.get("branch")) if scope else None
        expected_branch = scope_branch if scope_branch is not None else selection.get("branch")

        scope_sessions = (scope.sessions if isinstance(scope, ResolvedScope) else scope.get("sessions")) if scope else None
        if scope_sessions is not None and session_name not in set(scope_sessions):
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Session '{session_name}' is outside permitted sessions scope",
            )

        cache_key = f"chats:{session_name}:{expected_repo}:{expected_branch}"
        if not fresh and cache_key in self._cache:
            return self._cache[cache_key]

        # Fetch session record
        session_record: SessionRecord | None = None
        if expected_repo or expected_branch is not None:
            session_record = self.api.sessions_get(session_name)
            sources_list: list[SourceRecord] = []
            try:
                sources_coll, _ = self.collect(scope={"scope": "all"}, limits={"max_pages": 10})
                sources_list = list(sources_coll.sources)
            except Exception:
                pass

            if expected_repo:
                observed_repo = extract_session_repository(session_record, sources_list)
                if observed_repo != expected_repo:
                    raise OctodotError(
                        ErrorCode.BINDING_MISMATCH,
                        f"Session repository '{observed_repo}' does not match expected repository '{expected_repo}'",
                    )

            if expected_branch is not None:
                observed_branch = extract_session_branch(session_record)
                if observed_branch is None:
                    raise OctodotError(
                        ErrorCode.BRANCH_UNVERIFIED,
                        f"Session '{session_name}' starting branch is absent/unverified",
                    )
                if observed_branch != expected_branch:
                    raise OctodotError(
                        ErrorCode.BINDING_MISMATCH,
                        f"Starting branch mismatch: expected '{expected_branch}', observed '{observed_branch}'",
                    )
        else:
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

        eff_coverage = coverage
        eff_observation = observation
        skipped: set[str] = set()
        if eff_coverage and eff_coverage.skipped_scope:
            skipped.update(eff_coverage.skipped_scope)
        if observation and observation.coverage and observation.coverage.skipped_scope:
            skipped.update(observation.coverage.skipped_scope)

        if any(s.startswith("scope_filter:") for s in skipped):
            base_cov = eff_coverage or (observation.coverage if observation else None)
            reasons = list(base_cov.reasons) if base_cov else []
            if "scoped_read_not_full_scan" not in reasons:
                reasons.append("scoped_read_not_full_scan")
            skipped_list = list(base_cov.skipped_scope) if base_cov else list(skipped)
            eff_coverage = Coverage(
                complete=False,
                snapshot_atomic=False,
                pages=base_cov.pages if base_cov else 0,
                items=base_cov.items if base_cov else 0,
                skipped_scope=tuple(sorted(set(skipped_list))),
                reasons=tuple(sorted(set(reasons))),
                resume_ref=base_cov.resume_ref if base_cov else None,
            )
            if observation:
                eff_observation = Observation(
                    binding=observation.binding,
                    sources=observation.sources,
                    sessions=observation.sessions,
                    activities=observation.activities,
                    coverage=eff_coverage,
                    candidate_bundle=observation.candidate_bundle,
                    metadata=observation.metadata,
                )

        self.store.commit_scan_bundle(
            scan_id=scan_id,
            profile=self.profile,
            observation=eff_observation,
            events=events,
            coverage=eff_coverage,
            checkpoint_id=checkpoint_id,
            fault_point=fault_point,
        )

    def rescan_for_preflight(self, binding: Binding) -> SessionInspection:
        """Fresh rescan required for mutation preflight (selection reuse forbidden)."""
        return self.inspect(binding, fresh=True)
