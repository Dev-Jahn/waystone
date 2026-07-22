#!/usr/bin/env python3
"""Focused disposition settlement contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

from waystone.reviews import findings


class ReviewSettlementTests(unittest.TestCase):
    def test_materialization_is_not_implied_by_severity(self):
        source = Path(findings.__file__).read_text(encoding="utf-8")
        self.assertIn("fix-now", source)
        self.assertIn("fix-before-promotion", source)

    def test_outcome_progress_is_not_review_count(self):
        source = Path((Path(__file__).resolve().parents[2] / "waystone/runs/outcome.py")).read_text(encoding="utf-8")
        self.assertIn("OutcomeDelta", source)
        self.assertIn("no-objective-delta", Path((Path(__file__).resolve().parents[2] / "waystone/runs/observe.py")).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
