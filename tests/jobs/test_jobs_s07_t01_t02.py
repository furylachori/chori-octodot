"""Tests for S07-T01 and S07-T02: Resumable waits, fake-clock deadlines, and fixed selection vs discovery.

Covers:
- S07-T01: Fake-clock unchanged polling emits no new events; deadlines yield waiting plus
  the same durable job ID, then resume to the requested condition.
- S07-T02: Discovery cadence finds new sessions when allowed; fixed selection does not silently expand.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.api import JulesClient
from octodot.contracts import WaitResult
from octodot.errors import ErrorCode, OctodotError
from octodot.events import get_unacked_events
from octodot.jobs import WaitActionHandler, execute_wait
from octodot.models import (
    ActionResultStatus,
    Binding,
    Coverage,
    Event,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
)
from octodot.reads import ReadService
from octodot.store import SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class TestJobsS07T01T02(unittest.TestCase):
    """S07-T01 and S07-T02 test cases."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s07_t01_fake_clock_deadlines_yield_waiting_and_resume_to_condition(self) -> None:
        """S07-T01: Fake-clock unchanged polling emits no new events; deadlines yield waiting plus same job ID, then resume."""
        # 1. Setup transport with an in-progress session
        session_json = json.dumps({
            "name": "sessions/s_wait_1",
            "id": "s_wait_1",
            "state": "IN_PROGRESS",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        })
        sources_json = json.dumps({
            "sources": [{
                "name": "sources/github/OWNER/REPO",
                "id": "src_1",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoOwner": "OWNER",
                "githubRepoName": "REPO",
            }]
        })
        activities_json = json.dumps({"activities": []})

        transport = FixtureTransport()
        transport.set_response("GET", "/v1alpha/sources/github/OWNER/REPO", TransportOutcome(200, sources_json))
        transport.set_response("GET", "/v1alpha/sources", TransportOutcome(200, sources_json))
        transport.set_response("GET", "/v1alpha/sessions/s_wait_1", TransportOutcome(200, session_json))
        transport.set_response("GET", "/v1alpha/sessions/s_wait_1/activities", TransportOutcome(200, activities_json))

        client = JulesClient(transport, clock=self.clock)
        read_service = ReadService(client, store=self.store, clock=self.clock)

        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session="sessions/s_wait_1",
        )

        # 2. First wait run: Predicate 'attention', timeout 10.0 seconds
        # Fake clock polling over unchanged state emits zero new events.
        res1 = execute_wait(
            predicate="attention",
            timeout_seconds=10.0,
            selection=[binding],
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            poll_interval=2.0,
            max_iterations=10,
        )

        # Verify: Deadlines yield waiting (predicate_matched=False, resumed=False)
        self.assertFalse(res1.predicate_matched)
        self.assertFalse(res1.resumed)
        self.assertIsNotNone(res1.job_id)
        job_id = res1.job_id
        assert job_id is not None

        # Check durable job in S03 store
        durable_job = self.store.load_job(job_id)
        self.assertIsNotNone(durable_job)
        assert durable_job is not None
        self.assertEqual(durable_job["status"], "waiting")
        self.assertEqual(durable_job["details"]["predicate"], "attention")

        # Unchanged polling must have emitted 0 new events in store
        unacked = self.store.get_events(unacked_only=True)
        self.assertEqual(len(unacked), 0)

        # 3. Simulate remote session now requiring attention (e.g. AWAITING_PLAN_APPROVAL)
        updated_session = json.dumps({
            "name": "sessions/s_wait_1",
            "id": "s_wait_1",
            "state": "AWAITING_PLAN_APPROVAL",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        })
        transport.set_response("GET", "/v1alpha/sessions/s_wait_1", TransportOutcome(200, updated_session))

        # 4. Resume wait with the SAME durable job ID
        res2 = execute_wait(
            predicate="attention",
            timeout_seconds=10.0,
            job_id=job_id,  # SAME job ID!
            selection=[binding],
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            poll_interval=2.0,
        )

        # Verify: Resumes to requested condition and matches!
        self.assertTrue(res2.resumed)
        self.assertTrue(res2.predicate_matched)
        self.assertEqual(res2.job_id, job_id)

        # Durable state in store is updated to completed
        updated_job = self.store.load_job(job_id)
        assert updated_job is not None
        self.assertEqual(updated_job["status"], "completed")

    def test_s07_t02_discovery_cadence_and_fixed_selection(self) -> None:
        """S07-T02: Discovery cadence finds new sessions when allowed; fixed selection does not silently expand."""
        # 1. Setup transport with session 1 and session 2
        session1_json = json.dumps({
            "name": "sessions/s1",
            "id": "s1",
            "state": "IN_PROGRESS",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        })
        session2_json = json.dumps({
            "name": "sessions/s2",
            "id": "s2",
            "state": "AWAITING_PLAN_APPROVAL",
            "sourceContext": {
                "source": "sources/github/OWNER/REPO",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoContext": {"startingBranch": "feature/example"},
            },
        })
        sources_json = json.dumps({
            "sources": [{
                "name": "sources/github/OWNER/REPO",
                "id": "src_1",
                "githubRepo": {"owner": "OWNER", "repo": "REPO"},
                "githubRepoOwner": "OWNER",
                "githubRepoName": "REPO",
            }]
        })
        activities_json = json.dumps({"activities": []})

        transport = FixtureTransport()
        transport.set_response("GET", "/v1alpha/sources", TransportOutcome(200, sources_json))
        transport.set_response("GET", "/v1alpha/sources/github/OWNER/REPO", TransportOutcome(200, sources_json))
        transport.set_response("GET", "/v1alpha/sessions/s1", TransportOutcome(200, session1_json))
        transport.set_response("GET", "/v1alpha/sessions/s1/activities", TransportOutcome(200, activities_json))
        transport.set_response("GET", "/v1alpha/sessions/s2", TransportOutcome(200, session2_json))
        transport.set_response("GET", "/v1alpha/sessions/s2/activities", TransportOutcome(200, activities_json))

        # Discovery sessions list returns s1 and s2
        discovery_sessions_json = json.dumps({
            "sessions": [
                json.loads(session1_json),
                json.loads(session2_json),
            ]
        })
        transport.set_response("GET", "/v1alpha/sessions", TransportOutcome(200, discovery_sessions_json))

        client = JulesClient(transport, clock=self.clock)
        read_service = ReadService(client, store=self.store, clock=self.clock)

        binding1 = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session="sessions/s1",
        )

        # Part A: Fixed selection with allow_discovery=False
        # Even though s2 requires attention, s1 is IN_PROGRESS.
        # Fixed selection MUST NOT silently expand to include s2!
        res_fixed = execute_wait(
            predicate="attention",
            timeout_seconds=4.0,
            selection=[binding1],
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            poll_interval=2.0,
            allow_discovery=False,
            max_iterations=2,
        )
        # s1 does not need attention, so predicate is NOT matched on fixed selection
        self.assertFalse(res_fixed.predicate_matched)

        # Part B: Allow discovery with cadence=1
        # Discovery will find s2 which requires attention, matching the predicate!
        res_discovery = execute_wait(
            predicate="attention",
            timeout_seconds=6.0,
            selection=[binding1],
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            poll_interval=1.0,
            allow_discovery=True,
            discovery_cadence=1,
            max_iterations=5,
        )
        self.assertTrue(res_discovery.predicate_matched)


if __name__ == "__main__":
    unittest.main()
