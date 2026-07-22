#!/usr/bin/env python3
"""Focused release-surface contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class ReleaseTests(unittest.TestCase):
    def test_launcher_uses_canonical_cli(self):
        path = Path(__file__).resolve().parents[2] / "scripts/waystone.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("waystone.cli.main", source)
        self.assertNotIn("scripts.round", source)
        self.assertNotIn("scripts.delegate", source)

    def test_retired_groups_are_not_aliased(self):
        source = (Path(__file__).resolve().parents[2] / "waystone/cli/main.py").read_text(encoding="utf-8")
        self.assertIn("legacy group", source)
        self.assertNotIn("_run_module_main", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
