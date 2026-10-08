"""Tests for S07-T03: Terminal scan picking up late artifacts and publication watches.

Covers:
- S07-T03: Final terminal scan picks up late artifacts; all_terminal is not
  publication verified; a publication watch retains its requested predicate.
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
from octodot.jobs import check_all_terminal_predicate, execute_wait
from octodot.models import (
    ActivityRecord,
    Binding,
    SessionRecord,
    TransportOutcome,
)
from octodot.reads import ReadService
from octodot.store import SQLiteStore
from octodot.transport import FakeClock, FixtureTransport


class TestJobsS07T03(unittest.TestCase):
    """S07-T03 test suite for late artifact collection and publication watches."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.store = SQLiteStore(self.test_dir)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s07_t03_terminal_scan_picks_up_late_artifacts(self) -> None:
        """S07-T03: Final terminal scan picks up late artifacts upon reaching terminal state."""
        session_json = json.dumps({
            "name": "sessions/s_term_1",
            "id": "s_term_1",
            "state": "COMPLETED",
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
        # Activities contain late artifacts (e.g. export patch, git commit, summary artifact)
        activities_json = json.dumps({
            "activities": [
                {
                    "name": "sessions/s_term_1/activities/act_art_1",
                    "type": "artifactExported",
                    "artifact_url": "https://example.com/artifacts/patch.diff",
                },
                {
                    "name": "sessions/s_term_1/activities/act_pr_1",
                    "type": "pullRequestCreated",
                    "pr_url": "https://github.com/OWNER/REPO/pull/42",
                },
            ]
        })

        transport = FixtureTransport()
        transport.set_response("GET", "/v1alpha/sources/github/OWNER/REPO", TransportOutcome(200, sources_json))
        transport.set_response("GET", "/v1alpha/sessions/s_term_1", TransportOutcome(200, session_json))
        transport.set_response("GET", "/v1alpha/sessions/s_term_1/activities", TransportOutcome(200, activities_json))

        client = JulesClient(transport, clock=self.clock)
        read_service = ReadService(client, store=self.store, clock=self.clock)

        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session="sessions/s_term_1",
        )

        # Execute wait for all_terminal
        result = execute_wait(
            predicate="all_terminal",
            timeout_seconds=10.0,
            selection=[binding],
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            max_iterations=5,
        )

        self.assertTrue(result.predicate_matched)
        assert result.job_id is not None
        job_data = self.store.load_job(result.job_id)
        assert job_data is not None

        # Verify that late artifacts were collected and stored in job details
        late_arts = job_data["details"].get("late_artifacts", [])
        self.assertGreater(len(late_arts), 0)
        # PR activity was picked up
        pr_items = [item for item in late_arts if item.get("type") == "pullRequestCreated" or "pr" in str(item).lower()]
        self.assertGreater(len(pr_items), 0)

    def test_s07_t03_all_terminal_is_not_publication_verified(self) -> None:
        """S07-T03: all_terminal is not publication verified; publication watch retains requested predicate."""
        # Session is terminal (COMPLETED), but activities contain NO publication or PR evidence
        session_json = json.dumps({
            "name": "sessions/s_completed_no_pub",
            "id": "s_completed_no_pub",
            "state": "COMPLETED",
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
        # Activities have only internal progress, no publication
        activities_json = json.dumps({
            "activities": [
                {
                    "name": "sessions/s_completed_no_pub/activities/act_1",
                    "type": "taskFinished",
                }
            ]
        })

        transport = FixtureTransport()
        transport.set_response("GET", "/v1alpha/sources/github/OWNER/REPO", TransportOutcome(200, sources_json))
        transport.set_response("GET", "/v1alpha/sessions/s_completed_no_pub", TransportOutcome(200, session_json))
        transport.set_response("GET", "/v1alpha/sessions/s_completed_no_pub/activities", TransportOutcome(200, activities_json))

        client = JulesClient(transport, clock=self.clock)
        read_service = ReadService(client, store=self.store, clock=self.clock)

        binding = Binding(
            profile="default",
            profile_epoch=0,
            source="sources/github/OWNER/REPO",
            repository="OWNER/REPO",
            starting_branch="feature/example",
            session="sessions/s_completed_no_pub",
        )

        # 1. Plain all_terminal matches because lifecycle is terminal
        matched_plain, _ = check_all_terminal_predicate(
            selection=[binding],
            read_service=read_service,
            publication_watch=False,
        )
        self.assertTrue(matched_plain)

        # 2. But publication watch MUST NOT match (retains requested predicate)
        matched_pub, _ = check_all_terminal_predicate(
            selection=[binding],
            read_service=read_service,
            publication_watch=True,
        )
        self.assertFalse(matched_pub)

        # 3. Via execute_wait with publication_watch=True: yields waiting, does not falsely claim OK
        res_pub_watch = execute_wait(
            predicate="all_terminal",
            timeout_seconds=5.0,
            selection=[binding],
            store=self.store,
            read_service=read_service,
            clock=self.clock,
            publication_watch=True,
            max_iterations=2,
        )
        self.assertFalse(res_pub_watch.predicate_matched)
        assert res_pub_watch.job_id is not None
        job_record = self.store.load_job(res_pub_watch.job_id)
        assert job_record is not None
        self.assertEqual(job_record["status"], "waiting")
        self.assertTrue(job_record["details"]["publication_watch"])


if __name__ == "__main__":
    unittest.main()
