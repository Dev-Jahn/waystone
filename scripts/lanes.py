#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Parallel-lane integrity check for round fan-out.

When a round's tasks are implemented on independent branches ("lanes"), each task may carry a
`lane:` manifest declaring the branch and the base SHA it was cut from. Before integrating,
verify every lane branch actually CONTAINS its declared base SHA — the correct invariant is
containment of the recorded base, NOT descent from the current integration tip (which moves as
sibling lanes merge and would false-fail healthy lanes).

  lane:
    branch: feat/foo
    base_sha: <sha recorded when the lane was created>   # = the dependency's result if depends_on
    depends_on: [feat/bar]   # optional

Usage (also `waystone lanes verify`): lanes.py verify [root]   exit 0 ok, 3 if any lane fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import find_project_root, git_rc, is_ancestor, load_tasks  # noqa: E402


def check_lane(root: Path, task_id: str, lane: dict) -> list[str]:
    """Return a list of failure strings for one lane (empty = ok)."""
    fails = []
    branch = lane.get("branch")
    base = lane.get("base_sha")
    if not isinstance(branch, str) or not branch:
        return [f"{task_id}: lane.branch missing"]
    if not isinstance(base, str) or not base:
        return [f"{task_id}: lane.base_sha missing"]
    rc, _, _ = git_rc(root, "rev-parse", "--verify", f"{branch}^{{commit}}")
    if rc != 0:
        return [f"{task_id}: lane branch {branch!r} does not exist"]
    rc, _, _ = git_rc(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    if rc != 0:
        return [f"{task_id}: lane.base_sha {base[:12]} is not a known commit"]
    if not is_ancestor(root, base, branch):
        fails.append(f"{task_id}: branch {branch!r} does NOT contain its base_sha {base[:12]} "
                     f"(was it cut from a different base, or rebased away?)")
    return fails


def verify(root: Path, round_id: str | None = None) -> int:
    data = load_tasks(root)
    # only verify lanes still in flight — a merged-and-deleted historical lane (done/dropped)
    # must not fail the current round forever. Optionally scope to one round.
    lanes = [(t["id"], t["lane"]) for t in data.get("tasks", [])
             if isinstance(t, dict) and isinstance(t.get("lane"), dict)
             and t.get("status") not in ("done", "dropped")
             and (round_id is None or t.get("round") == round_id)]
    if not lanes:
        print("lanes: no in-flight lane manifests to verify"
              + (f" for round {round_id}" if round_id else ""))
        return 0
    all_fails = []
    for tid, lane in lanes:
        all_fails += check_lane(root, tid, lane)
    if all_fails:
        print(f"lanes: {len(all_fails)} problem(s) across {len(lanes)} lane(s):", file=sys.stderr)
        for f in all_fails:
            print(f"  ✗ {f}", file=sys.stderr)
        return 3
    print(f"lanes: all {len(lanes)} lane(s) contain their declared base_sha")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "verify":
        print(__doc__, file=sys.stderr)
        return 1
    rest = argv[1:]
    round_id = rest[rest.index("--round") + 1] if "--round" in rest and rest.index("--round") < len(rest) - 1 else None
    positional = [a for i, a in enumerate(rest) if not a.startswith("--") and (i == 0 or rest[i - 1] != "--round")]
    root = Path(positional[0]).resolve() if positional else find_project_root(Path.cwd())
    if root is None:
        print("lanes: no initialized project", file=sys.stderr)
        return 1
    return verify(root, round_id)


if __name__ == "__main__":
    sys.exit(main())
