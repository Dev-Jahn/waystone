#!/usr/bin/env python3
"""Focused review protocol contracts; legacy round gates are retired."""
from __future__ import annotations

from support import *  # noqa: F401,F403

from waystone.cli import review_group
from waystone.reviews import findings


class ReviewProtocolTests(unittest.TestCase):
    def test_review_group_exposes_only_claim_validation_disposition_materialization(self):
        source = Path(review_group.__file__).read_text(encoding="utf-8")
        for command in ("ingest", "validate", "disposition", "materialize"):
            self.assertIn(f'add_parser("{command}")', source)
        self.assertNotIn('add_parser("triage")', source)
        self.assertNotIn('add_parser("freeze")', source)

    def test_finding_schema_keeps_claim_validation_and_disposition_separate(self):
        self.assertNotEqual(findings.CLAIM_SCHEMA, findings.VALIDATION_SCHEMA)
        self.assertNotEqual(findings.VALIDATION_SCHEMA, findings.DISPOSITION_SCHEMA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
