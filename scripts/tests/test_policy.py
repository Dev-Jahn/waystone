#!/usr/bin/env python3
"""Focused policy and intent-promotion contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class PolicyTests(unittest.TestCase):
    def test_policy_schema_has_no_retired_workflow_rules(self):
        path = Path(__file__).resolve().parents[2] / "templates/project-policy-schema.json"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("round-close", source)
        self.assertNotIn("review-skipped-closes", source)

    def test_invariants_preserve_uncertainty_boundaries(self):
        source = (Path(__file__).resolve().parents[2] / "docs/invariants.md").read_text(encoding="utf-8")
        for transition in ("hypothesis → requirement", "finding → task", "probe → permanent test", "summary → owner authority"):
            self.assertIn(transition, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
