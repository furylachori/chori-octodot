"""Compatibility bridge and shorthand verification tests (S14-T02).

Standard library only. Compatible with Python 3.10+.
Covers S14-T02:
- Shorthand commands compile to valid jules-controller.plan.v1 read-only plans
- Shorthand commands reject mutations (send, reply, create, approve) with AUTH_DENIED
- Shorthand execution through ActionRunner produces valid results and deterministic exit codes
- Default registry / DisabledGrantVerifier blocks any mutation dispatch without a verified grant
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

from octodot.api import JulesClient
from octodot.authorization import DisabledGrantVerifier
from octodot.compat import (
    MUTATION_SHORTHAND_NAMES,
    SUPPORTED_SHORTHANDS,
    compile_shorthand_plan,
    run_shorthand,
    shorthand_ack,
    shorthand_chats,
    shorthand_events,
    shorthand_healthcheck,
    shorthand_inspect,
    shorthand_inventory,
    shorthand_reconcile,
    shorthand_status,
    shorthand_wait,
)
from octodot.contracts import (
    LIVE_INVOCATION_DEFAULTS,
    compute_plan_hash,
    validate_plan,
    validate_result,
)
from octodot.errors import (
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    ErrorCode,
    OctodotError,
)
from octodot.models import (
    Event,
    OperationRecord,
    OperationState,
    TransportOutcome,
)
from octodot.reads import ReadService
from octodot.registry import build_handler_registry
from octodot.runner import ActionRunner, run_plan
from octodot.store import InMemoryRecoveryFence, SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class TestCompatS14T02(unittest.TestCase):
    """S14-T02 Compatibility bridge and shorthand compiler test suite."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.fence = InMemoryRecoveryFence(epochs={"default": 1})
        self.clock = FakeClock()
        self.store = SQLiteStore(state_dir=self.state_dir, fence=self.fence)
        self.store.reconcile_profile_epoch("default", epoch=1, identity_validated=True, fence=self.fence)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s14_t02_shorthands_compile_valid_read_only_plans(self) -> None:
        """S14-T02: All supported shorthands compile to valid jules-controller.plan.v1 read-only plans."""
        shorthand_calls: dict[str, dict[str, Any]] = {
            "inventory": {"repo": "OWNER/REPO", "scope": "all"},
            "inspect": {"session": "sessions/s-example-1", "repo": "OWNER/REPO"},
            "chats": {"session": "sessions/s-example-1", "repo": "OWNER/REPO"},
            "events": {"session": "sessions/s-example-1", "repo": "OWNER/REPO", "limit": 20},
            "ack": {"event_id": "evt-1", "repo": "OWNER/REPO"},
            "wait": {"predicate": "all_terminal", "timeout_seconds": 10.0, "repo": "OWNER/REPO"},
            "reconcile": {"operation_id": "op-test-1", "repo": "OWNER/REPO", "scans": 2},
            "status": {"repo": "OWNER/REPO"},
            "healthcheck": {"repo": "OWNER/REPO"},
        }

        # Verify all supported shorthands are tested
        self.assertEqual(set(shorthand_calls.keys()), set(SUPPORTED_SHORTHANDS))

        for cmd, kwargs in shorthand_calls.items():
            with self.subTest(command=cmd):
                plan = compile_shorthand_plan(cmd, **kwargs)

                # Schema version and execution mode
                self.assertEqual(plan["schema_version"], "jules-controller.plan.v1")
                self.assertEqual(plan["execution"]["mode"], "read_only")
                self.assertEqual(plan["scope"]["repository"], "OWNER/REPO")

                # Plan hash integrity
                expected_hash = compute_plan_hash(plan)
                self.assertEqual(plan["plan_hash"], expected_hash)

                # Strict contract validation passes
                validate_plan(plan)

                # Exactly one action per shorthand
                self.assertEqual(len(plan["actions"]), 1)
                action = plan["actions"][0]
                self.assertTrue(action["id"].startswith("act-"))

    def test_s14_t02_shorthand_helper_functions_match_compiler(self) -> None:
        """S14-T02: Convenience shorthand_* helper functions compile identical plans."""
        p_inv = shorthand_inventory("OWNER/REPO", scope="all", plan_id="plan-p-inv")
        validate_plan(p_inv)
        self.assertEqual(p_inv["actions"][0]["op"], "inventory.collect")

        p_insp = shorthand_inspect("sessions/s-1", "OWNER/REPO", plan_id="plan-p-insp")
        validate_plan(p_insp)
        self.assertEqual(p_insp["actions"][0]["op"], "session.inspect")

        p_chats = shorthand_chats("sessions/s-1", "OWNER/REPO", plan_id="plan-p-chats")
        validate_plan(p_chats)
        self.assertEqual(p_chats["actions"][0]["op"], "chats.collect")

        p_events = shorthand_events("OWNER/REPO", session="sessions/s-1", plan_id="plan-p-ev")
        validate_plan(p_events)
        self.assertEqual(p_events["actions"][0]["op"], "events.read")

        p_ack = shorthand_ack("OWNER/REPO", event_ids=["evt-1"], plan_id="plan-p-ack")
        validate_plan(p_ack)
        self.assertEqual(p_ack["actions"][0]["op"], "events.ack")

        p_wait = shorthand_wait("all_terminal", timeout=5.0, repo="OWNER/REPO", plan_id="plan-p-wait")
        validate_plan(p_wait)
        self.assertEqual(p_wait["actions"][0]["op"], "wait")

        p_rec = shorthand_reconcile("op-1", repo="OWNER/REPO", plan_id="plan-p-rec")
        validate_plan(p_rec)
        self.assertEqual(p_rec["actions"][0]["op"], "operations.reconcile")

        p_stat = shorthand_status("OWNER/REPO", plan_id="plan-p-stat")
        validate_plan(p_stat)
        self.assertEqual(p_stat["actions"][0]["op"], "healthcheck")

        p_hc = shorthand_healthcheck("OWNER/REPO", plan_id="plan-p-hc")
        validate_plan(p_hc)
        self.assertEqual(p_hc["actions"][0]["op"], "healthcheck")


    def test_s14_t02_mutations_rejected_with_auth_denied(self) -> None:
        """S14-T02: Shorthand compiler and runner reject mutation names with AUTH_DENIED."""
        for mut_cmd in MUTATION_SHORTHAND_NAMES:
            with self.subTest(mutation=mut_cmd):
                # 1. compile_shorthand_plan raises AUTH_DENIED
                with self.assertRaises(OctodotError) as ctx_compile:
                    compile_shorthand_plan(mut_cmd)
                self.assertEqual(ctx_compile.exception.code, ErrorCode.AUTH_DENIED)

                # 2. run_shorthand raises AUTH_DENIED
                with self.assertRaises(OctodotError) as ctx_run:
                    run_shorthand(mut_cmd, store=self.store)
                self.assertEqual(ctx_run.exception.code, ErrorCode.AUTH_DENIED)

        # 3. Unknown shorthand command raises INVALID_INPUT
        with self.assertRaises(OctodotError) as ctx_unk:
            compile_shorthand_plan("nonexistent_command")
        self.assertEqual(ctx_unk.exception.code, ErrorCode.INVALID_INPUT)

    def test_s14_t02_shorthands_execute_through_runner(self) -> None:
        """S14-T02: Shorthand commands execute through ActionRunner and return valid results."""
        sources_data = {
            "sources": [
                {
                    "name": "sources/github/OWNER/REPO",
                    "id": "src-1",
                    "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                }
            ]
        }
        session_data = {
            "name": "sessions/s-example-1",
            "id": "s-example-1",
            "title": "Example Session",
            "state": "RUNNING",
            "createTime": "2026-10-07T12:00:00Z",
            "updateTime": "2026-10-07T12:01:00Z",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/integ"},
            },
        }
        activities_data = {
            "activities": [
                {
                    "name": "sessions/s-example-1/activities/act-1",
                    "id": "act-1",
                    "type": "userMessage",
                    "originator": "USER",
                    "createTime": "2026-10-07T12:00:00Z",
                    "text": "Hello",
                }
            ]
        }
        responses = {
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=json.dumps(sources_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=json.dumps({"sessions": [session_data]}).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-example-1"): TransportOutcome(status=200, body=json.dumps(session_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-example-1/activities"): TransportOutcome(status=200, body=json.dumps(activities_data).encode("utf-8")),
        }
        transport = FixtureTransport(responses=responses)
        client = JulesClient(transport=transport, clock=self.clock)
        read_service = ReadService(api=client)

        # 1. status shorthand
        res_status = run_shorthand("status", store=self.store, read_service=read_service, clock=self.clock, transport=transport)
        validate_result(res_status)
        self.assertEqual(res_status["exit_code"], EXIT_OK)
        self.assertEqual(res_status["action_results"][0]["op"], "healthcheck")

        # 2. inventory shorthand
        res_inv = run_shorthand("inventory", repo="OWNER/REPO", store=self.store, read_service=read_service, clock=self.clock, transport=transport)
        validate_result(res_inv)
        self.assertEqual(res_inv["exit_code"], EXIT_OK)
        self.assertEqual(res_inv["action_results"][0]["op"], "inventory.collect")

        # 3. inspect shorthand
        res_insp = run_shorthand("inspect", session="sessions/s-example-1", repo="OWNER/REPO", store=self.store, read_service=read_service, clock=self.clock, transport=transport)
        validate_result(res_insp)
        self.assertEqual(res_insp["exit_code"], EXIT_OK)
        self.assertEqual(res_insp["action_results"][0]["op"], "session.inspect")

        # 4. chats shorthand
        res_chats = run_shorthand("chats", session="sessions/s-example-1", repo="OWNER/REPO", store=self.store, read_service=read_service, clock=self.clock, transport=transport)
        validate_result(res_chats)
        self.assertEqual(res_chats["exit_code"], EXIT_OK)
        self.assertEqual(res_chats["action_results"][0]["op"], "chats.collect")

        # 5. events and ack shorthands with pre-seeded store event
        self.store.save_event(Event.create(
            event_id="evt-shorthand-1",
            event_type="test_event",
            resource_id="sessions/s-example-1",
            payload={"foo": "bar"},
        ))

        res_events = run_shorthand("events", session="sessions/s-example-1", repo="OWNER/REPO", store=self.store, clock=self.clock)
        validate_result(res_events)
        self.assertEqual(res_events["exit_code"], EXIT_OK)
        self.assertEqual(res_events["action_results"][0]["op"], "events.read")

        res_ack = run_shorthand("ack", event_id="evt-shorthand-1", repo="OWNER/REPO", store=self.store, clock=self.clock)
        validate_result(res_ack)
        self.assertEqual(res_ack["exit_code"], EXIT_OK)
        self.assertEqual(res_ack["action_results"][0]["op"], "events.ack")

        # 6. reconcile shorthand
        self.store.save_operation(OperationRecord(
            operation_id="op-shorthand-rec-1",
            state=OperationState.EFFECT_OBSERVED,
            request_hash="sha256:rechash",
        ))
        res_rec = run_shorthand("reconcile", operation_id="op-shorthand-rec-1", store=self.store, clock=self.clock, read_service=read_service, transport=transport, api=client)
        validate_result(res_rec)
        self.assertEqual(res_rec["exit_code"], EXIT_OK)
        self.assertEqual(res_rec["action_results"][0]["op"], "operations.reconcile")

        # 7. wait shorthand (predicate: all_terminal returns EXIT_OK or EXIT_WAITING)
        res_wait = run_shorthand("wait", predicate="all_terminal", timeout_seconds=1.0, store=self.store, read_service=read_service, clock=self.clock)
        validate_result(res_wait)
        self.assertIn(res_wait["exit_code"], (EXIT_OK, 2))
        self.assertIn(res_wait["action_results"][0]["status"], ("ok", "waiting"))
        self.assertEqual(res_wait["action_results"][0]["op"], "wait")

        # Verify ZERO POST occurred across all shorthand runs
        posts = sum(1 for c in transport.calls if c["method"] == "POST")
        self.assertEqual(posts, 0)

    def test_s14_t02_default_registry_blocks_mutation_dispatch_without_verified_grant(self) -> None:
        """S14-T02: Default registry / DisabledGrantVerifier blocks mutation dispatch without verified grant."""
        # Setup fixture data for mutation precondition checks so they reach grant verification
        session_data = {
            "name": "sessions/s-unauth-1",
            "id": "s-unauth-1",
            "title": "Unauth Session",
            "state": "RUNNING",
            "createTime": "2026-10-07T12:00:00Z",
            "updateTime": "2026-10-07T12:01:00Z",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/integ"},
            },
        }
        activities_data = {
            "activities": [
                {
                    "name": "sessions/s-unauth-1/activities/act-1",
                    "id": "act-1",
                    "type": "userMessage",
                    "originator": "USER",
                    "createTime": "2026-10-07T12:00:00Z",
                    "text": "Hello",
                },
                {
                    "name": "sessions/s-unauth-1/activities/act-2",
                    "id": "act-2",
                    "type": "agentMessage",
                    "originator": "AGENT",
                    "createTime": "2026-10-07T12:01:00Z",
                    "text": "Hi",
                },
            ]
        }
        sources_data = {
            "sources": [
                {
                    "name": "sources/github/OWNER/REPO",
                    "id": "src-1",
                    "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                }
            ]
        }
        transport = FixtureTransport(responses={
            ("GET", "/v1alpha/sessions/s-unauth-1"): TransportOutcome(status=200, body=json.dumps(session_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions/s-unauth-1/activities"): TransportOutcome(status=200, body=json.dumps(activities_data).encode("utf-8")),
            ("GET", "/v1alpha/sources"): TransportOutcome(status=200, body=json.dumps(sources_data).encode("utf-8")),
            ("GET", "/v1alpha/sessions"): TransportOutcome(status=200, body=json.dumps({"sessions": [session_data]}).encode("utf-8")),
        })
        client = JulesClient(transport=transport, clock=self.clock)
        read_service = ReadService(api=client)
        from octodot.journal import Journal
        journal = Journal(store=self.store, verifier=DisabledGrantVerifier(), fence=self.fence, clock=self.clock)

        # 1. Read-only registry has no mutation ops registered
        ro_handlers = build_handler_registry(mode="read_only", store=self.store)
        self.assertNotIn("chats.reply", ro_handlers)
        self.assertNotIn("tasks.create", ro_handlers)
        self.assertNotIn("plans.approve", ro_handlers)

        # 2. Mutation registry built with default verifier (DisabledGrantVerifier)
        mut_handlers = build_handler_registry(
            mode="mutation",
            store=self.store,
            read_service=read_service,
            transport=transport,
            api=client,
            journal=journal,
        )
        self.assertIn("chats.reply", mut_handlers)
        self.assertIn("tasks.create", mut_handlers)
        self.assertIn("plans.approve", mut_handlers)

        # Attempt chats.reply with default verifier (no grant)
        reply_action = {
            "id": "act-unauth-reply",
            "op": "chats.reply",
            "enabled": True,
            "target": "sessions/s-unauth-1",
            "payload": {"text": "Unauthorized message"},
            "operation_id": "op-unauth-1",
            "authorization_ref": "unauthorized-grant-ref",
        }
        res_reply = mut_handlers["chats.reply"].execute(reply_action, {
            "store": self.store,
            "verifier": DisabledGrantVerifier(),
            "profile": "default",
            "read_service": read_service,
            "journal": journal,
        })
        self.assertEqual(res_reply.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(res_reply.status.value, "blocked")

        # Attempt tasks.create with default verifier
        create_action = {
            "id": "act-unauth-create",
            "op": "tasks.create",
            "enabled": True,
            "target": "OWNER/REPO",
            "payload": {"prompt": "Create task", "title": "New Task", "starting_branch": "feature/integ"},
            "preconditions": {"branch": "feature/integ"},
            "operation_id": "op-unauth-2",
            "authorization_ref": "unauthorized-grant-ref",
        }
        res_create = mut_handlers["tasks.create"].execute(create_action, {
            "store": self.store,
            "verifier": DisabledGrantVerifier(),
            "profile": "default",
            "read_service": read_service,
            "journal": journal,
        })
        self.assertEqual(res_create.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(res_create.status.value, "blocked")

        # Attempt plans.approve with default verifier
        approve_action = {
            "id": "act-unauth-approve",
            "op": "plans.approve",
            "enabled": True,
            "target": "sessions/s-unauth-1",
            "payload": {"plan_id": "plan-unauth-1"},
            "operation_id": "op-unauth-3",
            "authorization_ref": "unauthorized-grant-ref",
        }
        res_approve = mut_handlers["plans.approve"].execute(approve_action, {
            "store": self.store,
            "verifier": DisabledGrantVerifier(),
            "profile": "default",
            "read_service": read_service,
            "journal": journal,
        })
        self.assertEqual(res_approve.exit_code, EXIT_MUTATION_BLOCKED)
        self.assertEqual(res_approve.status.value, "blocked")

        # Verify ZERO POST occurred when mutations were blocked
        posts = sum(1 for c in transport.calls if c["method"] == "POST")
        self.assertEqual(posts, 0)

    def test_fr11_chats_reply_adapter_fails_closed_on_epoch_error(self) -> None:
        """FR11: ChatsReplyHandlerAdapter fails closed with RECOVERY_FENCE_STALE if fence or store raises on epoch."""
        class BrokenFence:
            def get_current_epoch(self, profile: str) -> int:
                raise RuntimeError("Fence access corrupted")

        from octodot.actions.reply import ChatsReplyHandler
        from octodot.registry import ChatsReplyHandlerAdapter
        adapter_fence = ChatsReplyHandlerAdapter(
            handler=ChatsReplyHandler(),
            fence=BrokenFence(),
        )
        action = {
            "id": "act-reply-broken-fence",
            "op": "chats.reply",
            "enabled": True,
            "target": "sessions/s-1",
            "payload": {"prompt": "Hello"},
            "operation_id": "op-reply-bf-1",
            "authorization_ref": "grant-1",
        }
        with self.assertRaises(OctodotError) as cm:
            adapter_fence.execute(action, {"profile": "default"})
        self.assertEqual(cm.exception.code, ErrorCode.RECOVERY_FENCE_STALE)

        class BrokenStore:
            def get_profile_epoch(self, profile: str) -> int:
                raise RuntimeError("Store epoch query failed")

        adapter_store = ChatsReplyHandlerAdapter(
            handler=ChatsReplyHandler(),
            fence=None,
            store=BrokenStore(),
        )
        with self.assertRaises(OctodotError) as cm2:
            adapter_store.execute(action, {"profile": "default"})
        self.assertEqual(cm2.exception.code, ErrorCode.RECOVERY_FENCE_STALE)


if __name__ == "__main__":
    unittest.main()
