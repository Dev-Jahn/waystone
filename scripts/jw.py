#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unified front door for jahns-workflow scripts: `jw <group> <args>`.

Groups:
  validate [tasks.yaml]              validate the task registry
  roadmap  [root]                    regenerate ROADMAP.md
  ssot     split|digest|check [root] SSOT generated views
  status   [--project N]             cross-project dashboard
  remote   verify|drift [root]       is HEAD pushed / how far behind
  review   freeze|status|ingest ...  SHA-bound review cycles (PR mode); ingest = byte-exact reply copy
  review   bundle --round ID|--pr N  build a jahns-review-bundle/v1 zip for the web reviewer
  reviewer kit [--out dir]           render the ChatGPT reviewer kit (static protocol templates)
  approve  --pr N --sha X            SHA-bound human approval
  round    merge --pr N ...          deterministic merge guard

Existing hook/skill call sites that invoke jw_<name>.py directly keep working; this is an
additive convenience front door (GPT review: consolidate under one `jw` CLI).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _run_module_main(modname: str, argv: list[str]) -> int:
    """Invoke a sibling module's main() that reads sys.argv (legacy scripts)."""
    sys.argv = [modname, *argv]
    ns = runpy.run_path(str(HERE / f"{modname}.py"), run_name="__jw_dispatch__")
    return int(ns["main"]() or 0)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    group, rest = argv[0], argv[1:]

    # new-style modules expose main(argv)
    if group == "reviewer":
        import jw_bundle
        return jw_bundle.main(["kit", *rest[1:]] if rest and rest[0] == "kit" else rest)
    if group == "review":
        if rest and rest[0] == "bundle":
            import jw_bundle
            return jw_bundle.main(["bundle", *rest[1:]])
        import jw_review
        return jw_review.main(rest)
    if group == "remote":
        import importlib
        mod = importlib.import_module("jw_remote")
        return mod.main(rest)
    if group == "approve":
        import jw_merge
        return jw_merge.main(["approve", *rest])
    if group == "round":
        if rest and rest[0] == "merge":
            import jw_merge
            return jw_merge.main(["merge", *rest[1:]])
        if rest and rest[0] == "close":
            return _run_module_main("jw_round", rest)
        print("jw round: expected 'close' or 'merge'", file=sys.stderr)
        return 1
    if group == "lanes":
        return _run_module_main("jw_lanes", rest)
    if group == "resume":
        return _run_module_main("jw_resume", rest)

    # legacy modules with main() reading sys.argv
    legacy = {"validate": "jw_validate", "roadmap": "jw_roadmap",
              "ssot": "jw_ssot", "status": "jw_dashboard"}
    if group in legacy:
        return _run_module_main(legacy[group], rest)

    print(f"jw: unknown group {group!r}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
