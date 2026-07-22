#!/usr/bin/env python3
"""Observation-only advisory boundary; no finding or legacy workflow enforcement."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import WorkflowError, find_project_root  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "check":
        print("waystone overlay: only the observation-only check surface is available", file=sys.stderr)
        return 1
    root = find_project_root(Path.cwd())
    if root is None:
        print("waystone overlay check: no initialized project", file=sys.stderr)
        return 1
    print("waystone overlay check: advisory-only; no active enforcement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
