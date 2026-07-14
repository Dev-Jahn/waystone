#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unified front door for waystone scripts: `waystone <group> <args>`.

Groups:
  validate [tasks.yaml]              validate the task registry
  task     list|show|add|set|drop|archive ...  structured registry access (don't read/edit it raw)
  roadmap  [root]                    regenerate ROADMAP.md
  ssot     split|digest|check [root] SSOT generated views
  status   [--project N]             cross-project dashboard
  remote   verify|drift [root]       is HEAD pushed / how far behind
  review   freeze|status|ingest ...  SHA-bound review cycles (PR mode); ingest = byte-exact reply copy
  approve  --pr N --sha X            SHA-bound human approval
  round    merge --pr N ...          deterministic merge guard
  improve  trace|reviews|evidence|audit|decide ...  project logs + reviews + task-id evidence / decisions
  delegate run|status|show|verify|apply|discard ...  worktree runner + independent verifier evidence
  overlay  add|list|show|promote|demote|suspend|retire|replay ...  project-local adaptive warn deltas
  check    [--root DIR]               evaluate active overlay deltas at an explicit boundary (never blocks)

Existing hook/skill call sites that invoke sibling scripts directly keep working; this is an
additive convenience front door (GPT review: consolidate under one `waystone` CLI).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def _run_module_main(modname: str, argv: list[str]) -> int:
    """Invoke a sibling module's main() that reads sys.argv (legacy scripts)."""
    sys.argv = [modname, *argv]
    ns = runpy.run_path(str(HERE / f"{modname}.py"), run_name="__waystone_dispatch__")
    return int(ns["main"]() or 0)


def main(argv: list[str]) -> int:
    common.migrate_home_data()
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    group, rest = argv[0], argv[1:]

    # new-style modules expose main(argv)
    if group == "task":
        import tasks
        return tasks.main(rest)
    if group == "review":
        import review
        return review.main(rest)
    if group == "improve":
        import improve
        return improve.main(rest)
    if group == "delegate":
        import delegate
        return delegate.main(rest)
    if group == "overlay":
        import overlay
        return overlay.main(rest)
    if group == "check":
        import overlay
        return overlay.main(["check", *rest])
    if group == "remote":
        import importlib
        mod = importlib.import_module("remote")
        return mod.main(rest)
    if group == "approve":
        import merge
        return merge.main(["approve", *rest])
    if group == "round":
        if rest and rest[0] == "merge":
            import merge
            return merge.main(["merge", *rest[1:]])
        if rest and rest[0] == "close":
            return _run_module_main("round", rest)
        print("waystone round: expected 'close' or 'merge'", file=sys.stderr)
        return 1
    if group == "lanes":
        return _run_module_main("lanes", rest)
    if group == "resume":
        return _run_module_main("resume", rest)

    # legacy modules with main() reading sys.argv
    legacy = {"validate": "validate", "roadmap": "roadmap",
              "ssot": "ssot", "status": "dashboard"}
    if group in legacy:
        return _run_module_main(legacy[group], rest)

    print(f"waystone: unknown group {group!r}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
