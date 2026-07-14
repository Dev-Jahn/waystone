#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""PreToolUse hook body: redirect a raw `Read` of the canonical tasks.yaml to the `jw task` CLI.

A long-lived registry is thousands of lines; slurping it whole is wasteful. This denies the Read
(feeding a short redirect back to Claude, no user prompt) so the agent uses `jw task list`/`show`
instead. It only fires for the project's own tasks.yaml inside an initialized project; a same-named
file elsewhere, a different file, or a non-Read tool passes through untouched. Raw access still
exists as an escape hatch via the shell (`cat tasks.yaml`).

No third-party deps (inlines the project-root walk) so this hot Read-path hook resolves nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_NAME = ".jahns-workflow.yml"

REASON = (
    "tasks.yaml is the long machine-validated registry — read it through the CLI, not whole:\n"
    "  jw task list [--status S|--type T|--milestone M|--round R]   (compact view)\n"
    "  jw task show <id>                                            (one task's record)\n"
    "Mutate it the same way: jw task add/set/drop (validated, comment-preserving) — not Edit.\n"
    "(If you genuinely need the raw file, read it via the shell with `cat`.)"
)


def _find_project_root(start: Path) -> Path | None:
    """Walk upward from `start` to the directory holding .jahns-workflow.yml (mirrors common)."""
    cur = start.resolve()
    for p in (cur, *cur.parents):
        if (p / CONFIG_NAME).is_file():
            return p
    return None


def decide(payload: dict) -> dict | None:
    """The deny decision for a canonical-tasks.yaml Read, else None. Pure (only a root probe)."""
    if payload.get("tool_name") != "Read":
        return None
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    if not file_path:
        return None
    p = Path(file_path)
    if p.name != "tasks.yaml":
        return None
    root = _find_project_root(p.parent)
    # resolve BOTH sides so a symlinked tasks.yaml is still recognized as the canonical registry
    if root is None or (root / "tasks.yaml").resolve() != p.resolve():
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON,
    }}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    decision = decide(payload)
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
