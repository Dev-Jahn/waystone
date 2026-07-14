"""Shared helpers for waystone scripts (imported by sibling scripts)."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

CONFIG_NAME = ".waystone.yml"
LEGACY_CONFIG_NAME = ".jahns-workflow.yml"
TASKS_NAME = "tasks.yaml"


def machine_dir(home: Path | None = None) -> Path:
    """Waystone's host-neutral machine data root, optionally resolved under an injected home."""
    override = os.environ.get("WAYSTONE_HOME")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise WorkflowError(
                f"WAYSTONE_HOME must be an absolute path after user expansion, got {override!r}")
        return path
    return (Path.home() if home is None else Path(home)) / ".waystone"


def project_state_path(root: Path) -> Path:
    """Return the project-local state root without touching the filesystem."""
    return Path(root) / ".waystone"


def ensure_project_state_dir(root: Path) -> Path:
    """Create the project-local state root and restore its self-ignore file when needed."""
    state = project_state_path(root)
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != "*\n":
        ignore.write_text("*\n", encoding="utf-8")
    return state


def worktrees_cache_dir(home: Path | None = None) -> Path:
    return machine_dir(home) / "cache" / "worktrees"


def registry_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "projects.json"


def _legacy_claude_root(home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".claude" / "waystone"


def _legacy_codex_root(home: Path | None = None) -> Path:
    base_home = Path.home() if home is None else Path(home)
    codex_home = (Path(os.environ["CODEX_HOME"]).expanduser()
                  if os.environ.get("CODEX_HOME") else base_home / ".codex")
    return codex_home / "waystone"


def _legacy_data_dir(home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".claude" / "jahns-workflow"


def migrate_home_data(home: Path | None = None) -> Path:
    """Move the legacy data root once. A conflict is preserved and reported, never merged."""
    # C1 keeps the 0.8.x migration intact. C2 owns migration into the host-neutral storage model.
    if os.environ.get("WAYSTONE_HOST") == "codex":
        return _legacy_codex_root(home)
    new = _legacy_claude_root(home)
    old = _legacy_data_dir(home)
    if new.exists():
        if old.exists():
            print(
                f"waystone: legacy data dir {old} and new data dir {new} both exist; "
                f"using {new} and leaving {old} untouched",
                file=sys.stderr,
            )
        return new
    if old.exists():
        shutil.move(old, new)
    return new


class WorkflowError(Exception):
    """A recoverable workflow error raised by library helpers. Library code must raise this (an
    ordinary Exception, catchable by rollback logic) rather than calling sys.exit() — only CLI
    main() converts it to an exit code. (sys.exit raises SystemExit/BaseException, which slips past
    `except Exception` rollbacks.)"""
TASK_TYPES = ("feat", "fix", "perf", "gate", "spike", "decision", "docs", "chore")
TASK_STATUSES = ("pending", "active", "blocked", "done", "dropped")
MILESTONE_STATUSES = ("pending", "active", "done")
SEVERITIES = ("blocker", "major", "minor")

TASK_ID_RE = re.compile(r"^(?:%s)/[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$" % "|".join(TASK_TYPES))
MILESTONE_ID_RE = re.compile(r"^M[1-9][0-9]*$")
ROUND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$")


def find_project_root(start: Path) -> Path | None:
    """Walk upward from `start` to find either the current or legacy project config."""
    cur = start.resolve()
    for p in (cur, *cur.parents):
        if has_project_config(p):
            return p
    return None


def has_project_config(root: Path) -> bool:
    return any((root / name).is_file() for name in (CONFIG_NAME, LEGACY_CONFIG_NAME))


def _migrate_project_config(root: Path) -> Path:
    legacy = root / LEGACY_CONFIG_NAME
    current = root / CONFIG_NAME
    if current.exists():
        if legacy.exists():
            print(
                f"waystone: legacy config {legacy} and new config {current} both exist; "
                f"using {current} and leaving {legacy} untouched",
                file=sys.stderr,
            )
        return current
    if legacy.is_file():
        os.rename(legacy, current)
    return current


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_config(cfg: dict | None) -> dict:
    """Apply defaults + validation to a parsed config mapping (from disk OR from a PR head)."""
    cfg = dict(cfg or {})
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
        raise ValueError("review: must be a mapping (mode/reviewers/require_ci/approvers/operators)")
    rv.setdefault("mode", "packet")  # packet | pr
    rv.setdefault("reviewers", ["codex", "gpt-5.5-pro"])
    rv.setdefault("require_ci", False)
    rv.setdefault("approvers", [])  # extra trusted approver logins beyond the repo owner
    # GitHub actors trusted to POST cycle/result/findings markers (beyond the repo owner). The
    # logical `reviewer` in a result marker is just a model id; `operators` is who vouched for it
    # on GitHub — a separate provenance, so a collaborator can't forge a macro reviewer's verdict.
    rv.setdefault("operators", [])
    if rv["mode"] not in ("packet", "pr"):
        raise ValueError(f"review.mode must be 'packet' or 'pr', got {rv['mode']!r}")
    if not (isinstance(rv["reviewers"], list) and all(isinstance(r, str) for r in rv["reviewers"])):
        raise ValueError("review.reviewers must be a list of strings")
    if not isinstance(rv["require_ci"], bool):
        raise ValueError("review.require_ci must be a boolean")
    if not (isinstance(rv["approvers"], list) and all(isinstance(a, str) for a in rv["approvers"])):
        raise ValueError("review.approvers must be a list of strings")
    if not (isinstance(rv["operators"], list) and all(isinstance(o, str) for o in rv["operators"])):
        raise ValueError("review.operators must be a list of strings")
    dl = cfg.setdefault("delegation", {})
    if not isinstance(dl, dict):
        raise ValueError("delegation: must be a mapping (env_prep)")
    dl.setdefault("env_prep", None)  # None -> lockfile auto-detection at delegation time (no sandbox knob, R7)
    ep = dl["env_prep"]
    if ep is not None and not (isinstance(ep, list) and all(isinstance(x, str) for x in ep)):
        raise ValueError("delegation.env_prep must be a list of shell command strings")
    return cfg


def load_config(root: Path) -> dict:
    return normalize_config(load_yaml(_migrate_project_config(root)))


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
    Returns (pushed, info). Fail-closed: a fetch failure (network/auth/remote) returns
    (False, reason) rather than trusting a stale ref. Pass fetch=False for explicit offline use."""
    up = upstream_ref(root)
    if not up:
        return (False, {"reason": "no upstream tracking branch"})
    if fetch:
        rc, _, err = git_rc(root, "fetch", "--quiet", up.split("/", 1)[0])
        if rc != 0:
            return (False, {"reason": f"fetch failed — remote unverifiable: {err or 'error'}", "upstream": up})
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


def next_actionable(data: dict, cap: int = 6) -> list[tuple[str, str]]:
    """Tasks ready to pick up next: pending/active (or stale-blocked — `blocked` with every dep
    already done) whose deps are all satisfied. Pure: returns [(id, title)] sorted by id."""
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    by_id = {t["id"]: t for t in tasks}
    out = []
    for t in tasks:
        if t.get("status") not in ("pending", "active", "blocked"):
            continue
        deps = t.get("deps", []) or []
        if all(by_id.get(d, {}).get("status") == "done" for d in deps):
            out.append((t["id"], t.get("title", "")))
    return sorted(out)[:cap]


def _project_slug(root: Path) -> str:
    rp = str(root.resolve())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", rp).strip("-")[:60].rstrip("-")
    return f"{slug}-{hashlib.sha1(rp.encode('utf-8')).hexdigest()[:8]}"


def resume_path(root: Path) -> Path:
    """Project-local EPHEMERAL re-entry snapshot (NOT committed to the repo). Written
    deterministically by the PreCompact/SessionEnd hook (structured: HEAD/round/tasks) and CONSUMED
    by the next SessionStart."""
    return project_state_path(root) / "resume.md"


def start_here_path(root: Path) -> Path:
    """Project-local PERSISTENT re-entry pointer (NOT committed, NOT consumed). The
    MODEL overwrites it at round close / after review with a bounded live-frontier narrative; the
    SessionStart hook injects it so a new/resumed session picks up without a manual 'pick up where
    we left off'. Complements the ephemeral structured resume_path — narrative vs. structured."""
    return project_state_path(root) / "start-here.md"


def slugify(text: str, max_len: int = 40) -> str:
    """Filename slug for generated SSOT sections. Keeps Hangul (Korean headings stay
    readable); task IDs are NOT slugified with this — their grammar stays ASCII."""
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "section"
