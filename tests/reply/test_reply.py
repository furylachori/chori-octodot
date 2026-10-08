"""Offline test suite for S10 exact approved replies.

Standard library only. Compatible with Python 3.10+.
Tests cover S10-T01 through S10-T05 with deterministic fault injection,
fake clocks, synthetic fixtures, and POST counts asserted via FixtureTransport.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.actions.reply import (
    ChatsReplyHandler,
    compute_candidate_bundle_hash,
    detect_secrets,
    detect_unauthorized_consequential,
)
from octodot.api import JulesClient, compute_mutation_request_hash
from octodot.authorization import (
    DisabledGrantVerifier,
    FakeGrantVerifier,
)
from octodot.contracts import (
    canonical_bytes,
    canonical_hash,
    context_hash,
)
from octodot.errors import (
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    ErrorCode,
)
from octodot.journal import Journal
from octodot.models import (
    ActionResultStatus,
    Binding,
    Coverage,
    OperationRecord,
    OperationState,
    PreparedAction,
    VerifiedGrant,
)
from octodot.preparation import prepare_action
from octodot.reads import ReadService
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport, TransportOutcome


def _make_sample_session_dict(
    name: str = "sessions/EXAMPLE",
    state: str = "ACTIVE",
    repo: str = "OWNER/REPO",
    branch: str = "main",
    source: str = "sources/github/OWNER/REPO",
    update_time: str = "2026-10-07T12:05:00Z",
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "title": "Sample Session",
        "createTime": "2026-10-07T12:00:00Z",
        "updateTime": update_time,
        "sourceContext": {
            "source": source,
            "repository": repo,
            "githubRepoContext": {
                "startingBranch": branch,
            },
        },
    }


def _make_sample_activities_dict(
    session_name: str = "sessions/EXAMPLE",
    multi_message: bool = False,
    tied_chronology: bool = False,
    user_reply_already: bool = False,
    identical_user_message: str | None = None,
) -> dict[str, Any]:
    acts = [
        {
            "name": f"{session_name}/activities/act-1",
            "id": "act-1",
            "type": "userMessage",
            "originator": "USER",
            "createTime": "2026-10-07T12:00:10.000000001Z",
            "text": "Please inspect the codebase.",
        },
        {
            "name": f"{session_name}/activities/act-2",
            "id": "act-2",
            "type": "agentMessage",
            "originator": "AGENT",
            "createTime": (
                "2026-10-07T12:00:10.000000001Z"
                if tied_chronology
                else "2026-10-07T12:01:00.000000002Z"
            ),
            "text": "Should I proceed with option A or option B?",
        },
    ]

    if multi_message:
        acts.append({
            "name": f"{session_name}/activities/act-3",
            "id": "act-3",
            "type": "agentMessage",
            "originator": "AGENT",
            "createTime": "2026-10-07T12:01:05.000000003Z",
            "text": "Please confirm before I begin.",
        })

    if identical_user_message is not None:
        acts.append({
            "name": f"{session_name}/activities/act-manual",
            "id": "act-manual",
            "type": "userMessage",
            "originator": "USER",
            "createTime": "2026-10-07T12:02:00.000000004Z",
            "text": identical_user_message,
        })
    elif user_reply_already:
        acts.append({
            "name": f"{session_name}/activities/act-4",
            "id": "act-4",
            "type": "userMessage",
            "originator": "USER",
            "createTime": "2026-10-07T12:02:00.000000004Z",
            "text": "I will handle this manually.",
        })

    return {"activities": acts}


def _make_sources_dict(repo: str = "OWNER/REPO") -> dict[str, Any]:
    parts = repo.split("/")
    owner = parts[0] if len(parts) > 1 else "OWNER"
    repo_name = parts[1] if len(parts) > 1 else repo
    return {
        "sources": [
            {
                "name": f"sources/github/{repo}",
                "id": "src-1",
                "githubRepo": {
                    "owner": owner,
                    "repo": repo_name,
                },
            }
        ]
    }


class ReplyBaseTestCase(unittest.TestCase):
    """Base test case providing in-memory SQLite store, fake clock, and fixture transport."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.fence = InMemoryRecoveryFence(epochs={"default": 1}, checkpoints={"default": 0})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.test_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_transport(
        self,
        session_dict: dict[str, Any] | None = None,
        activities_dict: dict[str, Any] | None = None,
        sources_dict: dict[str, Any] | None = None,
        send_outcome: TransportOutcome | None = None,
    ) -> FixtureTransport:
        sess = session_dict or _make_sample_session_dict()
        acts = activities_dict or _make_sample_activities_dict()
        srcs = sources_dict or _make_sources_dict()

        sess_name = sess["name"]
        clean_sess = sess_name.strip()
        if not clean_sess.startswith("sessions/"):
            clean_sess = f"sessions/{clean_sess}"

        responses: dict[tuple[str, str], TransportOutcome] = {
            ("GET", f"/v1alpha/{clean_sess}"): TransportOutcome(
                status=200, body=json.dumps(sess).encode("utf-8")
            ),
            ("GET", f"/v1alpha/{clean_sess}/activities"): TransportOutcome(
                status=200, body=json.dumps(acts).encode("utf-8")
            ),
            ("GET", "/v1alpha/sources"): TransportOutcome(
                status=200, body=json.dumps(srcs).encode("utf-8")
            ),
        }

        send_target = f"/v1alpha/{clean_sess}:sendMessage"
        send_resp = send_outcome or TransportOutcome(status=200, body=b"{}")
        responses[("POST", send_target)] = send_resp

        return FixtureTransport(responses=responses)

    def _make_context(
        self,
        transport: FixtureTransport,
        verifier: Any = None,
        profile: str = "default",
        profile_epoch: int = 1,
    ) -> dict[str, Any]:
        client = JulesClient(transport=transport, clock=self.clock)
        read_service = ReadService(api=client)
        actual_verifier = verifier if verifier is not None else DisabledGrantVerifier()
        journal = Journal(
            store=self.store,
            verifier=actual_verifier,
            fence=self.fence,
            clock=self.clock,
        )
        client.ticket_authority = journal

        return {
            "read_service": read_service,
            "client": client,
            "api": client,
            "transport": transport,
            "store": self.store,
            "journal": journal,
            "grant_verifier": actual_verifier,
            "verifier": actual_verifier,
            "fence": self.fence,
            "clock": self.clock,
            "profile": profile,
            "profile_epoch": profile_epoch,
        }

    def _make_verified_grant(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
        prompt_text: str,
        repo: str = "OWNER/REPO",
        branch: str = "main",
        session: str = "sessions/EXAMPLE",
        publication_scope: str = "none",
        plan_hash: str | None = None,
    ) -> VerifiedGrant:
        read_service = context["read_service"]
        chats_coll = read_service.chats({"session": session}, fresh=True)
        cb = chats_coll.candidate_bundle

        material_ctx = {
            "op": "chats.reply",
            "target": session,
            "messages": cb.messages,
            "last_message_text": cb.last_message_text,
            "selected_activity_id": cb.selected_activity_id,
        }
        ctx_h = context_hash(material_ctx)
        payload = {"prompt": prompt_text}
        payload_h = canonical_hash(payload)

        plan = context.get("plan")
        if plan is None:
            plan = {
                "plan_id": action.get("plan_id", "test-plan"),
                "profile": context["profile"],
                "scope": {
                    "repository": repo,
                    "branch": branch,
                },
                "actions": [action],
            }
            context["plan"] = plan

        from octodot.contracts import compute_plan_hash
        actual_plan_hash = plan_hash or plan.get("plan_hash") or compute_plan_hash(dict(plan))

        return VerifiedGrant(
            action=action["id"],
            operation_id=action["operation_id"],
            profile=context["profile"],
            profile_epoch=context["profile_epoch"],
            source=f"sources/github/{repo}",
            repository=repo,
            branch=branch,
            payload_hash=payload_h,
            context_hash=ctx_h,
            plan_hash=actual_plan_hash,
            publication_scope=publication_scope,
            authorizing_source="authority-signer-1",
            session=session,
            max_attempts=1,
        )


class TestS10T01ExactApprovedTextAndBinding(ReplyBaseTestCase):
    """S10-T01: Exact approved text, source/repository/branch/session/bundle match required; stale state, changed bundle, partial history and branch drift yield zero POST."""

    def test_s10_t01_happy_path_exact_reply_succeeds(self) -> None:
        """S10-T01: Matching prerequisites and grant yield exactly one POST with byte-exact prompt."""
        prompt = "Approved: proceed with option A."
        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-reply-1",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-reply-1",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
            "preconditions": {
                "repository": "OWNER/REPO",
                "branch": "main",
                "session": "sessions/EXAMPLE",
            },
        }

        plan = {
            "plan_id": "test-plan",
            "profile": "default",
            "scope": {"repository": "OWNER/REPO", "branch": "main"},
            "actions": [action],
        }
        context["plan"] = plan

        grant = self._make_verified_grant(action, context, prompt)
        verifier = FakeGrantVerifier(grants={"grant-1": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertEqual(result.data_dict["prompt"], prompt)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0]["body"], canonical_bytes({"prompt": prompt}))

    def test_s10_t01_stale_terminal_state_yields_zero_post(self) -> None:
        """S10-T01: Stale completed/failed session state blocks dispatch with zero POST."""
        session_dict = _make_sample_session_dict(state="COMPLETED")
        transport = self._setup_transport(session_dict=session_dict)
        context = self._make_context(transport)

        action = {
            "id": "act-reply-stale",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-stale-1",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": "Proceed."},
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.OPERATION_CONFLICT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t01_changed_bundle_precondition_yields_zero_post(self) -> None:
        """S10-T01: Feedback bundle hash mismatch against precondition yields zero POST."""
        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-reply-changed-bundle",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-bundle-mismatch",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": "Proceed."},
            "preconditions": {
                "feedback_bundle_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            },
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t01_partial_history_coverage_yields_zero_post(self) -> None:
        """S10-T01: Partial history coverage blocks dispatch with zero POST."""
        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-reply-partial-cov",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-partial-1",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": "Proceed."},
        }

        # Simulate read service returning incomplete coverage
        real_chats = context["read_service"].chats
        def incomplete_chats(sel: Any, fresh: bool = True) -> Any:
            coll = real_chats(sel, fresh=fresh)
            coll.coverage = Coverage(complete=False, reasons=("max_pages_reached",))
            return coll

        context["read_service"].chats = incomplete_chats  # type: ignore[assignment]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.PARTIAL_COVERAGE)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t01_branch_drift_yields_zero_post(self) -> None:
        """S10-T01: Starting branch mismatch against expected branch yields zero POST."""
        session_dict = _make_sample_session_dict(branch="feature/drifted")
        transport = self._setup_transport(session_dict=session_dict)
        context = self._make_context(transport)

        action = {
            "id": "act-reply-drift",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-drift-1",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": "Proceed."},
            "preconditions": {
                "branch": "main",
            },
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BINDING_MISMATCH)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t01_branch_unverified_yields_zero_post(self) -> None:
        """S10-T01: Absent starting branch metadata blocks dispatch as BRANCH_UNVERIFIED."""
        session_dict = _make_sample_session_dict()
        session_dict["sourceContext"]["githubRepoContext"] = {}  # No startingBranch!
        transport = self._setup_transport(session_dict=session_dict)
        context = self._make_context(transport)

        action = {
            "id": "act-reply-nobranch",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-nobranch-1",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": "Proceed."},
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.BRANCH_UNVERIFIED)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)


class TestS10T02MultiMessageBundleAndMalformedContent(ReplyBaseTestCase):
    """S10-T02: Multi-message bundle preserved; tied chronology and missing/blank/malformed message content block dispatch."""

    def test_s10_t02_multi_message_bundle_preserved_in_dispatch(self) -> None:
        """S10-T02: Multi-message agent bundle is preserved in chronological order with exact hashes."""
        acts = _make_sample_activities_dict(multi_message=True)
        transport = self._setup_transport(activities_dict=acts)
        context = self._make_context(transport)

        prompt = "Confirmed: proceed with option B."
        action = {
            "id": "act-multi-msg",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-multi-1",
            "authorization_ref": "grant-multi",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        grant = self._make_verified_grant(action, context, prompt)
        verifier = FakeGrantVerifier(grants={"grant-multi": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertTrue(result.data_dict["api_accepted"])

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

    def test_s10_t02_tied_chronology_blocks_dispatch(self) -> None:
        """S10-T02: Tied integer nanoseconds in conversation chronology block dispatch."""
        acts = _make_sample_activities_dict(tied_chronology=True)
        transport = self._setup_transport(activities_dict=acts)
        context = self._make_context(transport)

        action = {
            "id": "act-tied",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-tied-1",
            "authorization_ref": "grant-tied",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": "Proceed."},
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.IDENTITY_AMBIGUOUS)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t02_missing_message_text_blocks_dispatch(self) -> None:
        """S10-T02: Missing prompt/text field in payload blocks dispatch."""
        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-missing",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-missing-1",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {},
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t02_blank_message_text_blocks_dispatch(self) -> None:
        """S10-T02: Blank or whitespace-only prompt blocks dispatch."""
        transport = self._setup_transport()
        context = self._make_context(transport)

        for blank in ("", "   ", "\t\n  \r\n"):
            with self.subTest(blank=repr(blank)):
                action = {
                    "id": "act-blank",
                    "op": "chats.reply",
                    "enabled": True,
                    "operation_id": f"op-blank-{hash(blank)}",
                    "authorization_ref": "grant-1",
                    "target": "sessions/EXAMPLE",
                    "payload": {"prompt": blank},
                }

                handler = ChatsReplyHandler()
                result = handler.execute(action, context)

                self.assertEqual(result.status, ActionResultStatus.BLOCKED)
                self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
                self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

                post_calls = [c for c in transport.calls if c["method"] == "POST"]
                self.assertEqual(len(post_calls), 0)

    def test_s10_t02_malformed_message_content_blocks_dispatch(self) -> None:
        """S10-T02: Non-string prompt blocks dispatch."""
        transport = self._setup_transport()
        context = self._make_context(transport)

        for malformed in (12345, ["not", "string"], {"nested": "dict"}):
            with self.subTest(malformed=type(malformed).__name__):
                action = {
                    "id": "act-malformed",
                    "op": "chats.reply",
                    "enabled": True,
                    "operation_id": f"op-malformed-{type(malformed).__name__}",
                    "authorization_ref": "grant-1",
                    "target": "sessions/EXAMPLE",
                    "payload": {"prompt": malformed},
                }

                handler = ChatsReplyHandler()
                result = handler.execute(action, context)

                self.assertEqual(result.status, ActionResultStatus.BLOCKED)
                self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
                self.assertEqual(result.error_code, ErrorCode.INVALID_INPUT)

                post_calls = [c for c in transport.calls if c["method"] == "POST"]
                self.assertEqual(len(post_calls), 0)


class TestS10T03EmptySuccessAndReconciliationAttribution(ReplyBaseTestCase):
    """S10-T03: Empty successful response is acceptance only; new exact user activity is effect evidence, not proof of attribution."""

    def test_s10_t03_empty_success_is_acceptance_only(self) -> None:
        """S10-T03: 200 with empty body records api_accepted=True without claiming effect_observed."""
        prompt = "Acceptance test message."
        # Configure activities without the reply having appeared yet
        acts = _make_sample_activities_dict()
        transport = self._setup_transport(activities_dict=acts)
        context = self._make_context(transport)

        action = {
            "id": "act-accept-only",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-acc-1",
            "authorization_ref": "grant-acc",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        grant = self._make_verified_grant(action, context, prompt)
        verifier = FakeGrantVerifier(grants={"grant-acc": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.exit_code, EXIT_OK)
        # Separate flags
        self.assertTrue(result.data_dict["api_accepted"])
        self.assertFalse(result.data_dict["effect_observed"])
        self.assertEqual(result.data_dict["attribution"], "")
        self.assertFalse(result.data_dict["ui_verified"])

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

    def test_s10_t03_reconciliation_observes_user_activity_with_manual_attribution_uncertainty(self) -> None:
        """S10-T03: Subsequent read observing new matching user activity sets effect_observed=True but attribution as uncertain:manual_match."""
        prompt = "Exact approved prompt text."
        acts_initial = _make_sample_activities_dict()
        transport = self._setup_transport(activities_dict=acts_initial)
        context = self._make_context(transport)

        action = {
            "id": "act-reconcile",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-rec-1",
            "authorization_ref": "grant-rec",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        grant = self._make_verified_grant(action, context, prompt)
        verifier = FakeGrantVerifier(grants={"grant-rec": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        # When reconciliation queries activities, return the new user activity
        reconciled_acts = _make_sample_activities_dict()
        reconciled_acts["activities"].append({
            "name": "sessions/EXAMPLE/activities/act-new",
            "id": "act-new",
            "type": "userMessage",
            "originator": "USER",
            "createTime": "2026-10-07T12:03:00.000000005Z",
            "text": prompt,
        })
        transport.set_response(
            "GET",
            "/v1alpha/sessions/EXAMPLE/activities",
            [
                # Preflight 1 (chats rescan)
                TransportOutcome(status=200, body=json.dumps(acts_initial).encode("utf-8")),
                # Preflight 2 (inside prepare_action)
                TransportOutcome(status=200, body=json.dumps(acts_initial).encode("utf-8")),
                # Post-dispatch reconciliation scan
                TransportOutcome(status=200, body=json.dumps(reconciled_acts).encode("utf-8")),
            ],
        )

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertTrue(result.data_dict["api_accepted"])
        # New user activity observed: effect evidence, not controller attribution proof
        self.assertTrue(result.data_dict["effect_observed"])
        self.assertEqual(result.data_dict["attribution"], "uncertain:manual_match")

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)


class TestS10T04NoSecondAttempt(ReplyBaseTestCase):
    """S10-T04: Manual identical message, text changed after authorization, unknown existing intent and restart cannot cause a second attempt."""

    def test_s10_t04_manual_identical_message_yields_zero_post(self) -> None:
        """S10-T04: Existing identical manual message in conversation blocks dispatch with zero POST."""
        prompt = "Identical message already posted."
        acts = _make_sample_activities_dict(identical_user_message=prompt)
        transport = self._setup_transport(activities_dict=acts)
        context = self._make_context(transport)

        action = {
            "id": "act-dup-manual",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-dup-manual",
            "authorization_ref": "grant-1",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertIn(result.error_code, (ErrorCode.OPERATION_CONFLICT, ErrorCode.IDENTITY_AMBIGUOUS))

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t04_text_changed_after_authorization_yields_zero_post(self) -> None:
        """S10-T04: Text changed after grant creation fails payload hash check with zero POST."""
        original_prompt = "Original approved text."
        modified_prompt = "Modified text after grant."

        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-text-changed",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-text-changed",
            "authorization_ref": "grant-original",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": modified_prompt},
        }

        # Issue grant over original_prompt
        grant = self._make_verified_grant(action, context, original_prompt)
        verifier = FakeGrantVerifier(grants={"grant-original": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.GRANT_INVALID)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t04_existing_unknown_intent_blocks_second_attempt(self) -> None:
        """S10-T04: Existing unresolved intent in UNKNOWN state blocks subsequent dispatch with zero POST."""
        prompt = "Message with pending unknown intent."
        transport = self._setup_transport()
        context = self._make_context(transport)

        # Pre-seed an existing operation in UNKNOWN state
        op_id = "op-unknown-1"
        self.store.save_operation(
            OperationRecord(
                operation_id=op_id,
                state=OperationState.UNKNOWN,
                request_hash="sha256:pre-existing-hash",
                binding=Binding(profile="default", profile_epoch=1, source="src", repository="OWNER/REPO", starting_branch="main", session="sessions/EXAMPLE"),
            ),
            fence=self.fence,
        )

        action = {
            "id": "act-unknown-block",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": op_id,
            "authorization_ref": "grant-unknown",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        grant = self._make_verified_grant(action, context, prompt)
        verifier = FakeGrantVerifier(grants={"grant-unknown": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertIn(result.status, (ActionResultStatus.UNKNOWN, ActionResultStatus.BLOCKED))
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.UNRESOLVED_INTENT)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t04_restart_or_replay_blocks_second_post(self) -> None:
        """S10-T04: Restart/replay with recorded operation in ACCEPTED state never issues a second POST."""
        prompt = "Approved replay prompt."
        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-replay",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-replay-1",
            "authorization_ref": "grant-replay",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        grant = self._make_verified_grant(action, context, prompt)
        verifier = FakeGrantVerifier(grants={"grant-replay": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()

        # Run 1: Successful initial dispatch
        res1 = handler.execute(action, context)
        self.assertEqual(res1.status, ActionResultStatus.OK)
        post_calls_1 = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls_1), 1)

        # Run 2: Replay after restart (same operation_id, same prompt)
        res2 = handler.execute(action, context)
        self.assertEqual(res2.status, ActionResultStatus.OK)
        self.assertTrue(res2.data_dict["api_accepted"])

        # Still exactly 1 total POST call across both executions!
        post_calls_2 = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls_2), 1)


class TestS10T05SecurityAndPublicationGating(ReplyBaseTestCase):
    """S10-T05: Secret-pattern input, unauthorized consequential content and a grant covering a different publication effect fail closed."""

    def test_s10_t05_secret_pattern_input_fails_closed(self) -> None:
        """S10-T05: Secret patterns (GitHub tokens, API keys, bearer tokens) fail closed with zero POST."""
        secret_examples = [
            "Please use token ghp_1234567890abcdef1234567890abcdef to authenticate.",
            "My key is AIzaSyD-1234567890123456789012345678901 for access.",
            "Use Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.secrettoken123456",
            "AWS credentials: AKIAIOSFODNN7EXAMPLE",
            "api_key = 'abcdef1234567890abcdef123456'",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
        ]

        transport = self._setup_transport()
        context = self._make_context(transport)

        for secret_text in secret_examples:
            with self.subTest(secret=secret_text[:30]):
                action = {
                    "id": "act-secret",
                    "op": "chats.reply",
                    "enabled": True,
                    "operation_id": f"op-secret-{hash(secret_text)}",
                    "authorization_ref": "grant-1",
                    "target": "sessions/EXAMPLE",
                    "payload": {"prompt": secret_text},
                }

                handler = ChatsReplyHandler()
                result = handler.execute(action, context)

                self.assertEqual(result.status, ActionResultStatus.BLOCKED)
                self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
                self.assertEqual(result.error_code, ErrorCode.AUTH_DENIED)

                post_calls = [c for c in transport.calls if c["method"] == "POST"]
                self.assertEqual(len(post_calls), 0)

    def test_s10_t05_unauthorized_consequential_content_fails_closed(self) -> None:
        """S10-T05: Unauthorized consequential directives in chat reply fail closed with zero POST."""
        consequential_prompts = [
            "Please deploy to production immediately.",
            "Auto_create_pr now.",
            "Create pull request for these changes.",
            "Delete repository OWNER/REPO",
            "Drop database test_db",
            "Call approve_plan on plan-123",
        ]

        transport = self._setup_transport()
        context = self._make_context(transport)

        for prompt in consequential_prompts:
            with self.subTest(prompt=prompt):
                action = {
                    "id": "act-conseq",
                    "op": "chats.reply",
                    "enabled": True,
                    "operation_id": f"op-conseq-{hash(prompt)}",
                    "authorization_ref": "grant-1",
                    "target": "sessions/EXAMPLE",
                    "payload": {"prompt": prompt},
                }

                handler = ChatsReplyHandler()
                result = handler.execute(action, context)

                self.assertEqual(result.status, ActionResultStatus.BLOCKED)
                self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
                self.assertEqual(result.error_code, ErrorCode.AUTH_DENIED)

                post_calls = [c for c in transport.calls if c["method"] == "POST"]
                self.assertEqual(len(post_calls), 0)

    def test_s10_t05_grant_different_publication_effect_fails_closed(self) -> None:
        """S10-T05: Grant with different publication scope (e.g. 'pr') fails closed with zero POST."""
        prompt = "Valid chat reply."
        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-pub-mismatch",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-pub-1",
            "authorization_ref": "grant-pub-mismatch",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        # Issue grant with publication_scope='pr' instead of 'none'
        grant = self._make_verified_grant(
            action, context, prompt, publication_scope="pr"
        )
        verifier = FakeGrantVerifier(grants={"grant-pub-mismatch": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.GRANT_INVALID)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)

    def test_s10_t05_default_disabled_grant_verifier_blocks_dispatch(self) -> None:
        """S10-T05: Default DisabledGrantVerifier unconditionally blocks dispatch with zero POST."""
        prompt = "Approved chat text."
        transport = self._setup_transport()
        # Context without custom verifier -> defaults to DisabledGrantVerifier
        context = self._make_context(transport, verifier=DisabledGrantVerifier())

        action = {
            "id": "act-disabled-ver",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-disabled-1",
            "authorization_ref": "grant-any",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": prompt},
        }

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.BLOCKED)
        self.assertEqual(result.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(result.error_code, ErrorCode.VERIFIER_UNAVAILABLE)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 0)


class TestS10FullReplySequenceByteFidelity(ReplyBaseTestCase):
    """Offline fixtures verifying full reply sequence and byte-for-byte fidelity."""

    def test_full_reply_sequence_preserves_unicode_and_exact_bytes(self) -> None:
        """Full reply sequence preserves exact Unicode, punctuation, emoji, newlines and spaces."""
        complex_text = (
            "“Approved: Proceed with phase 2.” 🚀\n"
            "  * Line 2 with 2-space indent\n"
            "  * Unicode glyphs: α, β, γ, 日本語, ñoño\n"
            "End of directive."
        )

        transport = self._setup_transport()
        context = self._make_context(transport)

        action = {
            "id": "act-byte-fidelity",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-fidelity-1",
            "authorization_ref": "grant-fidelity",
            "target": "sessions/EXAMPLE",
            "payload": {"prompt": complex_text},
            "preconditions": {
                "repository": "OWNER/REPO",
                "branch": "main",
                "session": "sessions/EXAMPLE",
            },
        }

        grant = self._make_verified_grant(action, context, complex_text)
        verifier = FakeGrantVerifier(grants={"grant-fidelity": grant})
        context["grant_verifier"] = verifier
        context["journal"] = Journal(
            store=self.store, verifier=verifier, fence=self.fence, clock=self.clock
        )
        context["client"].ticket_authority = context["journal"]

        handler = ChatsReplyHandler()
        result = handler.execute(action, context)

        self.assertEqual(result.status, ActionResultStatus.OK)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(result.data_dict["prompt"], complex_text)

        post_calls = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)

        # Byte-for-byte assertion
        expected_body = canonical_bytes({"prompt": complex_text})
        self.assertEqual(post_calls[0]["body"], expected_body)
        parsed = json.loads(post_calls[0]["body"].decode("utf-8"))
        self.assertEqual(parsed["prompt"], complex_text)


if __name__ == "__main__":
    unittest.main()
