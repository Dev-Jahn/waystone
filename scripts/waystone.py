#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Compatibility adapter for the Waystone CLI composition root."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if __name__ == "waystone":
    __path__ = [str(REPO_ROOT / "waystone")]

from waystone.cli.main import __doc__, main, os  # noqa: E402, F401


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
