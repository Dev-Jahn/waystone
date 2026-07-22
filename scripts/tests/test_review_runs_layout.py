#!/usr/bin/env python3
"""Focused canonical review-run layout contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

from waystone.features import review_layout


class ReviewRunsLayoutTests(unittest.TestCase):
    def test_run_owner_is_uuid7_and_nested_findings_are_canonical(self):
        run_id = review_layout.new_run_id()
        self.assertEqual(review_layout.require_uuid7(run_id), run_id)
        root = Path(tempfile.mkdtemp()) / "docs/reviews/runs"
        path = review_layout.canonical_finding_path(
            root, run_id, review_layout.new_run_id(), review_layout.FINDING_CLAIM)
        self.assertIn(f"runs/{run_id}/findings/", path.as_posix())

    def test_no_legacy_flat_reader_is_exposed(self):
        self.assertFalse(hasattr(review_layout, "read_legacy_artifact"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
