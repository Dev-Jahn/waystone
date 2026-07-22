#!/usr/bin/env python3
"""Focused execution-safety preflight contracts; assurance owns stage requirements."""
from __future__ import annotations

from support import *  # noqa: F401,F403

from waystone.runs import assurance, preflight


class RunPreflightTests(unittest.TestCase):
    def test_preflight_module_does_not_require_a_blanket_check_count(self):
        source = Path(preflight.__file__).read_text(encoding="utf-8")
        self.assertNotIn("at least one check", source.lower())
        self.assertNotIn("minimum_checks", source)

    def test_stage_assurance_is_compiled_by_assurance_module(self):
        self.assertTrue(hasattr(assurance, "compile_assurance_plan"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
