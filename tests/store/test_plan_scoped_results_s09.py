"""Tests for F3: Plan-scoped action results identity, durable plan_bindings, and v003 migration.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.contracts import LIVE_INVOCATION_DEFAULTS, compute_plan_hash
from octodot.errors import ErrorCode, OctodotError, StateStoreError
from octodot.migrations import CURRENT_SCHEMA_VERSION, get_schema_version, migrate_database
from octodot.models import ActionResult, ActionResultStatus
from octodot.runner import ActionRunner, run_plan
from octodot.store import SQLiteStore
from octodot.transport import FixtureTransport


class MockActionHandler:
    def __init__(self, op: str, data: dict | None = None) -> None:
        self.op = op
        self.data = data or {}
        self.call_count = 0

    def can_handle(self, op: str) -> bool:
        return op == self.op

    def execute(self, action: dict, context: dict) -> ActionResult:
        self.call_count += 1
        d = dict(self.data)
        d["_plan_hash"] = context.get("plan", {}).get("plan_hash", "")
        return ActionResult.create(
            action_id=action["id"],
            op=action["op"],
            status=ActionResultStatus.OK,
            exit_code=0,
            data=d,
        )


def _make_plan(plan_id: str, actions: list[dict]) -> dict:
    plan = {
        "schema_version": "jules-controller.plan.v1",
        "plan_id": plan_id,
        "profile": "default",
        "execution": {"mode": "read_only"},
        "scope": {"repository": "OWNER/REPO"},
        "limits": dict(LIVE_INVOCATION_DEFAULTS, max_posts=0),
        "actions": actions,
        "output": {"format": "json"},
    }
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


class TestPlanScopedResultsS09(unittest.TestCase):
    """Test suite validating F3 requirements with reopened file-backed store."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.transport = FixtureTransport()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_v003_migration_preserves_seeded_rows_and_creates_bindings(self) -> None:
        """v003 migration preserves existing v2 rows, does not erase journal, and creates plan_bindings."""
        db_path = os.path.join(self.test_dir, "octodot.db")
        conn = sqlite3.connect(db_path)

        # Apply migrations up to v2
        migrate_database(conn, target_version=2)
        self.assertEqual(get_schema_version(conn), 2)

        # Seed v2 action_results
        hash_seed = "hash-seed-1"
        conn.execute(
            """
            INSERT INTO action_results (action_id, plan_id, op, status, exit_code, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("act-shared", "plan-seed-1", "healthcheck", "ok", 0, json.dumps({"_plan_hash": hash_seed, "source": "v2"}), "2026-10-01T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO action_results (action_id, plan_id, op, status, exit_code, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("act-unique", "plan-seed-1", "session.inspect", "ok", 0, json.dumps({"_plan_hash": hash_seed, "source": "v2"}), "2026-10-01T00:00:00Z"),
        )
        # Seed an operation to verify journal is not erased
        conn.execute(
            """
            INSERT INTO operations (operation_id, state, request_hash, profile, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("op-seed-1", "accepted", "req-hash-1", "default", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        # Migrate to v3 via SQLiteStore auto_migrate
        store = SQLiteStore(self.test_dir, auto_migrate=True)
        try:
            conn2 = sqlite3.connect(str(store.db_path))
            self.assertEqual(get_schema_version(conn2), CURRENT_SCHEMA_VERSION)
            self.assertEqual(get_schema_version(conn2), 3)

            # Verify plan_bindings was created and seeded
            cursor = conn2.cursor()
            cursor.execute("SELECT plan_id, plan_hash FROM plan_bindings WHERE plan_id = ?", ("plan-seed-1",))
            binding_row = cursor.fetchone()
            self.assertIsNotNone(binding_row)
            self.assertEqual(binding_row[0], "plan-seed-1")
            self.assertEqual(binding_row[1], hash_seed)

            # Verify action_results rows preserved and accessible plan-scoped
            ar1 = store.get_action_result("act-shared", plan_id="plan-seed-1")
            self.assertIsNotNone(ar1)
            self.assertEqual(dict(ar1.data)["source"], "v2")

            ar2 = store.get_action_result("act-unique", plan_id="plan-seed-1")
            self.assertIsNotNone(ar2)
            self.assertEqual(dict(ar2.data)["source"], "v2")

            # Verify operations table preserved (journal not erased)
            op_rec = store.get_operation("op-seed-1")
            self.assertIsNotNone(op_rec)
            self.assertEqual(op_rec.operation_id, "op-seed-1")

            conn2.close()
        finally:
            store.close()

    def test_exact_a_b_a_scenario_returns_only_a_data(self) -> None:
        """The exact A -> B -> A scenario returns only A's data using reopened file-backed store."""
        # 1. Run Plan A with actions shared + unique_a
        plan_a = _make_plan("plan-A", [
            {"id": "act-shared", "op": "healthcheck", "params": {}},
            {"id": "act-unique-a", "op": "healthcheck", "params": {}},
        ])

        handler_a_shared = MockActionHandler("healthcheck", {"from": "plan_A_shared"})
        handler_a_unique = MockActionHandler("healthcheck", {"from": "plan_A_unique"})

        store1 = SQLiteStore(self.test_dir)
        try:
            res_a = run_plan(
                plan_a,
                handlers={"healthcheck": handler_a_shared},
                store=store1,
                transport=self.transport,
            )
            self.assertEqual(res_a["status"], "ok")
        finally:
            store1.close()

        # 2. Reopen DB and Run Plan B with shared (returning different data) + unique_b
        plan_b = _make_plan("plan-B", [
            {"id": "act-shared", "op": "healthcheck", "params": {}},
            {"id": "act-unique-b", "op": "healthcheck", "params": {}},
        ])

        handler_b_shared = MockActionHandler("healthcheck", {"from": "plan_B_shared_different"})

        store2 = SQLiteStore(self.test_dir)
        try:
            res_b = run_plan(
                plan_b,
                handlers={"healthcheck": handler_b_shared},
                store=store2,
                transport=self.transport,
            )
            self.assertEqual(res_b["status"], "ok")
        finally:
            store2.close()

        # 3. Reopen DB and Replay Plan A
        store3 = SQLiteStore(self.test_dir)
        try:
            # Replay Plan A with NO handler calls expected
            no_call_handler = MockActionHandler("healthcheck", {"from": "MUST_NOT_BE_CALLED"})
            res_a_replay = run_plan(
                plan_a,
                handlers={"healthcheck": no_call_handler},
                store=store3,
                transport=self.transport,
            )
            self.assertEqual(res_a_replay["status"], "ok")
            self.assertEqual(no_call_handler.call_count, 0)

            # Verify that replayed A contains ONLY A's data, NOT B's shared result!
            results_map = {r["action_id"]: dict(r["data"]) for r in res_a_replay["action_results"]}
            self.assertIn("act-shared", results_map)
            self.assertEqual(results_map["act-shared"]["from"], "plan_A_shared")
            self.assertNotEqual(results_map["act-shared"]["from"], "plan_B_shared_different")
            self.assertEqual(results_map["act-unique-a"]["from"], "plan_A_shared")  # MockActionHandler shared op
        finally:
            store3.close()

    def test_same_action_ids_different_ops_across_plans_stay_isolated(self) -> None:
        """Same action IDs with different operations across plans remain isolated in reopened DB."""
        plan_a = _make_plan("plan-op-A", [
            {"id": "act-same-id", "op": "healthcheck", "params": {}},
        ])
        plan_b = _make_plan("plan-op-B", [
            {"id": "act-same-id", "op": "capabilities.inspect", "params": {}},
        ])

        h_hc = MockActionHandler("healthcheck", {"type": "healthcheck"})
        h_ci = MockActionHandler("capabilities.inspect", {"type": "inspect"})

        store1 = SQLiteStore(self.test_dir)
        try:
            run_plan(plan_a, handlers={"healthcheck": h_hc}, store=store1, transport=self.transport)
            run_plan(plan_b, handlers={"capabilities.inspect": h_ci}, store=store1, transport=self.transport)
        finally:
            store1.close()

        # Reopen DB and check isolation
        store2 = SQLiteStore(self.test_dir)
        try:
            ar_a = store2.get_action_result("act-same-id", plan_id="plan-op-A")
            self.assertIsNotNone(ar_a)
            self.assertEqual(ar_a.op, "healthcheck")
            self.assertEqual(dict(ar_a.data)["type"], "healthcheck")

            ar_b = store2.get_action_result("act-same-id", plan_id="plan-op-B")
            self.assertIsNotNone(ar_b)
            self.assertEqual(ar_b.op, "capabilities.inspect")
            self.assertEqual(dict(ar_b.data)["type"], "inspect")
        finally:
            store2.close()

    def test_changed_content_under_same_plan_id_rejected_even_if_incomplete(self) -> None:
        """Reusing same plan_id with different hash is rejected even if action rows are incomplete."""
        store = SQLiteStore(self.test_dir)
        try:
            # 1. Partial/incomplete execution of plan-conflict
            plan_orig = _make_plan("plan-conflict", [
                {"id": "act-1", "op": "healthcheck", "params": {}},
                {"id": "act-2", "op": "capabilities.inspect", "params": {}},
            ])
            # Durable plan binding is established
            store.record_plan_binding(plan_orig["plan_id"], plan_orig["plan_hash"])

            # Save only act-1 (incomplete actions in store)
            ar1 = ActionResult.create(
                action_id="act-1",
                op="healthcheck",
                status=ActionResultStatus.OK,
                exit_code=0,
                data={"_plan_hash": plan_orig["plan_hash"]},
            )
            store.save_action_result(ar1, plan_id=plan_orig["plan_id"])

            # 2. Try to run plan with SAME plan_id but CHANGED actions (different hash)
            plan_changed = copy.deepcopy(plan_orig)
            plan_changed["scope"]["branch"] = "different-branch"
            plan_changed["plan_hash"] = compute_plan_hash(plan_changed)

            with self.assertRaises(OctodotError) as ctx:
                run_plan(plan_changed, store=store, transport=self.transport)
            self.assertEqual(ctx.exception.code, ErrorCode.OPERATION_CONFLICT)
        finally:
            store.close()

    def test_interrupted_partial_plan_replays_or_resumes(self) -> None:
        """Interrupted/partial plan records do not falsely replay as complete, and retain expected behavior."""
        store1 = SQLiteStore(self.test_dir)
        try:
            plan = _make_plan("plan-partial-test", [
                {"id": "act-1", "op": "healthcheck", "params": {}},
                {"id": "act-2", "op": "healthcheck", "params": {}},
            ])
            # Plan binding recorded
            store1.record_plan_binding(plan["plan_id"], plan["plan_hash"])

            # Only act-1 saved (interrupted before act-2)
            ar1 = ActionResult.create(
                action_id="act-1",
                op="healthcheck",
                status=ActionResultStatus.OK,
                exit_code=0,
                data={"_plan_hash": plan["plan_hash"], "val": "first"},
            )
            store1.save_action_result(ar1, plan_id=plan["plan_id"])
        finally:
            store1.close()

        # Reopen DB
        store2 = SQLiteStore(self.test_dir)
        try:
            # Running plan with same hash does not return incomplete replay of 1 item;
            # it executes handlers
            h = MockActionHandler("healthcheck", {"val": "executed"})
            res = run_plan(plan, handlers={"healthcheck": h}, store=store2, transport=self.transport)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(len(res["action_results"]), 2)

            # Now replay with same hash should replay all 2 actions with zero handler calls
            h_noop = MockActionHandler("healthcheck")
            res_replay = run_plan(plan, handlers={"healthcheck": h_noop}, store=store2, transport=self.transport)
            self.assertEqual(res_replay["status"], "ok")
            self.assertEqual(h_noop.call_count, 0)
            self.assertEqual(len(res_replay["action_results"]), 2)
        finally:
            store2.close()


if __name__ == "__main__":
    unittest.main()
