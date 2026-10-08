"""Identity and repository/branch binding test suite (S04-T01, S04-T02).

Standard library only. Compatible with Python 3.10+.
Tests pure deterministic binding, resolution, and verification functions.
"""

from __future__ import annotations

import os
import sys
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.errors import ErrorCode, OctodotError
from octodot.identity import (
    bind_session,
    compare_bindings,
    extract_session_branch,
    extract_session_repository,
    extract_session_source,
    resolve_source,
    resolve_source_name,
    validate_repository_name,
    verify_branch_selection,
)
from octodot.models import Binding, SessionRecord, SourceRecord


class TestS04T01IdentityResolution(unittest.TestCase):
    """S04-T01: Identity resolution, malformed types, ambiguous matches, repoless sessions, branch differences."""

    def test_s04_t01_wrong_owner_or_source_fails_with_binding_mismatch(self) -> None:
        """S04-T01: Resolving a repository with wrong owner or wrong repo raises BINDING_MISMATCH."""
        sources = [
            SourceRecord(name="sources/1", github_repo_owner="WRONG", github_repo_name="REPO"),
            SourceRecord(name="sources/2", github_repo_owner="OWNER", github_repo_name="OTHER"),
        ]

        with self.assertRaises(OctodotError) as ctx:
            resolve_source(sources, "OWNER/REPO")
        self.assertEqual(ctx.exception.code, ErrorCode.BINDING_MISMATCH)

        # Empty sources list also raises BINDING_MISMATCH
        with self.assertRaises(OctodotError) as ctx_empty:
            resolve_source([], "OWNER/REPO")
        self.assertEqual(ctx_empty.exception.code, ErrorCode.BINDING_MISMATCH)

    def test_s04_t01_malformed_owner_repo_types_in_source_record_not_matched(self) -> None:
        """S04-T01: Sources with malformed owner/repo types (int, None, dict) do not match."""
        sources = [
            SourceRecord(name="sources/bad-1", github_repo_owner=123, github_repo_name="REPO"),  # type: ignore[arg-type]
            SourceRecord(name="sources/bad-2", github_repo_owner="OWNER", github_repo_name=None),
            SourceRecord(name="sources/bad-3", github_repo_owner={"owner": "OWNER"}, github_repo_name="REPO"),  # type: ignore[arg-type]
        ]

        with self.assertRaises(OctodotError) as ctx:
            resolve_source(sources, "OWNER/REPO")
        self.assertEqual(ctx.exception.code, ErrorCode.BINDING_MISMATCH)

    def test_s04_t01_malformed_repository_input_rejected_as_invalid_input(self) -> None:
        """S04-T01: Malformed repository argument (non-string, invalid types) raises INVALID_INPUT."""
        for bad_repo in (123, None, ["OWNER/REPO"], {"repo": "OWNER/REPO"}):
            with self.subTest(bad_repo=bad_repo):
                with self.assertRaises(OctodotError) as ctx:
                    validate_repository_name(bad_repo)  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s04_t01_slash_source_names_and_invalid_slash_syntax_rejected(self) -> None:
        """S04-T01: Slash syntax errors (double slashes, trailing/leading slashes, extra segments) rejected."""
        invalid_repos = [
            "OWNER//REPO",
            "/OWNER/REPO",
            "OWNER/REPO/",
            "OWNER/REPO/EXTRA",
            "NO_SLASH_REPO",
            "OWNER/ REPO",
            "OWNER /REPO",
            "",
            "   ",
        ]
        for bad_syntax in invalid_repos:
            with self.subTest(bad_syntax=bad_syntax):
                with self.assertRaises(OctodotError) as ctx:
                    validate_repository_name(bad_syntax)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

                with self.assertRaises(OctodotError) as ctx_res:
                    resolve_source([], bad_syntax)
                self.assertEqual(ctx_res.exception.code, ErrorCode.INVALID_INPUT)

    def test_s04_t01_never_construct_source_name_from_text(self) -> None:
        """S04-T01: Source name is resolved exclusively from API-returned SourceRecord, never fabricated."""
        sources = [
            SourceRecord(name="sources/opaque-remote-id-987", github_repo_owner="OWNER", github_repo_name="REPO"),
        ]
        resolved = resolve_source(sources, "OWNER/REPO")
        self.assertEqual(resolved.name, "sources/opaque-remote-id-987")
        self.assertNotEqual(resolved.name, "sources/github/OWNER/REPO")

        name = resolve_source_name(sources, "OWNER/REPO")
        self.assertEqual(name, "sources/opaque-remote-id-987")

    def test_s04_t01_ambiguous_matches_raise_identity_ambiguous(self) -> None:
        """S04-T01: Multiple sources with identical owner/repo raise IDENTITY_AMBIGUOUS."""
        sources = [
            SourceRecord(name="sources/dup-1", github_repo_owner="OWNER", github_repo_name="REPO"),
            SourceRecord(name="sources/dup-2", github_repo_owner="OWNER", github_repo_name="REPO"),
        ]
        with self.assertRaises(OctodotError) as ctx:
            resolve_source(sources, "OWNER/REPO")
        self.assertEqual(ctx.exception.code, ErrorCode.IDENTITY_AMBIGUOUS)

    def test_s04_t01_repoless_sessions_fail_binding_with_binding_mismatch(self) -> None:
        """S04-T01: Repoless session (no source_context or missing repo fields) raises BINDING_MISMATCH."""
        repoless_session = SessionRecord(
            name="sessions/REPOLESS-001",
            state="ACTIVE",
            source_context=(),
        )
        repo = extract_session_repository(repoless_session, sources=[])
        self.assertIsNone(repo)

        with self.assertRaises(OctodotError) as ctx:
            bind_session(
                profile="default",
                profile_epoch=1,
                session=repoless_session,
                sources=[],
            )
        self.assertEqual(ctx.exception.code, ErrorCode.BINDING_MISMATCH)

    def test_s04_t01_exact_branch_case_differences_rejected(self) -> None:
        """S04-T01: Branch comparison is strictly case-sensitive; case differences fail with BINDING_MISMATCH."""
        pairs = [
            ("main", "Main"),
            ("Main", "main"),
            ("FEATURE/EXAMPLE", "feature/example"),
            ("feature/example", "FEATURE/EXAMPLE"),
            ("master", "MASTER"),
        ]
        for deliberate, observed in pairs:
            with self.subTest(deliberate=deliberate, observed=observed):
                with self.assertRaises(OctodotError) as ctx:
                    verify_branch_selection(deliberate, observed)
                self.assertEqual(ctx.exception.code, ErrorCode.BINDING_MISMATCH)

    def test_s04_t01_exact_branch_slash_differences_rejected(self) -> None:
        """S04-T01: Branch comparison rejects slash differences (extra slashes, leading/trailing)."""
        pairs = [
            ("feature/foo", "feature//foo"),
            ("feature/foo", "/feature/foo"),
            ("feature/foo", "feature/foo/"),
            ("feature/foo/bar", "feature/foo//bar"),
        ]
        for deliberate, observed in pairs:
            with self.subTest(deliberate=deliberate, observed=observed):
                with self.assertRaises(OctodotError) as ctx:
                    verify_branch_selection(deliberate, observed)
                self.assertEqual(ctx.exception.code, ErrorCode.BINDING_MISMATCH)

    def test_s04_t01_exact_branch_match_succeeds(self) -> None:
        """S04-T01: Exact case-sensitive branch match succeeds."""
        branches = ["main", "master", "feature/example", "fix/bug-123", "v1.0.0-release"]
        for b in branches:
            with self.subTest(b=b):
                verified = verify_branch_selection(b, b)
                self.assertEqual(verified, b)


class TestS04T02BranchSelectionAndNoDefaulting(unittest.TestCase):
    """S04-T02: Explicit main or master allowed only when deliberately selected; no default-branch substitution."""

    def test_s04_t02_absent_branch_never_substituted_with_main_or_master(self) -> None:
        """S04-T02: Absent branch raises BRANCH_UNVERIFIED; never defaults to main or master."""
        # Absent observed branch with no deliberate branch specified
        with self.assertRaises(OctodotError) as ctx_none:
            verify_branch_selection(None, None)
        self.assertEqual(ctx_none.exception.code, ErrorCode.BRANCH_UNVERIFIED)

        # Absent observed branch with empty string
        with self.assertRaises(OctodotError) as ctx_empty:
            verify_branch_selection("main", "")
        self.assertEqual(ctx_empty.exception.code, ErrorCode.BRANCH_UNVERIFIED)

        # Even when deliberate branch is 'main', absent observed branch cannot be substituted
        with self.assertRaises(OctodotError) as ctx_main:
            verify_branch_selection("main", None)
        self.assertEqual(ctx_main.exception.code, ErrorCode.BRANCH_UNVERIFIED)

        # Even when deliberate branch is 'master', absent observed branch cannot be substituted
        with self.assertRaises(OctodotError) as ctx_master:
            verify_branch_selection("master", None)
        self.assertEqual(ctx_master.exception.code, ErrorCode.BRANCH_UNVERIFIED)

    def test_s04_t02_explicit_main_or_master_allowed_only_when_deliberately_selected(self) -> None:
        """S04-T02: Explicit main or master is valid when deliberately selected and matches observed."""
        # Deliberately selected 'main' matching observed 'main'
        res_main = verify_branch_selection("main", "main")
        self.assertEqual(res_main, "main")

        # Deliberately selected 'master' matching observed 'master'
        res_master = verify_branch_selection("master", "master")
        self.assertEqual(res_master, "master")

    def test_s04_t02_bind_session_with_absent_branch_enforces_branch_unverified(self) -> None:
        """S04-T02: bind_session with required_branch on session missing branch raises BRANCH_UNVERIFIED."""
        session = SessionRecord(
            name="sessions/EX-001",
            state="ACTIVE",
            source_context=(
                ("source", "sources/src-1"),
                ("githubRepo", {"owner": "OWNER", "repo": "REPO"}),
                # No githubRepoContext or startingBranch
            ),
        )

        # Requiring 'main' on session with absent branch must fail with BRANCH_UNVERIFIED
        with self.assertRaises(OctodotError) as ctx:
            bind_session(
                profile="default",
                profile_epoch=1,
                session=session,
                required_branch="main",
            )
        self.assertEqual(ctx.exception.code, ErrorCode.BRANCH_UNVERIFIED)

        # Requiring 'master' on session with absent branch must fail with BRANCH_UNVERIFIED
        with self.assertRaises(OctodotError) as ctx_master:
            bind_session(
                profile="default",
                profile_epoch=1,
                session=session,
                required_branch="master",
            )
        self.assertEqual(ctx_master.exception.code, ErrorCode.BRANCH_UNVERIFIED)

    def test_s04_t02_bind_session_with_matching_explicit_branch_succeeds(self) -> None:
        """S04-T02: bind_session succeeds when deliberate required_branch matches observed session branch."""
        session_main = SessionRecord(
            name="sessions/EX-002",
            state="ACTIVE",
            source_context=(
                ("source", "sources/src-1"),
                ("githubRepo", {"owner": "OWNER", "repo": "REPO"}),
                ("githubRepoContext", {"startingBranch": "main"}),
            ),
        )

        binding = bind_session(
            profile="prod-profile",
            profile_epoch=42,
            session=session_main,
            required_repository="OWNER/REPO",
            required_branch="main",
        )
        self.assertEqual(binding.profile, "prod-profile")
        self.assertEqual(binding.profile_epoch, 42)
        self.assertEqual(binding.source, "sources/src-1")
        self.assertEqual(binding.repository, "OWNER/REPO")
        self.assertEqual(binding.starting_branch, "main")
        self.assertEqual(binding.session, "sessions/EX-002")

    def test_s04_t02_compare_bindings_flags_branch_unverified_and_mismatch(self) -> None:
        """S04-T02: compare_bindings returns BRANCH_UNVERIFIED for absent branch and BINDING_MISMATCH for difference."""
        target_binding = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/src-1",
            repository="OWNER/REPO",
            starting_branch="main",
            session="sessions/TARGET",
        )

        # Session binding with absent branch
        sess_binding_no_branch = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/src-1",
            repository="OWNER/REPO",
            starting_branch=None,
            session="sessions/OBSERVED",
        )
        ok, err, reason = compare_bindings(sess_binding_no_branch, target_binding)
        self.assertFalse(ok)
        self.assertEqual(err, ErrorCode.BRANCH_UNVERIFIED)

        # Session binding with mismatched branch case
        sess_binding_wrong_case = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/src-1",
            repository="OWNER/REPO",
            starting_branch="Main",
            session="sessions/OBSERVED",
        )
        ok2, err2, reason2 = compare_bindings(sess_binding_wrong_case, target_binding)
        self.assertFalse(ok2)
        self.assertEqual(err2, ErrorCode.BINDING_MISMATCH)

        # Exact matching binding
        sess_binding_exact = Binding(
            profile="default",
            profile_epoch=1,
            source="sources/src-1",
            repository="OWNER/REPO",
            starting_branch="main",
            session="sessions/OBSERVED",
        )
        ok3, err3, reason3 = compare_bindings(sess_binding_exact, target_binding)
        self.assertTrue(ok3)
        self.assertIsNone(err3)
        self.assertIsNone(reason3)
