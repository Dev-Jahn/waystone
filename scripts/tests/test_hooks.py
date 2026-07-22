#!/usr/bin/env python3
"""Focused hook contracts for selected-work protection and objective-first context."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import importlib.util


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HookTests(unittest.TestCase):
    def test_tasks_guard_ignores_non_tasks_patch(self):
        module = _load("tasks_guard", SCRIPTS.parent / "hooks/scripts/tasks_guard.py")
        self.assertIsNone(module._canonical_tasks_path({"tool_name": "Write", "tool_input": {
            "file_path": "/tmp/other.yaml"}}))

    def test_session_context_has_only_objective_first_surface(self):
        source = (SCRIPTS.parent / "hooks/scripts/session_context.py").read_text(encoding="utf-8")
        self.assertIn("project_status_projection", source)
        self.assertIn("last_delta", source)
        self.assertNotIn("import delegate", source)
        self.assertNotIn("import review", source)
        self.assertNotIn("SSOT", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
