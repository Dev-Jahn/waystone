#!/usr/bin/env python3
"""Focused objective-first status projection contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

from waystone.runs.observe import render_project_status


class RunObserveTests(unittest.TestCase):
    def test_status_renderer_names_objective_and_delta_before_audit(self):
        source = Path(render_project_status.__code__.co_filename).read_text(encoding="utf-8")
        self.assertLess(source.index("Current objective"), source.index("Audit:"))
        self.assertIn("Last outcome delta", source)

    def test_status_does_not_use_task_count_as_progress(self):
        source = Path(render_project_status.__code__.co_filename).read_text(encoding="utf-8")
        self.assertNotIn("tasks completed", source.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
