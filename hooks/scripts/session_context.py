#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""SessionStart hook body: emit additionalContext (SSOT digest + active tasks + branch).

Called by session_context.sh with the project root as argv[1]; hook JSON on stdin (unused
beyond what the wrapper extracted). Output is capped to keep per-session token cost low.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from jw_common import git_branch_info, load_config, load_tasks, next_actionable  # noqa: E402

MAX_CHARS = 8000
MAX_TASK_LINES = 8


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    try:
        cfg = load_config(root)
        data = load_tasks(root)
    except Exception as e:  # malformed config must not break session start
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"[jahns-workflow] config/tasks unreadable: {e}",
        }}))
        return 0

    g = git_branch_info(root)
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    done = sum(1 for t in tasks if t.get("status") == "done")
    active = [t for t in tasks if t.get("status") == "active"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    decisions = [t for t in tasks if t.get("id", "").startswith("decision/") and t.get("status") not in ("done", "dropped")]
    rounds = sorted({t["round"] for t in active if t.get("round")})

    lines = [
        f"[jahns-workflow] project: {data.get('project', root.name)} | branch: {g['branch']}"
        f" ({'dirty +' + str(g['dirty']) if g['dirty'] else 'clean'}) | tasks: {done}/{len(tasks)} done",
    ]
    if rounds:
        lines.append(f"active round: {', '.join(rounds)}")
    for label, group in (("active", active), ("blocked", blocked), ("pending decision", decisions)):
        for t in group[:MAX_TASK_LINES]:
            lines.append(f"  {label}: {t['id']} — {t.get('title', '')}")
    nxt = next_actionable(data, cap=5)
    if nxt:
        lines.append("next actionable (deps satisfied):")
        for tid, title in nxt:
            lines.append(f"  → {tid} — {title}")
    lines.append(f"Task registry: tasks.yaml | Roadmap: ROADMAP.md | Conventions: see CLAUDE.md workflow section")

    digest = root / cfg["generated_dir"] / "DIGEST.md"
    if digest.is_file():
        lines.append("")
        lines.append(digest.read_text(encoding="utf-8").rstrip())
    elif cfg.get("ssot"):
        lines.append(f"SSOT: {cfg['ssot']} (no digest generated yet — run /jahns-workflow:round or jw_ssot.py digest)")

    ctx = "\n".join(lines)
    if len(ctx) > MAX_CHARS:
        ctx = ctx[:MAX_CHARS] + "\n…[truncated by jahns-workflow cap]"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
