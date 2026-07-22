#!/usr/bin/env python3
"""Focused advisory-improvement contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import improve


class ImproveTests(unittest.TestCase):
    def test_improve_reads_the_canonical_status_projection(self):
        source = Path(improve.__file__).read_text(encoding="utf-8")
        self.assertIn("project_status_projection", source)
        self.assertIn("project_status_json", source)

    def test_improve_has_no_workflow_or_task_authority(self):
        source = Path(improve.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import delegate", source)
        self.assertNotIn("import round", source)
        self.assertNotIn("review.ingest", source)
        self.assertNotIn("tasks.create", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
