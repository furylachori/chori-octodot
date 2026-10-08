"""Deterministic repository/branch binding and identity resolution.

Standard library only. Compatible with Python 3.10+.
Pure functions over models.SourceRecord, SessionRecord, and Binding; no I/O.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from octodot.errors import ErrorCode, OctodotError
from octodot.models import Binding, SessionRecord, SourceRecord

# Strict OWNER/REPO pattern: exactly one forward slash, alphanumeric with _.-
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def validate_repository_name(repository: str) -> tuple[str, str]:
    """Validate repository name strictly adhering to OWNER/REPO format.

    Rejects:
    - Non-string or empty input
    - Missing slash or multiple slashes (e.g. OWNER//REPO, OWNER/REPO/EXTRA)
    - Leading or trailing slashes (e.g. /OWNER/REPO, OWNER/REPO/)
    - Surrounding whitespace
    - Invalid characters

    Returns (owner, repo) tuple.
    Raises OctodotError(ErrorCode.INVALID_INPUT, ...) if invalid.
    """
    if not isinstance(repository, str):
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Repository must be a string, got {type(repository).__name__}",
        )

    stripped = repository.strip()
    if stripped != repository:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Repository name contains whitespace: '{repository}'",
        )

    if not _REPO_PATTERN.match(repository):
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Repository name '{repository}' must strictly match OWNER/REPO format",
        )

    owner, repo = repository.split("/", 1)
    if not owner or not repo:
        raise OctodotError(
            ErrorCode.INVALID_INPUT,
            f"Repository name '{repository}' has empty owner or repo",
        )

    return owner, repo


def resolve_source(
    sources: Iterable[SourceRecord],
    repository: str,
) -> SourceRecord:
    """Resolve a single matching SourceRecord for an OWNER/REPO repository string.

    Matches strictly on structured fields:
        s.github_repo_owner == owner and s.github_repo_name == repo (case-sensitive).
    Never constructs 'sources/github/OWNER/REPO' from text.

    Rejects:
    - Malformed owner/repo types in source records (must be string).
    - Ambiguous matches (multiple matching sources -> IDENTITY_AMBIGUOUS).
    - Missing matches (zero matching sources -> BINDING_MISMATCH).
    - Invalid repository format -> INVALID_INPUT.
    """
    owner, repo = validate_repository_name(repository)

    matching_sources: list[SourceRecord] = []
    for s in sources:
        if not isinstance(s, SourceRecord):
            continue
        # Malformed owner/repo types in source record are ignored / not matched
        if not isinstance(s.github_repo_owner, str) or not isinstance(s.github_repo_name, str):
            continue
        # Exact case-sensitive match on structured fields
        if s.github_repo_owner == owner and s.github_repo_name == repo:
            matching_sources.append(s)

    if len(matching_sources) > 1:
        raise OctodotError(
            ErrorCode.IDENTITY_AMBIGUOUS,
            f"Ambiguous source match for repository '{repository}': "
            f"found {len(matching_sources)} matching sources",
        )

    if not matching_sources:
        raise OctodotError(
            ErrorCode.BINDING_MISMATCH,
            f"No matching source found for repository '{repository}'",
        )

    return matching_sources[0]


def resolve_source_name(
    sources: Iterable[SourceRecord],
    repository: str,
) -> str:
    """Resolve the remote source resource name for a repository.

    Never constructs 'sources/github/OWNER/REPO' from text.
    """
    source = resolve_source(sources, repository)
    return source.name


def _parse_source_context(
    session: SessionRecord,
) -> dict[str, Any]:
    """Extract source_context from session as a dictionary."""
    if not session.source_context:
        return {}
    if isinstance(session.source_context, Mapping):
        return dict(session.source_context)
    try:
        return dict(session.source_context)
    except (ValueError, TypeError):
        return {}


def extract_session_source(session: SessionRecord) -> str | None:
    """Extract source resource name from session's source_context."""
    sc = _parse_source_context(session)
    source = sc.get("source") or sc.get("sourceName") or sc.get("source_name")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return None


def extract_session_branch(session: SessionRecord) -> str | None:
    """Extract startingBranch from session's source_context.

    Checks:
    - githubRepoContext.startingBranch / github_repo_context.starting_branch
    - top-level startingBranch / starting_branch in source_context

    Returns exact branch string or None if absent.
    """
    sc = _parse_source_context(session)

    # Check nested githubRepoContext / github_repo_context
    gh_ctx = sc.get("githubRepoContext") or sc.get("github_repo_context")
    if isinstance(gh_ctx, Mapping):
        branch = gh_ctx.get("startingBranch") or gh_ctx.get("starting_branch")
        if isinstance(branch, str) and branch:
            return branch
    elif isinstance(gh_ctx, (list, tuple)):
        try:
            gh_dict = dict(gh_ctx)
            branch = gh_dict.get("startingBranch") or gh_dict.get("starting_branch")
            if isinstance(branch, str) and branch:
                return branch
        except (ValueError, TypeError):
            pass

    # Check top-level source_context
    top_branch = sc.get("startingBranch") or sc.get("starting_branch")
    if isinstance(top_branch, str) and top_branch:
        return top_branch

    return None


def extract_session_repository(
    session: SessionRecord,
    sources: Iterable[SourceRecord] | None = None,
) -> str | None:
    """Determine the session's repository name as OWNER/REPO.

    Checks:
    1. Direct githubRepo structured fields in session's source_context.
    2. Lookup of session.source in provided sources iterable.

    Returns None if repoless.
    """
    sc = _parse_source_context(session)

    # Check direct githubRepo in source_context
    gh_repo = sc.get("githubRepo") or sc.get("github_repo")
    if isinstance(gh_repo, Mapping):
        owner = gh_repo.get("owner")
        repo = gh_repo.get("repo")
        if isinstance(owner, str) and isinstance(repo, str) and owner and repo:
            candidate = f"{owner}/{repo}"
            if _REPO_PATTERN.match(candidate):
                return candidate

    # Check direct repository string
    direct_repo = sc.get("repository") or sc.get("repo")
    if isinstance(direct_repo, str) and _REPO_PATTERN.match(direct_repo):
        return direct_repo

    # Check via source lookup
    sess_source = extract_session_source(session)
    if sess_source and sources is not None:
        for s in sources:
            if not isinstance(s, SourceRecord):
                continue
            if s.name == sess_source:
                if isinstance(s.github_repo_owner, str) and isinstance(s.github_repo_name, str):
                    candidate = f"{s.github_repo_owner}/{s.github_repo_name}"
                    if _REPO_PATTERN.match(candidate):
                        return candidate

    return None


def verify_branch_selection(
    deliberate_branch: str | None,
    observed_branch: str | None,
) -> str:
    """Verify exact case-sensitive starting branch comparison.

    Rules:
    - If observed_branch is None or empty: raises OctodotError(ErrorCode.BRANCH_UNVERIFIED).
      Never substitutes default or absent branch (never defaults to main or master).
    - If deliberate_branch is provided: compares exact case-sensitive equality.
      Mismatch raises OctodotError(ErrorCode.BINDING_MISMATCH).
    - Explicit 'main' or 'master' is allowed ONLY when deliberate_branch equals it;
      it is never assumed as a default for absent branch.

    Returns the verified branch string.
    """
    if observed_branch is None or not observed_branch:
        raise OctodotError(
            ErrorCode.BRANCH_UNVERIFIED,
            "Starting branch is absent / unverified on session; "
            "default or absent-branch substitution is forbidden",
        )

    if deliberate_branch is not None:
        if observed_branch != deliberate_branch:
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Starting branch mismatch: expected '{deliberate_branch}', "
                f"observed '{observed_branch}' (exact case-sensitive comparison)",
            )

    return observed_branch


def bind_session(
    profile: str,
    profile_epoch: int,
    session: SessionRecord,
    sources: Iterable[SourceRecord] | None = None,
    required_repository: str | None = None,
    required_branch: str | None = None,
) -> Binding:
    """Construct and verify a Binding for a session.

    Enforces:
    - Session has a valid source and repository (rejects repoless sessions with BINDING_MISMATCH).
    - If required_repository specified: exact case-sensitive match (BINDING_MISMATCH).
    - If required_branch specified:
      - If session branch is absent: raises OctodotError(ErrorCode.BRANCH_UNVERIFIED).
      - If branch case or slashes differ: raises OctodotError(ErrorCode.BINDING_MISMATCH).
    - Explicit main/master is allowed only if explicitly requested, never defaulted.

    Returns a frozen Binding instance.
    """
    if not isinstance(profile, str) or not profile.strip():
        raise OctodotError(ErrorCode.INVALID_INPUT, "Profile must be a non-empty string")
    if not isinstance(profile_epoch, int) or profile_epoch < 0 or isinstance(profile_epoch, bool):
        raise OctodotError(ErrorCode.INVALID_INPUT, "profile_epoch must be a non-negative integer")

    repo = extract_session_repository(session, sources)
    if repo is None:
        raise OctodotError(
            ErrorCode.BINDING_MISMATCH,
            f"Session '{session.name}' has no repository binding (repoless session)",
        )

    if required_repository is not None:
        req_owner, req_name = validate_repository_name(required_repository)
        canonical_req = f"{req_owner}/{req_name}"
        if repo != canonical_req:
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Session repository '{repo}' does not match required repository '{canonical_req}'",
            )

    source_name = extract_session_source(session)
    if source_name is None:
        if sources is not None:
            matched_src = resolve_source(sources, repo)
            source_name = matched_src.name
        else:
            raise OctodotError(
                ErrorCode.BINDING_MISMATCH,
                f"Session '{session.name}' has no source binding",
            )

    observed_branch = extract_session_branch(session)
    if required_branch is not None:
        verified_branch = verify_branch_selection(required_branch, observed_branch)
    else:
        verified_branch = observed_branch

    return Binding(
        profile=profile,
        profile_epoch=profile_epoch,
        source=source_name,
        repository=repo,
        starting_branch=verified_branch,
        session=session.name,
    )


def compare_bindings(
    session_binding: Binding,
    target_binding: Binding,
) -> tuple[bool, ErrorCode | None, str | None]:
    """Pure comparison of session binding against target binding.

    Returns (matches: bool, error_code: ErrorCode | None, reason: str | None).
    """
    # 1. Repository comparison (exact case-sensitive)
    if session_binding.repository != target_binding.repository:
        return (
            False,
            ErrorCode.BINDING_MISMATCH,
            f"Repository mismatch: '{session_binding.repository}' != '{target_binding.repository}'",
        )

    # 2. Starting branch comparison
    if target_binding.starting_branch is not None:
        if session_binding.starting_branch is None:
            return (
                False,
                ErrorCode.BRANCH_UNVERIFIED,
                "Session branch is absent / unverified",
            )
        if session_binding.starting_branch != target_binding.starting_branch:
            return (
                False,
                ErrorCode.BINDING_MISMATCH,
                f"Starting branch mismatch: '{session_binding.starting_branch}' != '{target_binding.starting_branch}'",
            )

    # 3. Source comparison
    if target_binding.source and session_binding.source != target_binding.source:
        return (
            False,
            ErrorCode.BINDING_MISMATCH,
            f"Source mismatch: '{session_binding.source}' != '{target_binding.source}'",
        )

    return (True, None, None)
