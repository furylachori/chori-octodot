"""Tests for S05 preparation, hash determinism, and read completeness.

Test ID: S05-T05.
Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Mapping

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.authorization import FakeGrantVerifier
from octodot.contracts import (
    canonical_hash,
    compute_plan_hash,
    context_hash,
    request_hash,
)
from octodot.errors import ErrorCode, OctodotError
from octodot.models import (
    Binding,
    CandidateBundle,
    Coverage,
    Observation,
    PreparedAction,
    VerifiedGrant,
)
from octodot.preparation import (
    compute_mutation_request_body,
    compute_mutation_request_hash,
    extract_material_context,
    prepare_action,
    prepare_plan_actions,
    validate_only,
)


class SpyCredentialSource:
    """Credential source spy that records every access attempt."""

    def __init__(self) -> None:
        self.accesses: list[str] = []

    def get_credential(self, profile: str) -> str | None:
        self.accesses.append(profile)
        return "synthetic-secret-token"

    def was_accessed(self) -> bool:
        return len(self.accesses) > 0

    def access_count(self) -> int:
        return len(self.accesses)


class FakeReadService:
    """In-memory fake implementing the frozen ReadService Protocol."""

    def __init__(
        self,
        chats_result: Any = None,
        inspect_result: Any = None,
        collect_result: Any = None,
    ) -> None:
        self.chats_result = chats_result
        self.inspect_result = inspect_result
        self.collect_result = collect_result

    def collect(
        self, scope: dict[str, Any], limits: dict[str, Any]
    ) -> tuple[Any, Coverage]:
        if self.collect_result is not None:
            return self.collect_result
        return ({}, Coverage(complete=True))

    def inspect(self, binding: Binding) -> Any:
        if self.inspect_result is not None:
            return self.inspect_result
        return {
            "session": binding.session,
            "state": "WAITING_ON_USER",
            "coverage": Coverage(complete=True),
        }

    def chats(self, selection: dict[str, Any]) -> Any:
        if self.chats_result is not None:
            return self.chats_result
        return {
            "messages": [{"id": "m1", "text": "Hello, world!"}],
            "candidate_bundle": CandidateBundle(
                messages=({"id": "m1", "text": "Hello, world!"},),
                has_ambiguity=False,
                last_message_text="Hello, world!",
                selected_activity_id="act-1",
            ),
            "coverage": Coverage(complete=True),
        }


def make_sample_reply_plan() -> dict[str, Any]:
    plan: dict[str, Any] = {
        "schema_version": "jules-controller.plan.v1",
        "plan_id": "plan-reply-test",
        "profile": "default",
        "execution": {"mode": "mutation"},
        "scope": {
            "repository": "OWNER/REPO",
            "branch": "feature/example",
            "sessions": ["sessions/EXAMPLE"],
        },
        "limits": {
            "deadline_seconds": 180,
            "request_timeout_seconds": 20,
            "max_http_requests": 120,
            "max_posts": 1,
            "max_pages": 100,
            "max_sessions": 200,
            "max_response_bytes": 8388608,
            "max_total_bytes": 33554432,
            "max_output_bytes": 65536,
        },
        "actions": [
            {
                "id": "act-reply-1",
                "op": "chats.reply",
                "enabled": True,
                "operation_id": "op-reply-1",
                "authorization_ref": "grant-reply-1",
                "target": "sessions/EXAMPLE",
                "payload": {"text": "Understood, proceeding."},
                "preconditions": {},
            }
        ],
        "output": {"format": "json"},
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


class TestS05T05PreparationAndHashDeterminism(unittest.TestCase):
    """S05-T05: Preparation hash changes only for material context and request changes; incomplete history cannot be prepared for dispatch."""

    def test_s05_t05_preparation_hashes_equal_execution_recomputed_hashes(self) -> None:
        """S05-T05: Preparation and execution produce identical canonical hashes."""
        plan = make_sample_reply_plan()
        action = plan["actions"][0]
        read_service = FakeReadService()

        # Phase 1: Preparation time
        prep = prepare_action(
            action=action,
            plan=plan,
            current_profile_epoch=1,
            read_service=read_service,
        )

        # Phase 2: Execution time recomputation
        recomputed_payload_hash = canonical_hash(action["payload"])
        observed_ctx = extract_material_context(action=action, read_service=read_service)
        recomputed_context_hash = context_hash(observed_ctx)
        recomputed_plan_hash = compute_plan_hash(plan)
        recomputed_request_hash = compute_mutation_request_hash(
            target=action["target"],
            op=action["op"],
            payload=action["payload"],
        )

        # Assert all hashes are strictly equal
        self.assertEqual(prep.payload_hash, recomputed_payload_hash)
        self.assertEqual(prep.context_hash, recomputed_context_hash)
        self.assertEqual(prep.plan_hash, recomputed_plan_hash)
        self.assertEqual(prep.request_hash, recomputed_request_hash)

    def test_s05_t05_hash_changes_for_material_context_changes(self) -> None:
        """S05-T05: Material context changes alter context_hash."""
        action = {"id": "act-1", "op": "chats.reply", "target": "sessions/EXAMPLE", "payload": {"text": "ok"}}
        plan = make_sample_reply_plan()

        ctx1 = {
            "op": "chats.reply",
            "messages": [{"id": "m1", "text": "Original message"}],
        }
        prep1 = prepare_action(action=action, plan=plan, current_profile_epoch=1, context=ctx1)

        ctx2 = {
            "op": "chats.reply",
            "messages": [{"id": "m1", "text": "Altered message text"}],
        }
        prep2 = prepare_action(action=action, plan=plan, current_profile_epoch=1, context=ctx2)

        self.assertNotEqual(prep1.context_hash, prep2.context_hash)

    def test_s05_t05_hash_unchanged_for_volatile_observation_metadata(self) -> None:
        """S05-T05: Volatile observation timestamps and scan IDs do not alter context_hash."""
        action = {"id": "act-1", "op": "chats.reply", "target": "sessions/EXAMPLE", "payload": {"text": "ok"}}
        plan = make_sample_reply_plan()

        ctx_run1 = {
            "op": "chats.reply",
            "messages": [{"id": "m1", "text": "Same message"}],
            "scanned_at": "2026-10-07T10:00:00Z",
            "scan_id": "scan-111",
            "timestamp": "2026-10-07T10:00:00Z",
            "observed_at": "2026-10-07T10:00:01Z",
        }
        prep1 = prepare_action(action=action, plan=plan, current_profile_epoch=1, context=ctx_run1)

        ctx_run2 = {
            "op": "chats.reply",
            "messages": [{"id": "m1", "text": "Same message"}],
            "scanned_at": "2026-10-07T15:30:00Z",
            "scan_id": "scan-999",
            "timestamp": "2026-10-07T15:30:00Z",
            "observed_at": "2026-10-07T15:30:05Z",
        }
        prep2 = prepare_action(action=action, plan=plan, current_profile_epoch=1, context=ctx_run2)

        # Invariant: volatile metadata exclusion preserves identical context hash
        self.assertEqual(prep1.context_hash, prep2.context_hash)

    def test_s05_t05_hash_changes_for_request_payload_changes(self) -> None:
        """S05-T05: Changing action payload alters payload_hash and request_hash."""
        plan = make_sample_reply_plan()
        action1 = {"id": "act-1", "op": "chats.reply", "target": "sessions/EXAMPLE", "payload": {"text": "Version 1"}}
        action2 = {"id": "act-1", "op": "chats.reply", "target": "sessions/EXAMPLE", "payload": {"text": "Version 2"}}

        prep1 = prepare_action(action=action1, plan=plan, current_profile_epoch=1)
        prep2 = prepare_action(action=action2, plan=plan, current_profile_epoch=1)

        self.assertNotEqual(prep1.payload_hash, prep2.payload_hash)
        self.assertNotEqual(prep1.request_hash, prep2.request_hash)

    def test_s05_t05_incomplete_history_coverage_cannot_be_prepared(self) -> None:
        """S05-T05: Incomplete history coverage blocks preparation with PARTIAL_COVERAGE."""
        plan = make_sample_reply_plan()
        action = plan["actions"][0]

        # Read service returns incomplete coverage (e.g. hit page limit)
        incomplete_read_service = FakeReadService(
            chats_result={
                "messages": [{"id": "m1", "text": "Partial message"}],
                "coverage": Coverage(complete=False, reasons=("max_pages_reached",)),
            }
        )

        with self.assertRaises(OctodotError) as ctx:
            prepare_action(
                action=action,
                plan=plan,
                current_profile_epoch=1,
                read_service=incomplete_read_service,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.PARTIAL_COVERAGE)
        self.assertIn("incomplete", str(ctx.exception).lower())

    def test_s05_t05_ambiguous_candidate_bundle_cannot_be_prepared(self) -> None:
        """S05-T05: Candidate bundle with chronological ambiguity blocks preparation with IDENTITY_AMBIGUOUS."""
        plan = make_sample_reply_plan()
        action = plan["actions"][0]

        ambiguous_read_service = FakeReadService(
            chats_result={
                "candidate_bundle": CandidateBundle(
                    messages=({"id": "m1"}, {"id": "m2"}),
                    has_ambiguity=True,
                    ambiguity_reasons=("tied_timestamps_detected", "multiple_unread_instructions"),
                ),
                "coverage": Coverage(complete=True),
            }
        )

        with self.assertRaises(OctodotError) as ctx:
            prepare_action(
                action=action,
                plan=plan,
                current_profile_epoch=1,
                read_service=ambiguous_read_service,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.IDENTITY_AMBIGUOUS)
        self.assertIn("ambiguity", str(ctx.exception).lower())

    def test_s05_t05_validate_only_never_touches_credential_source(self) -> None:
        """S05-T05: --validate-only path performs structural & grant checks without accessing CredentialSource."""
        plan = make_sample_reply_plan()
        action = plan["actions"][0]

        # Prepare matching grant
        prep = prepare_action(action=action, plan=plan, current_profile_epoch=1)
        grant = VerifiedGrant(
            action=prep.action,
            operation_id=prep.operation_id,
            profile=prep.binding.profile,
            profile_epoch=prep.binding.profile_epoch,
            source=prep.binding.source,
            repository=prep.binding.repository,
            branch=prep.binding.starting_branch or "",
            payload_hash=prep.payload_hash,
            context_hash=prep.context_hash,
            plan_hash=prep.plan_hash,
            publication_scope=prep.publication_scope,
            authorizing_source="offline_fixture",
            session=prep.binding.session,
            max_attempts=1,
        )

        fake_verifier = FakeGrantVerifier(grants={"grant-reply-1": grant})
        spy_credentials = SpyCredentialSource()

        # Run validate_only with credential source spy
        report = validate_only(
            plan=plan,
            current_profile_epoch=1,
            grant_verifier=fake_verifier,
            credential_source=spy_credentials,
        )

        # Invariant 1: Credential source was NEVER accessed
        self.assertFalse(spy_credentials.was_accessed())
        self.assertEqual(spy_credentials.access_count(), 0)

        # Invariant 2: Report correctly reflects validation status
        self.assertTrue(report.eligible)
        self.assertEqual(len(report.blockers), 0)
        self.assertEqual(len(report.verified_grants), 1)
        self.assertTrue(report.is_valid)

    def test_s05_t05_preparation_for_all_mutation_operations(self) -> None:
        """S05-T05: Preparation supports chats.reply, plans.approve, and tasks.create with proper request body mapping."""
        # 1. chats.reply
        reply_action = {
            "id": "act-r",
            "op": "chats.reply",
            "operation_id": "op-r",
            "target": "sessions/S1",
            "payload": {"text": "Reply text"},
        }
        body_r = compute_mutation_request_body("chats.reply", reply_action["payload"])
        self.assertEqual(body_r, {"prompt": "Reply text"})
        req_hash_r = compute_mutation_request_hash(reply_action["target"], "chats.reply", reply_action["payload"])
        self.assertEqual(req_hash_r, request_hash({"target": "sessions/S1", "body": {"prompt": "Reply text"}}))

        # 2. plans.approve
        approve_action = {
            "id": "act-a",
            "op": "plans.approve",
            "operation_id": "op-a",
            "target": "sessions/S1",
            "payload": {"plan_id": "plan-xyz"},
        }
        body_a = compute_mutation_request_body("plans.approve", approve_action["payload"])
        self.assertEqual(body_a, {})
        req_hash_a = compute_mutation_request_hash(approve_action["target"], "plans.approve", approve_action["payload"])
        self.assertEqual(req_hash_a, request_hash({"target": "sessions/S1", "body": {}}))

        # 3. tasks.create
        create_action = {
            "id": "act-c",
            "op": "tasks.create",
            "operation_id": "op-c",
            "target": "OWNER/REPO",
            "payload": {
                "title": "New Task",
                "prompt": "Fix bug",
                "requirePlanApproval": True,
            },
        }
        body_c = compute_mutation_request_body("tasks.create", create_action["payload"])
        self.assertEqual(body_c, create_action["payload"])
        req_hash_c = compute_mutation_request_hash(create_action["target"], "tasks.create", create_action["payload"])
        self.assertEqual(req_hash_c, request_hash({"target": "OWNER/REPO", "body": create_action["payload"]}))


if __name__ == "__main__":
    unittest.main()
