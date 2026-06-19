"""Shared helpers for jahns-workflow scripts (imported by sibling scripts)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

CONFIG_NAME = ".jahns-workflow.yml"
TASKS_NAME = "tasks.yaml"
REGISTRY_PATH = Path.home() / ".claude" / "jahns-workflow" / "projects.json"

TASK_TYPES = ("feat", "fix", "perf", "gate", "spike", "decision", "docs", "chore")
TASK_STATUSES = ("pending", "active", "blocked", "done", "dropped")
MILESTONE_STATUSES = ("pending", "active", "done")
SEVERITIES = ("blocker", "major", "minor")

TASK_ID_RE = re.compile(r"^(?:%s)/[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$" % "|".join(TASK_TYPES))
MILESTONE_ID_RE = re.compile(r"^M[1-9][0-9]*$")
ROUND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$")


def find_project_root(start: Path) -> Path | None:
    """Walk upward from `start` to find the directory containing .jahns-workflow.yml."""
    cur = start.resolve()
    for p in (cur, *cur.parents):
        if (p / CONFIG_NAME).is_file():
            return p
    return None


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(root: Path) -> dict:
    cfg = load_yaml(root / CONFIG_NAME) or {}
    cfg.setdefault("progress", "PROGRESS.md")
    cfg.setdefault("adr_dir", "docs/adr")
    cfg.setdefault("reviews_dir", "docs/reviews")
    cfg.setdefault("progress_archive_dir", "docs/progress")
    cfg.setdefault("generated_dir", "docs/ssot")
    cfg.setdefault("digest_max_lines", 150)
    gen = Path(cfg["generated_dir"])
    if gen.is_absolute() or ".." in gen.parts:
        raise ValueError(f"generated_dir must be a relative path inside the repo: {cfg['generated_dir']!r}")
    rv = cfg.setdefault("review", {})
    if not isinstance(rv, dict):
        raise ValueError("review: must be a mapping (mode/reviewers/require_ci)")
    rv.setdefault("mode", "packet")  # packet | pr
    rv.setdefault("reviewers", ["codex", "gpt-5.5-pro"])
    rv.setdefault("require_ci", False)
    if rv["mode"] not in ("packet", "pr"):
        raise ValueError(f"review.mode must be 'packet' or 'pr', got {rv['mode']!r}")
    return cfg


def git_rc(root: Path, *args: str) -> tuple[int, str, str]:
    """Run git; return (returncode, stdout, stderr). Distinguishes failure from empty output."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return (127, "", str(e))
    return (out.returncode, out.stdout.strip(), out.stderr.strip())


def git_full_sha(root: Path, ref: str = "HEAD") -> str | None:
    """Full 40-char commit sha for `ref`, or None if it does not resolve."""
    rc, out, _ = git_rc(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return out if rc == 0 and out else None


def upstream_ref(root: Path) -> str | None:
    """The tracked upstream (e.g. 'origin/main') of the current branch, or None."""
    rc, out, _ = git_rc(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    return out if rc == 0 and out else None


def is_ancestor(root: Path, a: str, b: str) -> bool:
    """True iff commit `a` is an ancestor of (i.e. contained in) commit `b`."""
    rc, _, _ = git_rc(root, "merge-base", "--is-ancestor", a, b)
    return rc == 0


def head_pushed(root: Path, fetch: bool = True) -> tuple[bool, dict]:
    """Is the current HEAD contained in its tracked upstream (i.e. actually pushed)?
    Returns (pushed, info). info carries upstream/head/behind for reporting."""
    up = upstream_ref(root)
    if not up:
        return (False, {"reason": "no upstream tracking branch"})
    if fetch:
        git_rc(root, "fetch", "--quiet", up.split("/", 1)[0])
    head = git_full_sha(root, "HEAD")
    pushed = is_ancestor(root, "HEAD", up)
    rc, out, _ = git_rc(root, "rev-list", "--count", f"HEAD..{up}")
    behind = int(out) if rc == 0 and out.isdigit() else None
    return (pushed, {"upstream": up, "head": head, "behind": behind})


def load_tasks(root: Path) -> dict:
    data = load_yaml(root / TASKS_NAME)
    return data if isinstance(data, dict) else {}


def git(root: Path, *args: str) -> str:
    """Run a git command in `root`; return stdout or '' on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_branch_info(root: Path) -> dict:
    branch = git(root, "branch", "--show-current") or "(detached)"
    dirty = len([ln for ln in git(root, "status", "--porcelain").splitlines() if ln])
    ahead_behind = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    behind, ahead = (ahead_behind.split() + ["", ""])[:2] if ahead_behind else ("?", "?")
    return {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind}


def slugify(text: str, max_len: int = 40) -> str:
    """Filename slug for generated SSOT sections. Keeps Hangul (Korean headings stay
    readable); task IDs are NOT slugified with this — their grammar stays ASCII."""
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "section"
