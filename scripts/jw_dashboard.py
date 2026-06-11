#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Cross-project terminal dashboard for all jahns-workflow projects.

Usage: jw_dashboard.py [--project NAME]
Reads the global registry (~/.claude/jahns-workflow/projects.json). Entry forms:
  { "name": "...", "path": "/abs/local/clone" }   — local: git state + tasks.yaml from disk
  { "name": "...", "repo": "owner/name" }          — remote: tasks.yaml fetched via `gh api`
  both                                             — local preferred while the path exists
Deterministic, no LLM involved.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from jw_common import REGISTRY_PATH, git_branch_info, load_tasks  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
BLUE, RED, GREEN, YELLOW = "\033[34m", "\033[31m", "\033[32m", "\033[33m"


def c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def render_tasks(data: dict) -> None:
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    if not tasks:
        print(c(DIM, "    (no tasks registered)"))
        return
    done = sum(1 for t in tasks if t.get("status") == "done")
    rounds = sorted({t["round"] for t in tasks if t.get("round") and t.get("status") == "active"})
    latest = rounds[-1] if rounds else max((t.get("round") or "" for t in tasks), default="") or "—"
    bar_n = round(20 * done / len(tasks))
    bar = "█" * bar_n + "░" * (20 - bar_n)
    print(f"    {bar} {done}/{len(tasks)} done   round: {latest}")

    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        if t.get("status") == "active":
            print(f"    {c(BLUE, '● active ')} {c(BOLD, t['id'])} — {t.get('title', '')}")
    for t in tasks:
        if t.get("status") == "blocked":
            unmet = [d for d in t.get("deps", []) if by_id.get(d, {}).get("status") != "done"]
            why = f"  {c(DIM, 'waiting: ' + ', '.join(unmet))}" if unmet else ""
            print(f"    {c(RED, '⛔ blocked')} {c(BOLD, t['id'])} — {t.get('title', '')}{why}")
    pend = sum(1 for t in tasks if t.get("status") == "pending")
    if pend:
        print(c(DIM, f"    … {pend} pending"))


def show_local(name: str, path: Path) -> None:
    g = git_branch_info(path)
    dirty = c(YELLOW, f"±{g['dirty']}") if g["dirty"] else c(GREEN, "clean")
    sync = f"↑{g['ahead']}↓{g['behind']}" if g["ahead"] != "?" else c(DIM, "no upstream")
    print(f"{c(BOLD, '■ ' + name)}  ⎇ {c(BLUE, g['branch'])} {dirty} {sync}  {c(DIM, str(path))}")
    render_tasks(load_tasks(path))


def gh_api(*args: str) -> str | None:
    try:
        out = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def show_remote(name: str, repo: str) -> None:
    meta_raw = gh_api(f"repos/{repo}", "--jq", "{branch: .default_branch, pushed: .pushed_at}")
    if meta_raw is None:
        print(f"{c(BOLD, '■ ' + name)}  {c(RED, '✗ gh fetch failed')} {c(DIM, repo)}")
        return
    meta = json.loads(meta_raw)
    pushed = (meta.get("pushed") or "")[:10]
    print(f"{c(BOLD, '■ ' + name)}  ⎇ {c(BLUE, meta.get('branch', '?'))} "
          f"{c(DIM, f'remote {repo}, pushed {pushed}')}")
    content = gh_api(f"repos/{repo}/contents/tasks.yaml", "--jq", ".content")
    if content is None:
        print(c(DIM, "    (no tasks.yaml on remote default branch)"))
        return
    try:
        data = yaml.safe_load(base64.b64decode(content))
    except (ValueError, yaml.YAMLError) as e:
        print(c(RED, f"    remote tasks.yaml unreadable: {e}"))
        return
    render_tasks(data if isinstance(data, dict) else {})


def show_entry(entry: dict) -> None:
    name = entry.get("name", "?")
    path = entry.get("path")
    repo = entry.get("repo")
    if path and Path(path).expanduser().is_dir():
        show_local(name, Path(path).expanduser())
    elif repo:
        show_remote(name, repo)
    elif path:
        print(f"{c(BOLD, '■ ' + name)}  {c(RED, '✗ path missing')} {c(DIM, str(path))}")
    else:
        print(f"{c(BOLD, '■ ' + name)}  {c(RED, '✗ entry has neither path nor repo')}")


def main() -> int:
    idx = sys.argv.index("--project") if "--project" in sys.argv else -1
    only = sys.argv[idx + 1] if 0 <= idx < len(sys.argv) - 1 else None
    if not REGISTRY_PATH.is_file():
        print("no projects registered yet — run /jahns-workflow:init in a project first")
        return 0
    reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    projects = reg.get("projects", [])
    if only:
        projects = [p for p in projects if p.get("name") == only]
    if not projects:
        print(f"no registered project matches {only!r}" if only else "registry is empty")
        return 0
    for i, p in enumerate(projects):
        if i:
            print()
        show_entry(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
