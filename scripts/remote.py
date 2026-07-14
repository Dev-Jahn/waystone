#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Remote reconciliation: is the local HEAD actually pushed, and how far behind is it.

Subcommands (also reachable as `jw remote <sub>`):
  verify [root]   exit 0 if HEAD is contained in its tracked upstream (pushed), else 3
  drift  [root]   print how many commits the local HEAD is behind upstream (informational)

Pure git, deterministic. Used by the round skill to refuse emitting a review packet that
points at an unpushed HEAD, and by the dashboard to surface remote drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import find_project_root, head_pushed  # noqa: E402


def _root(argv: list[str]) -> Path | None:
    if argv:
        return Path(argv[0]).resolve()
    return find_project_root(Path.cwd())


def verify(root: Path) -> int:
    pushed, info = head_pushed(root, fetch=True)
    if "reason" in info:
        print(f"remote: cannot verify — {info['reason']}", file=sys.stderr)
        return 3
    if pushed:
        behind = info.get("behind")
        tail = f" ({behind} behind {info['upstream']})" if behind else ""
        print(f"remote: HEAD {info['head'][:12]} is pushed to {info['upstream']}{tail}")
        return 0
    print(f"remote: HEAD {(info.get('head') or '?')[:12]} is NOT on {info['upstream']} — push before requesting review",
          file=sys.stderr)
    return 3


def drift(root: Path) -> int:
    pushed, info = head_pushed(root, fetch=True)
    if "reason" in info:
        print(f"remote: {info['reason']}")
        return 0
    behind = info.get("behind")
    state = "pushed" if pushed else "UNPUSHED"
    print(f"{state}; {behind if behind is not None else '?'} behind {info['upstream']}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("verify", "drift"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    root = _root(rest)
    if root is None:
        print("remote: no initialized project (missing .jahns-workflow.yml)", file=sys.stderr)
        return 1
    return {"verify": verify, "drift": drift}[sub](root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
