#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Write a deterministic re-entry snapshot before the context window is summarized.

Closes the "update memory before compaction" loop the user used to run by hand every round.
The snapshot is a compact pointer (HEAD, branch, active round, active/blocked tasks, what to
pick up next) written to a plugin-local ephemeral file (NOT committed to the repo). The
SessionStart hook reads it back after a compaction/resume. Called by PreCompact / SessionEnd
hooks and at round close.

Usage (also `waystone resume`): resume.py [root]   |   resume.py --path [root]
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    find_project_root, git, git_branch_info, git_full_sha, load_tasks,
    next_actionable, resume_path, start_here_path,
)


def snapshot(root: Path) -> str:
    data = load_tasks(root)
    g = git_branch_info(root)
    head = git(root, "log", "-1", "--format=%h %s") or "(no commits)"
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    active = [t for t in tasks if t.get("status") == "active"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    rounds = sorted({t["round"] for t in active if t.get("round")})
    nxt = next_actionable(data, cap=6)

    L = [f"captured_head: {git_full_sha(root, 'HEAD') or 'none'}",
         f"captured_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
         f"[waystone resume] {data.get('project', root.name)} — re-entry pointer",
         f"branch: {g['branch']} ({'dirty +' + str(g['dirty']) if g['dirty'] else 'clean'}) | HEAD: {head}"]
    if rounds:
        L.append(f"active round: {', '.join(rounds)}")
    for t in active[:8]:
        L.append(f"  active: {t['id']} — {t.get('title', '')}")
    for t in blocked[:6]:
        L.append(f"  blocked: {t['id']} — {t.get('title', '')}")
    if nxt:
        L.append("next actionable (deps satisfied):")
        for tid, title in nxt:
            L.append(f"  → {tid} — {title}")
    L.append("Authoritative state: tasks.yaml + PROGRESS.md + ROADMAP.md (this is only a pointer).")
    return "\n".join(L) + "\n"


def write(root: Path) -> int:
    p = resume_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(snapshot(root), encoding="utf-8")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    want_path = "--path" in argv
    want_start_here = "--start-here-path" in argv
    positional = [a for a in argv if not a.startswith("--")]
    root = Path(positional[0]).resolve() if positional else find_project_root(Path.cwd())
    if root is None:
        return 0  # silent no-op outside a project (hook fast-path safety)
    if want_start_here:
        sh = start_here_path(root)
        sh.parent.mkdir(parents=True, exist_ok=True)  # so the model can Write to it directly
        print(sh)
        return 0
    if want_path:
        print(resume_path(root))
        return 0
    return write(root)


if __name__ == "__main__":
    sys.exit(main())
