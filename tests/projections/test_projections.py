"""Projections test suite for lifecycle, candidate bundle, failure, and plan (S04-T03 to S04-T06).

Standard library only. Compatible with Python 3.10+.
Tests pure deterministic projections and golden fixtures.
"""

from __future__ import annotations

import os
import sys
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.contracts import canonical_hash
from octodot.errors import ErrorCode
from octodot.models import (
    ActivityRecord,
    Coverage,
    LifecycleBucket,
    SessionRecord,
)
from octodot.projections import (
    ActivityKind,
    AttentionReason,
    FailureKind,
    parse_rfc3339_nanoseconds,
    project_activity,
    project_attention,
    project_candidate_bundle,
    project_failure,
    project_lifecycle,
    project_plan,
    project_session,
)


class TestS04T03LifecycleAndAttentionProjections(unittest.TestCase):
    """S04-T03: Unknown states visible & block writes. Exclusive buckets. Overlapping attention."""

    def test_s04_t03_exclusive_lifecycle_buckets(self) -> None:
        """S04-T03: Exclusive buckets: open/completed/failed/unknown. Terminal = completed + failed."""
        open_cases = ["ACTIVE", "PLANNING", "AWAITING_USER_FEEDBACK", "IN_PROGRESS", "PAUSED", "QUEUED", "PENDING", "RUNNING"]
        for st in open_cases:
            with self.subTest(st=st):
                proj = project_lifecycle(st)
                self.assertEqual(proj.bucket, LifecycleBucket.OPEN)
                self.assertFalse(proj.is_terminal)
                self.assertFalse(proj.blocks_writes)
                self.assertEqual(proj.raw_state, st)

        completed_cases = ["COMPLETED", "SUCCEEDED", "SUCCESS"]
        for st in completed_cases:
            with self.subTest(st=st):
                proj = project_lifecycle(st)
                self.assertEqual(proj.bucket, LifecycleBucket.COMPLETED)
                self.assertTrue(proj.is_terminal)
                self.assertFalse(proj.blocks_writes)
                self.assertEqual(proj.raw_state, st)

        failed_cases = ["FAILED", "ERROR", "CANCELLED"]
        for st in failed_cases:
            with self.subTest(st=st):
                proj = project_lifecycle(st)
                self.assertEqual(proj.bucket, LifecycleBucket.FAILED)
                self.assertTrue(proj.is_terminal)
                self.assertFalse(proj.blocks_writes)
                self.assertEqual(proj.raw_state, st)

    def test_s04_t03_unknown_states_preserved_verbatim_and_block_writes(self) -> None:
        """S04-T03: Unknown states remain visible verbatim and block state-dependent writes."""
        unknown_cases = [
            "MYSTERY_FUTURE_STATE",
            "STATE_UNSPECIFIED",
            "CUSTOM_WORKFLOW_STATE",
            "UNEXPECTED",
            "",
            "UNKNOWN",
        ]
        for st in unknown_cases:
            with self.subTest(st=st):
                proj = project_lifecycle(st)
                self.assertEqual(proj.bucket, LifecycleBucket.UNKNOWN)
                self.assertFalse(proj.is_terminal)
                self.assertTrue(proj.blocks_writes, f"State '{st}' must block state-dependent writes")
                self.assertEqual(proj.raw_state, st)

    def test_s04_t03_lifecycle_projection_from_session_record(self) -> None:
        """S04-T03: project_lifecycle works transparently with SessionRecord."""
        sess = SessionRecord(name="sessions/EX-101", state="FUTURE_STATE")
        proj = project_lifecycle(sess)
        self.assertEqual(proj.bucket, LifecycleBucket.UNKNOWN)
        self.assertTrue(proj.blocks_writes)
        self.assertEqual(proj.raw_state, "FUTURE_STATE")

    def test_s04_t03_attention_subsets_may_overlap(self) -> None:
        """S04-T03: Attention reasons may freely overlap (e.g. needs_reply AND needs_plan_approval)."""
        # Session in PLANNING that also has candidate messages and an unapproved plan
        lifecycle = project_lifecycle("PLANNING")
        plan = project_plan(
            activities=[
                {
                    "id": "act-p1",
                    "type": "PLAN_GENERATED",
                    "planId": "plan-1",
                    "plan": {"title": "Test Plan"},
                }
            ],
            session={"requirePlanApproval": True},
        )
        candidate = project_candidate_bundle(
            activities=[
                {
                    "id": "act-m1",
                    "type": "AGENT_MESSAGE",
                    "text": "Please confirm your intent.",
                    "createTime": "2026-10-07T10:00:00Z",
                }
            ]
        )

        attention = project_attention(
            lifecycle=lifecycle,
            candidate_bundle=candidate,
            plan=plan,
        )

        self.assertTrue(attention.needs_attention)
        # Check overlapping reasons
        self.assertTrue(attention.has_reason(AttentionReason.NEEDS_REPLY))
        self.assertTrue(attention.has_reason(AttentionReason.NEEDS_PLAN_APPROVAL))
        self.assertIn(AttentionReason.NEEDS_REPLY, attention.reasons)
        self.assertIn(AttentionReason.NEEDS_PLAN_APPROVAL, attention.reasons)

        # Another overlap: FAILED session with ambiguous candidate bundle
        failed_lifecycle = project_lifecycle("FAILED")
        ambiguous_candidate = project_candidate_bundle(
            coverage=Coverage(complete=False)
        )
        failed_attention = project_attention(
            lifecycle=failed_lifecycle,
            candidate_bundle=ambiguous_candidate,
        )
        self.assertTrue(failed_attention.has_reason(AttentionReason.FAILED))
        self.assertTrue(failed_attention.has_reason(AttentionReason.AMBIGUOUS_CHRONOLOGY))


class TestS04T04CandidateBundleAndConversationProjection(unittest.TestCase):
    """S04-T04: Multi-message feedback, no question-mark heuristics, tied ns timestamps, manual reply, drift."""

    def test_s04_t04_rfc3339_nanosecond_parsing_exact(self) -> None:
        """S04-T04: RFC3339 timestamps are parsed into exact integer nanoseconds without float loss."""
        # 1 ns difference
        ns1 = parse_rfc3339_nanoseconds("2026-10-07T12:00:00.123456789Z")
        ns2 = parse_rfc3339_nanoseconds("2026-10-07T12:00:00.123456788Z")
        self.assertEqual(ns1 - ns2, 1)

        # Timezone offsets
        utc = parse_rfc3339_nanoseconds("2026-10-07T12:00:00Z")
        plus2 = parse_rfc3339_nanoseconds("2026-10-07T14:00:00+02:00")
        minus7 = parse_rfc3339_nanoseconds("2026-10-07T05:00:00-07:00")
        self.assertEqual(utc, plus2)
        self.assertEqual(utc, minus7)

    def test_s04_t04_multi_message_feedback_preserved_in_chronological_order(self) -> None:
        """S04-T04: Multi-message feedback: all agent messages since last user message are preserved."""
        activities = [
            {
                "id": "act-1",
                "type": "USER_MESSAGE",
                "text": "Start task",
                "createTime": "2026-10-07T10:00:00Z",
            },
            {
                "id": "act-2",
                "type": "AGENT_MESSAGE",
                "text": "I examined the repository.",
                "createTime": "2026-10-07T10:01:00Z",
            },
            {
                "id": "act-3",
                "type": "AGENT_MESSAGE",
                "text": "I located three candidate files.",
                "createTime": "2026-10-07T10:02:00Z",
            },
            {
                "id": "act-4",
                "type": "AGENT_MESSAGE",
                "text": "Which approach do you prefer?",
                "createTime": "2026-10-07T10:03:00Z",
            },
        ]
        bundle = project_candidate_bundle(activities=activities)

        self.assertFalse(bundle.has_ambiguity)
        self.assertEqual(len(bundle.messages), 3)
        self.assertEqual(bundle.messages[0]["text"], "I examined the repository.")
        self.assertEqual(bundle.messages[1]["text"], "I located three candidate files.")
        self.assertEqual(bundle.messages[2]["text"], "Which approach do you prefer?")
        self.assertEqual(bundle.last_message_text, "Which approach do you prefer?")
        self.assertEqual(bundle.selected_activity_id, "act-4")

    def test_s04_t04_messages_without_question_marks_are_included(self) -> None:
        """S04-T04: Agent messages without question marks are preserved without heuristics."""
        activities = [
            {
                "id": "act-1",
                "type": "USER_MESSAGE",
                "text": "Do work",
                "createTime": "2026-10-07T10:00:00Z",
            },
            {
                "id": "act-2",
                "type": "AGENT_MESSAGE",
                "text": "Please acknowledge this step before I proceed.",
                "createTime": "2026-10-07T10:01:00Z",
            },
        ]
        bundle = project_candidate_bundle(activities=activities)

        self.assertFalse(bundle.has_ambiguity)
        self.assertEqual(len(bundle.messages), 1)
        self.assertEqual(bundle.messages[0]["text"], "Please acknowledge this step before I proceed.")
        self.assertNotIn("?", bundle.last_message_text)

    def test_s04_t04_tied_nanosecond_timestamps_flag_ambiguity(self) -> None:
        """S04-T04: Tied nanosecond timestamps flag tied_timestamps ambiguity."""
        activities = [
            {
                "id": "act-1",
                "type": "AGENT_MESSAGE",
                "text": "Message A",
                "createTime": "2026-10-07T10:00:00.500000000Z",
            },
            {
                "id": "act-2",
                "type": "AGENT_MESSAGE",
                "text": "Message B",
                "createTime": "2026-10-07T10:00:00.500000000Z",  # Tied timestamp!
            },
        ]
        bundle = project_candidate_bundle(activities=activities)

        self.assertTrue(bundle.has_ambiguity)
        self.assertIn("tied_timestamps", bundle.ambiguity_reasons)

    def test_s04_t04_manual_reply_detected_when_user_replied_last(self) -> None:
        """S04-T04: When user replied after agent message, manual reply is detected and candidate empty."""
        activities = [
            {
                "id": "act-1",
                "type": "USER_MESSAGE",
                "text": "Start task",
                "createTime": "2026-10-07T10:00:00Z",
            },
            {
                "id": "act-2",
                "type": "AGENT_MESSAGE",
                "text": "Is this correct?",
                "createTime": "2026-10-07T10:01:00Z",
            },
            {
                "id": "act-3",
                "type": "USER_MESSAGE",
                "text": "Yes, manual reply from web UI.",
                "createTime": "2026-10-07T10:02:00Z",
            },
        ]
        bundle = project_candidate_bundle(activities=activities)

        self.assertTrue(bundle.has_ambiguity)
        self.assertIn("manual_reply_detected", bundle.ambiguity_reasons)
        self.assertEqual(len(bundle.messages), 0)
        self.assertIsNone(bundle.last_message_text)

    def test_s04_t04_incomplete_history_flags_ambiguity(self) -> None:
        """S04-T04: Incomplete query coverage flags incomplete_history ambiguity."""
        bundle = project_candidate_bundle(
            activities=[],
            coverage=Coverage(complete=False, reasons=("max_pages_reached",)),
        )
        self.assertTrue(bundle.has_ambiguity)
        self.assertIn("incomplete_history", bundle.ambiguity_reasons)

    def test_s04_t04_before_after_session_drift_flags_ambiguity(self) -> None:
        """S04-T04: Session state or update_time drift between observation points flags session_drift_detected."""
        prior = SessionRecord(
            name="sessions/EX-001",
            state="AWAITING_USER_FEEDBACK",
            update_time="2026-10-07T10:00:00Z",
        )
        current = SessionRecord(
            name="sessions/EX-001",
            state="ACTIVE",
            update_time="2026-10-07T10:05:00Z",
        )
        bundle = project_candidate_bundle(
            activities=[],
            prior_session=prior,
            current_session=current,
        )
        self.assertTrue(bundle.has_ambiguity)
        self.assertIn("session_drift_detected", bundle.ambiguity_reasons)


class TestS04T05FailureProjectionDistinctions(unittest.TestCase):
    """S04-T05: Distinct failure projection: current, historical, nonzero expected test, transport error, stall."""

    def test_s04_t05_current_failure_distinct(self) -> None:
        """S04-T05: Current failure state produces CURRENT_FAILURE and is_failed=True."""
        session = SessionRecord(name="sessions/EX-F1", state="FAILED")
        proj = project_failure(session=session)

        self.assertEqual(proj.kind, FailureKind.CURRENT_FAILURE)
        self.assertTrue(proj.is_failed)

    def test_s04_t05_historical_failure_after_recovery_distinct(self) -> None:
        """S04-T05: Earlier failed command recovered in active session produces HISTORICAL_FAILURE."""
        session = SessionRecord(name="sessions/EX-REC", state="ACTIVE")
        activities = [
            {
                "id": "act-1",
                "type": "COMMAND_EXECUTION",
                "command": "git checkout -b feature",
                "exitCode": 1,
                "status": "FAILED",
                "createTime": "2026-10-07T10:00:00Z",
            },
            {
                "id": "act-2",
                "type": "COMMAND_EXECUTION",
                "command": "git checkout feature",
                "exitCode": 0,
                "status": "COMPLETED",
                "createTime": "2026-10-07T10:01:00Z",
            },
        ]
        proj = project_failure(session=session, activities=activities)

        self.assertEqual(proj.kind, FailureKind.HISTORICAL_FAILURE)
        self.assertFalse(proj.is_failed)

    def test_s04_t05_nonzero_expected_test_command_distinct(self) -> None:
        """S04-T05: Nonzero exit code from expected test command produces NONZERO_EXPECTED_TEST_COMMAND."""
        session = SessionRecord(name="sessions/EX-TEST", state="ACTIVE")
        activities = [
            {
                "id": "act-test",
                "type": "COMMAND_EXECUTION",
                "command": "pytest tests/failing_test.py",
                "exitCode": 1,
                "expectedFailure": True,
                "createTime": "2026-10-07T10:00:00Z",
            }
        ]
        proj = project_failure(session=session, activities=activities)

        self.assertEqual(proj.kind, FailureKind.NONZERO_EXPECTED_TEST_COMMAND)
        self.assertFalse(proj.is_failed)

    def test_s04_t05_transport_error_distinct(self) -> None:
        """S04-T05: Transport-level error produces TRANSPORT_ERROR and is_failed=True."""
        proj = project_failure(transport_error=ErrorCode.TIMEOUT)

        self.assertEqual(proj.kind, FailureKind.TRANSPORT_ERROR)
        self.assertTrue(proj.is_failed)

    def test_s04_t05_suspected_stall_distinct(self) -> None:
        """S04-T05: Inactive open session produces SUSPECTED_STALL and is_failed=False."""
        session = SessionRecord(name="sessions/EX-STALL", state="ACTIVE")
        proj = project_failure(session=session, is_stalled=True)

        self.assertEqual(proj.kind, FailureKind.SUSPECTED_STALL)
        self.assertFalse(proj.is_failed)

    def test_s04_t05_all_five_kinds_are_mutually_distinct(self) -> None:
        """S04-T05: All failure kinds are mutually distinct enum members."""
        kinds = {
            FailureKind.NONE,
            FailureKind.CURRENT_FAILURE,
            FailureKind.HISTORICAL_FAILURE,
            FailureKind.NONZERO_EXPECTED_TEST_COMMAND,
            FailureKind.TRANSPORT_ERROR,
            FailureKind.SUSPECTED_STALL,
        }
        self.assertEqual(len(kinds), 6)


class TestS04T06PlanProjectionAndUnknownActivities(unittest.TestCase):
    """S04-T06: Exact plan content hash (contracts.canonical_hash); unknown activity types represented without guessing."""

    def test_s04_t06_plan_content_hash_retains_exact_approved_content(self) -> None:
        """S04-T06: Plan projection retains exact canonical hash matching contracts.canonical_hash."""
        plan_content = {
            "steps": [
                {"description": "Read codebase", "step_number": 1},
                {"description": "Run unit tests", "step_number": 2},
            ],
            "title": "Refactoring Plan",
        }
        expected_hash = canonical_hash(plan_content)

        activities = [
            {
                "id": "act-plan-1",
                "type": "PLAN_GENERATED",
                "planId": "plan-xyz-99",
                "plan": plan_content,
                "createTime": "2026-10-07T10:00:00Z",
            },
            {
                "id": "act-app-1",
                "type": "PLAN_APPROVED",
                "planId": "plan-xyz-99",
                "createTime": "2026-10-07T10:05:00Z",
            },
        ]
        session = SessionRecord(name="sessions/EX-PLAN", state="IN_PROGRESS", require_plan_approval=True)

        proj = project_plan(activities=activities, session=session)

        self.assertEqual(proj.latest_plan_id, "plan-xyz-99")
        self.assertEqual(proj.latest_plan_hash, expected_hash)
        self.assertTrue(proj.is_approved)
        self.assertTrue(proj.requires_approval)

    def test_s04_t06_unknown_activity_types_represented_without_inventing_semantics(self) -> None:
        """S04-T06: Unknown activity types represented as ActivityKind.UNKNOWN with verbatim type string."""
        raw_unknown_activity = {
            "id": "act-mystery-1",
            "type": "UNANNOUNCED_FUTURE_GOOGLE_ACTIVITY_TYPE",
            "createTime": "2026-10-07T10:00:00Z",
            "customPayload": {"foo": "bar"},
        }
        proj = project_activity(raw_unknown_activity)

        self.assertEqual(proj.kind, ActivityKind.UNKNOWN)
        self.assertTrue(proj.is_unknown_type)
        self.assertEqual(proj.raw_type, "UNANNOUNCED_FUTURE_GOOGLE_ACTIVITY_TYPE")
        self.assertEqual(proj.activity_id, "act-mystery-1")

    def test_s04_t06_comprehensive_session_projection(self) -> None:
        """S04-T06: project_session ties together lifecycle, attention, candidate, failure, and plan."""
        session = SessionRecord(
            name="sessions/FULL-001",
            state="AWAITING_USER_FEEDBACK",
            source_context=(
                ("source", "sources/src-10"),
                ("githubRepo", {"owner": "OWNER", "repo": "REPO"}),
                ("githubRepoContext", {"startingBranch": "feature/golden"}),
            ),
        )
        activities = [
            {
                "id": "act-1",
                "type": "AGENT_MESSAGE",
                "text": "How would you like to handle migration?",
                "createTime": "2026-10-07T11:00:00Z",
            }
        ]
        proj = project_session(session=session, activities=activities)

        self.assertEqual(proj.session_id, "sessions/FULL-001")
        self.assertEqual(proj.lifecycle.bucket, LifecycleBucket.OPEN)
        self.assertTrue(proj.attention.needs_attention)
        self.assertTrue(proj.attention.has_reason(AttentionReason.NEEDS_REPLY))
        self.assertEqual(len(proj.candidate_bundle.messages), 1)
        self.assertEqual(proj.disposition, "waiting_for_user_feedback")
        self.assertEqual(proj.delivery, "delivered")
        self.assertEqual(proj.publication, "none")
        self.assertIsNotNone(proj.binding)
        self.assertEqual(proj.binding.repository, "OWNER/REPO")
        self.assertEqual(proj.binding.starting_branch, "feature/golden")
