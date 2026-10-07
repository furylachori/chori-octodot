"""Tests for S03-T06: Incomplete scan evidence retention and write eligibility gating.

An incomplete scan can retain partial evidence but cannot advance a complete checkpoint,
establish absence or supply write-eligible context.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.errors import ErrorCode, StateStoreError
from octodot.models import Binding, CandidateBundle, Coverage, Observation
from octodot.store import SQLiteStore


class TestStoreS03T06IncompleteScans(unittest.TestCase):
    """S03-T06 Incomplete scan tests: evidence retention, checkpoint advance gating, write eligibility."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_s03_t06_incomplete_scan_retains_partial_evidence(self) -> None:
        """S03-T06: An incomplete scan retains partial evidence in the store."""
        store = SQLiteStore(self.test_dir)
        try:
            scan_id = "scan_incomplete_01"
            store.begin_scan(scan_id, profile="default", details={"source": "test"})

            # Store partial observations during incomplete scan
            obs1 = Observation(
                binding=Binding(
                    profile="default",
                    profile_epoch=1,
                    source="sources/github/OWNER/REPO",
                    repository="OWNER/REPO",
                    starting_branch="feature/example",
                ),
                sources=({"name": "sources/github/OWNER/REPO"},),
                coverage=Coverage(complete=False, pages=1, items=5, reasons=("page_cap_hit",)),
                metadata=(("stage", "partial_collect"),),
            )
            store.save_observation(obs1, scan_id=scan_id, observation_id="obs_01")

            # Commit scan as incomplete
            store.commit_scan(
                scan_id=scan_id,
                complete=False,
                coverage=Coverage(complete=False, pages=1, items=5),
            )

            # Verify scan is marked incomplete
            self.assertFalse(store.is_scan_complete(scan_id))
            scan_data = store.get_scan(scan_id)
            self.assertIsNotNone(scan_data)
            self.assertFalse(scan_data["complete"])

            # Verify partial observations are retained and retrievable
            observations = store.get_observations(scan_id=scan_id)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].binding.repository, "OWNER/REPO")
            self.assertFalse(observations[0].coverage.complete)
        finally:
            store.close()

    def test_s03_t06_incomplete_scan_cannot_advance_checkpoint(self) -> None:
        """S03-T06: An incomplete scan cannot advance a checkpoint; raises PARTIAL_COVERAGE."""
        store = SQLiteStore(self.test_dir)
        try:
            scan_id = "scan_incomplete_02"
            store.begin_scan(scan_id, profile="default")
            store.commit_scan(scan_id, complete=False)

            # Advance checkpoint on incomplete scan must fail
            with self.assertRaises(StateStoreError) as ctx:
                store.advance_checkpoint(
                    checkpoint_id="cp_invalid",
                    profile="default",
                    scan_id=scan_id,
                )
            self.assertEqual(ctx.exception.code, ErrorCode.PARTIAL_COVERAGE)
            self.assertIn("incomplete scan", ctx.exception.message)

            # Checkpoint was not created
            self.assertIsNone(store.get_checkpoint("default"))
        finally:
            store.close()

    def test_s03_t06_incomplete_scan_cannot_establish_absence_or_write_eligibility(self) -> None:
        """S03-T06: Incomplete scan cannot establish absence or supply write-eligible context."""
        store = SQLiteStore(self.test_dir)
        try:
            scan_id = "scan_incomplete_03"
            store.begin_scan(scan_id, profile="default")
            store.commit_scan(
                scan_id,
                complete=False,
                coverage=Coverage(complete=False, reasons=("interrupted",)),
            )

            # Cannot establish absence
            self.assertFalse(store.can_establish_absence(scan_id))
            # Cannot supply write-eligible context
            self.assertFalse(store.is_scan_write_eligible(scan_id))
        finally:
            store.close()

    def test_s03_t06_complete_scan_advances_checkpoint_and_enables_writes(self) -> None:
        """S03-T06: Complete scan establishes absence, supplies write eligibility, and advances checkpoint."""
        store = SQLiteStore(self.test_dir)
        try:
            scan_id = "scan_complete_04"
            store.begin_scan(scan_id, profile="default")
            store.commit_scan(
                scan_id,
                complete=True,
                coverage=Coverage(complete=True, pages=2, items=20),
            )

            # Scan is complete
            self.assertTrue(store.is_scan_complete(scan_id))
            self.assertTrue(store.can_establish_absence(scan_id))
            self.assertTrue(store.is_scan_write_eligible(scan_id))

            # Advancing checkpoint succeeds
            store.advance_checkpoint(
                checkpoint_id="cp_valid_04",
                profile="default",
                scan_id=scan_id,
                position="token_xyz",
            )

            cp = store.get_checkpoint("default")
            self.assertIsNotNone(cp)
            self.assertEqual(cp["checkpoint_id"], "cp_valid_04")
            self.assertEqual(cp["position"], "token_xyz")
        finally:
            store.close()
