#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Write an objective-first re-entry snapshot before context is summarized.

Usage (also `waystone resume`): resume.py [root]   |   resume.py --path [root]
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    WorkflowError, ensure_project_state_dir, find_project_root, git_branch_info,
    git_full_sha, resume_path, start_here_path, write_text_atomic,
)
from waystone.project.context import resolve_project_context  # noqa: E402
from waystone.runs.engine import ReadOnlyStoreUnavailable, open_read_only_store  # noqa: E402
from waystone.runs.observe import project_status_projection  # noqa: E402


def snapshot(root: Path) -> str:
    g = git_branch_info(root)
    context = resolve_project_context(root)
    try:
        with open_read_only_store(context.canonical_root) as store:
            status = project_status_projection(context.canonical_root, store)
    except ReadOnlyStoreUnavailable:
        status = project_status_projection(context.canonical_root)
    objective = status.objective_ref
    active = status.active_run or {}
    delta = status.last_delta or {}
    waiting = active.get("state") == "waiting_context" if isinstance(active, dict) else False
    L = [f"captured_head: {git_full_sha(root, 'HEAD') or 'none'}",
         f"captured_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
         f"[waystone resume] objective re-entry pointer",
         f"branch: {g['branch']} ({'dirty +' + str(g['dirty']) if g['dirty'] else 'clean'})",
         f"objective: {objective!r}",
         f"stage: {active.get('lifecycle_stage') or 'none'}",
         f"waiting-context: {'yes' if waiting else 'no'}",
         f"last-delta: {delta!r}",
         "Authority: status read model (objective, stage, waiting context, OutcomeDelta)."]
    return "\n".join(L) + "\n"


def write(root: Path) -> int:
    ensure_project_state_dir(root)  # the gated primitive — never a bare mkdir under root
    write_text_atomic(resume_path(root), snapshot(root))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    want_path = "--path" in argv
    want_start_here = "--start-here-path" in argv
    positional = [a for a in argv if not a.startswith("--")]
    root = Path(positional[0]).resolve() if positional else find_project_root(Path.cwd())
    if root is None:
        return 0  # silent no-op outside a project (hook fast-path safety)
    try:
        if want_start_here:
            ensure_project_state_dir(root)  # so the model can Write to it directly
            print(start_here_path(root))
            return 0
        if want_path:
            print(resume_path(root))  # pure read — no state creation
            return 0
        return write(root)
    except WorkflowError as e:
        # An explicit positional root is a caller assertion — refuse it loudly instead of
        # scaffolding .waystone at an arbitrary path.
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
