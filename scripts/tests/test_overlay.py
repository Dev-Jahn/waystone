#!/usr/bin/env python3
"""Focused observation-only overlay contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import overlay


class OverlayTests(unittest.TestCase):
    def test_overlay_is_observation_only(self):
        source = Path(overlay.__file__).read_text(encoding="utf-8")
        self.assertIn("observation-only", source)
        self.assertNotIn("import delegate", source)
        self.assertNotIn("round.close", source)
        self.assertNotIn("round-close-open-findings-v1", source)

    def test_overlay_cli_exposes_check_only(self):
        source = Path(overlay.__file__).read_text(encoding="utf-8")
        self.assertIn('argv[0] != "check"', source)
        self.assertNotIn('argv[0] == "apply"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
