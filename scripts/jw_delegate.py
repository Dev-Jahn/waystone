#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Delegation primitive — `jw delegate` (0.8.0 M1).

Delegate a single implementation task to an external runner (codex) in an isolated git worktree,
then bring the result back through an explicit, harness-computed artifact contract. The dirty working
tree is fixed as an immutable snapshot commit (no history pollution) and used as the delegation base,
so what the delegate sees is exactly what the user sees now. The harness computes the patch and
changed-files list from git directly (explicit provenance); the delegate's own report (verification,
limitations, risks) is carried through labeled delegate-claimed and never promoted to fact — an
independent verifier (main) accepts or discards via `apply`/`discard`.

See dev_docs/0.8.0-m1-implementation-notes.md for the binding spec.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from jw_common import (  # noqa: E402
    WorkflowError, _project_slug, find_project_root, git_full_sha, load_config, load_tasks,
)

DELEG_REF_NS = "refs/jw/delegations"
TERMINAL_STATES = ("applied", "discarded")
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "delegate-prompt.md"

# lockfile -> (prep command, kind), first match wins (S7). None env_prep in config falls through here.
_LOCKFILE_DETECT = (
    ("uv.lock", "uv sync --frozen", "uv"),
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile", "pnpm"),
    ("package-lock.json", "npm ci", "npm"),
    ("Cargo.toml", "cargo fetch", "cargo"),
    ("go.mod", "go mod download", "go"),
)

_PROFILE_EXAMPLE = (
    "schema: jw-profile-1\n"
    "bindings:\n"
    "  implementer: {execution: external-runner, backend: \"codex:gpt-5.4-codex\"}\n"
)


class _RefusedWrite(WorkflowError):
    """A plugin-local directory could not be created — maps to exit 2 (refused write, §2)."""


# ---- git plumbing (private; jw_common.git_rc has no env/cwd-index support) ----
def _git(cwd: Path, *args: str, env: dict | None = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run git in `cwd`; return (rc, stdout, stderr). `env` overlays os.environ (for GIT_INDEX_FILE).
    Output decodes with surrogateescape — git output is not guaranteed UTF-8 (H1) and a status/scan
    path must never crash on it."""
    full = {**os.environ, **env} if env else None
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           errors="surrogateescape", env=full, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return (127, "", str(e))
    return (p.returncode, p.stdout.strip(), p.stderr.strip())


def _git_out(cwd: Path, *args: str, env: dict | None = None) -> str:
    """git that must succeed — raises WorkflowError on failure so a raw git rc never leaks to exit."""
    rc, out, err = _git(cwd, *args, env=env)
    if rc != 0:
        raise WorkflowError(f"git {args[0]} failed: {err or out or f'rc {rc}'}")
    return out


def _git_path(root: Path, name: str) -> Path | None:
    """Resolve a repo internal path (e.g. MERGE_HEAD, rebase-merge) via `git rev-parse --git-path`,
    which is worktree-aware. Returns an absolute Path or None if git could not resolve it."""
    rc, out, _ = _git(root, "rev-parse", "--git-path", name)
    if rc != 0 or not out:
        return None
    p = Path(out)
    return p if p.is_absolute() else (root / p)


# ---- snapshot primitive (§3 — temp-index read-tree-HEAD, verified sequence) ---
def _check_snapshot_preconditions(root: Path) -> None:
    """Fail loud (WorkflowError) on any state that would bake a partial/conflicted tree into the base:
    unborn HEAD, submodules, unmerged index, or an in-progress merge/cherry-pick/revert/rebase (§3)."""
    if git_full_sha(root, "HEAD") is None:
        raise WorkflowError("repository has no commits yet (unborn HEAD) — commit something before delegating")
    if (root / ".gitmodules").exists():
        raise WorkflowError("submodules are not supported in M1 (.gitmodules present) — refusing a partial snapshot")
    if (root / "JW_REPORT.yaml").exists():
        # H2: it would be baked into the base, consumed as the delegate's report, and phantom-deleted
        # from the user's tree by the resulting patch.
        raise WorkflowError("JW_REPORT.yaml is a reserved delegation-protocol filename — "
                            "remove or rename it and retry")
    rc, out, _ = _git(root, "ls-files", "-u")
    if rc == 0 and out:
        raise WorkflowError("repository has unmerged paths — resolve the conflict before delegating")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        p = _git_path(root, name)
        if p is not None and p.exists():
            raise WorkflowError(f"an operation is in progress ({name}) — finish or abort it before delegating")
    for name in ("rebase-merge", "rebase-apply"):
        p = _git_path(root, name)
        if p is not None and p.is_dir():
            raise WorkflowError(f"a rebase is in progress ({name}) — finish or abort it before delegating")


def _snapshot(cwd: Path, message: str) -> tuple[str, bool]:
    """Fix cwd's current tracked+staged+untracked(non-ignored) state as an immutable commit object,
    seeded from HEAD via a throwaway index (§3 verified sequence — the live index/worktree are never
    touched). If the resulting tree equals HEAD's tree the state is clean: return (HEAD, False) and
    create no commit (clean-tree shortcut). Otherwise commit-tree the snapshot parented on HEAD and
    return (snapshot_sha, True). Works identically in the main repo and a linked worktree (HEAD there
    is the detached base, so `-p HEAD` parents the result on the base)."""
    head = _git_out(cwd, "rev-parse", "HEAD")
    head_tree = _git_out(cwd, "rev-parse", "HEAD^{tree}")
    tmpdir = tempfile.mkdtemp(prefix="jw-snap-")
    try:
        env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
        _git_out(cwd, "read-tree", "HEAD", env=env)          # seed (S1 — not an index copy)
        _git_out(cwd, "add", "-A", env=env)                  # tracked mods + staged + untracked(non-ignored)
        tree = _git_out(cwd, "write-tree", env=env)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if tree == head_tree:
        return head, False
    sha = _git_out(cwd, "commit-tree", tree, "-p", head, "-m", message)
    return sha, True


def _make_did(task_id: str) -> str:
    """Delegation id: `<UTC yyyymmddTHHMMSSZ>-<task-slug>` (task slug = id with '/' -> '-'). It records
    an execution event, so a timestamp is intentional (the 0.7 decisions.jsonl precedent)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{task_id.replace('/', '-')}"


# ---- residence (§9 — everything plugin-local, keyed by project slug) ----------
def _plugin_base() -> Path:
    return Path.home() / ".claude" / "jahns-workflow"


def _delegations_dir(root: Path) -> Path:
    return _plugin_base() / "delegations" / _project_slug(root)


def _worktrees_dir(root: Path) -> Path:
    return _plugin_base() / "worktrees" / _project_slug(root)


def _record_dir(root: Path, did: str) -> Path:
    return _delegations_dir(root) / did


def _worktree_path(root: Path, did: str) -> Path:
    return _worktrees_dir(root) / did


def _profile_path() -> Path:
    return _plugin_base() / "profile.yml"


def _mkdir_or_refuse(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise _RefusedWrite(f"cannot create plugin-local directory {path}: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- profile / binding (§11 — fail-loud, no default-model guessing) -----------
def _load_profile() -> tuple[dict, str]:
    """Load ~/.claude/jahns-workflow/profile.yml and its byte fingerprint. Raises WorkflowError with a
    creation guide if the file is absent (the harness never guesses a default model)."""
    path = _profile_path()
    if not path.is_file():
        raise WorkflowError(
            f"no delegation profile at {path} — create it with a role binding, e.g.:\n\n{_PROFILE_EXAMPLE}")
    raw = path.read_bytes()
    fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()[:12]
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    if not isinstance(data, dict):
        raise WorkflowError(f"profile {path} is not a mapping")
    return data, fingerprint


def _resolve_binding(profile: dict, role: str) -> dict:
    """Resolve the binding for `role`; validate execution axis and backend shape (S13/§11)."""
    bindings = profile.get("bindings")
    b = bindings.get(role) if isinstance(bindings, dict) else None
    if not isinstance(b, dict):
        raise WorkflowError(
            f"profile has no binding for role {role!r} — add it to {_profile_path()}, e.g.:\n\n{_PROFILE_EXAMPLE}")
    execution = b.get("execution")
    backend = b.get("backend")
    if execution != "external-runner":
        raise WorkflowError(f"binding execution {execution!r} not implemented in M1 (only 'external-runner')")
    if not isinstance(backend, str) or ":" not in backend or not backend.split(":", 1)[1]:
        raise WorkflowError(f"binding backend must be '<runner>:<model>', got {backend!r}")
    return {"role": role, "execution": execution, "backend": backend, "source": "profile"}


def _runner_model(backend: str) -> str:
    """Extract the model from a `codex:<model>` backend. Non-codex runners are schema-valid but not
    executable in M1 (§6) — fail loud rather than silently substitute."""
    runner, _, model = backend.partition(":")
    if runner != "codex":
        raise WorkflowError(f"backend {backend!r} not implemented in M1 (only 'codex:<model>')")
    return model


# ---- task packet (§7 — assemble the fields a delegate needs, not a raw copy) --
def _build_packet(data: dict, task_id: str, accept_flags: list[str], root: Path) -> tuple[dict, list[str]]:
    """Assemble packet.yaml from the registry + --accept flags. Fail loud on non-delegable status or an
    empty acceptance set (#3 — the harness never invents criteria)."""
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    by_id = {t.get("id"): t for t in tasks}
    task = by_id.get(task_id)
    if task is None:
        raise WorkflowError(f"task {task_id} is not in the registry")
    status = task.get("status", "pending")
    if status == "blocked":
        raise WorkflowError(f"task {task_id} is blocked — if its deps are now satisfied, set it active and retry")
    if status not in ("pending", "active"):
        raise WorkflowError(f"task {task_id} is {status} — only pending/active tasks can be delegated")
    acceptance: list[str] = []
    for a in list(task.get("accept") or []) + list(accept_flags):
        if a not in acceptance:
            acceptance.append(a)
    if not acceptance:
        raise WorkflowError(
            f"task {task_id} has no acceptance criteria — add `accept:` (YAML list) to the task or pass --accept")
    deps = [{"id": d, "status": by_id.get(d, {}).get("status", "unknown")} for d in (task.get("deps") or [])]
    packet = {
        "schema": "jw-packet-1",
        "task": {
            "id": task_id, "title": task.get("title"), "status": status,
            "milestone": task.get("milestone"), "round": task.get("round"),
            "deps": deps, "anchor": task.get("anchor"), "notes": task.get("notes"),
        },
        "acceptance": acceptance,
        "project": {"name": data.get("project"), "root": str(root.resolve())},
    }
    return packet, acceptance


def _render_prompt(packet: dict, base_sha: str) -> str:
    task = packet["task"]
    lines = [f"- id: {task['id']}", f"- title: {task.get('title')}", f"- status: {task['status']}"]
    for field in ("milestone", "round", "anchor", "notes"):
        if task.get(field):
            lines.append(f"- {field}: {task[field]}")
    if task.get("deps"):
        lines.append("- deps: " + ", ".join(f"{d['id']} ({d['status']})" for d in task["deps"]))
    acceptance = "\n".join(f"{i}. {c}" for i, c in enumerate(packet["acceptance"], 1))
    return (_TEMPLATE_PATH.read_text(encoding="utf-8")
            .replace("{{TASK_BLOCK}}", "\n".join(lines))
            .replace("{{ACCEPTANCE}}", acceptance)
            .replace("{{BASE_SHA}}", base_sha))


# ---- env prep (§5 — explicit config first, lockfile detection second) ---------
def _resolve_env_prep(worktree: Path, cfg: dict) -> tuple[str, list[str]]:
    """(kind, commands): explicit config, else first-matching lockfile, else none-detected (a
    document project is a normal case — recorded and proceeded)."""
    explicit = (cfg.get("delegation") or {}).get("env_prep")
    if explicit:
        return "explicit", list(explicit)
    for fname, cmd, kind in _LOCKFILE_DETECT:
        if (worktree / fname).exists():
            return f"detected:{kind}", [cmd]
    return "none-detected", []


def _run_env_prep(worktree: Path, commands: list[str]) -> tuple[int, str]:
    """Run each prep command in the worktree cwd (no shell, shlex.split, 600s each). Returns (rc,
    stderr_tail<=20 lines); rc 0 means every command succeeded."""
    for cmd in commands:
        try:
            p = subprocess.run(shlex.split(cmd), cwd=str(worktree),
                               capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, f"{cmd}: {e}"
        if p.returncode != 0:
            return p.returncode, "\n".join(p.stderr.strip().splitlines()[-20:])
    return 0, ""


# ---- runner (§6 — codex exec; isolated for monkeypatching in tests) -----------
def _run_codex(worktree: Path, model: str, prompt_path: Path, record_dir: Path) -> tuple[int, float]:
    """Invoke `codex exec` in the worktree (workspace-write sandbox hardcoded, S8). Returns (rc,
    duration_s). The full --json stream and last message are preserved as local evidence."""
    cmd = ["codex", "exec", "-C", str(worktree), "-m", model, "-s", "workspace-write",
           "--color", "never", "--output-last-message", str(record_dir / "last_message.md"), "--json"]
    start = time.monotonic()
    try:
        with open(prompt_path, encoding="utf-8") as pin, \
             open(record_dir / "runner.jsonl", "w", encoding="utf-8") as jout, \
             open(record_dir / "runner.stderr", "w", encoding="utf-8") as jerr:
            p = subprocess.run(cmd, stdin=pin, stdout=jout, stderr=jerr, timeout=3600)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:
        (record_dir / "runner.stderr").write_text(str(e), encoding="utf-8")
        rc = 127
    return rc, round(time.monotonic() - start, 3)


# ---- status.json (mutable lifecycle) ------------------------------------------
def _read_status_raw(record_dir: Path) -> dict | None:
    """Lenient read: None = corrupt/unreadable (the caller decides fail-safe vs fail-loud, H3)."""
    p = record_dir / "status.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_status(record_dir: Path) -> dict:
    """Strict read for single-record paths — a corrupt file names itself (WorkflowError, exit 1),
    never an uncaught traceback (H3)."""
    st = _read_status_raw(record_dir)
    if st is None:
        raise WorkflowError(f"corrupt status.json in delegation record: {record_dir / 'status.json'}")
    return st


def _set_state(record_dir: Path, state: str, *, env: dict | None = None, error: str | None = None) -> dict:
    st = _read_status_raw(record_dir) or {}  # a corrupt file is superseded — discard IS the recovery path
    st.setdefault("at_transitions", []).append({"state": state, "at": _now_iso()})
    st["state"] = state
    if env is not None:
        st["env"] = env
    if error is not None:
        st["error"] = error
    tmp = record_dir / "status.json.tmp"  # atomic replace: a crash mid-write must not corrupt the record
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, record_dir / "status.json")
    return st


# ---- owner lock (§4 — single mutation owner; terminal = {applied, discarded}) -
def _iter_delegations(root: Path):
    ddir = _delegations_dir(root)
    if not ddir.is_dir():
        return
    for sub in sorted(ddir.iterdir()):
        if sub.is_dir() and (sub / "exposure.json").exists():
            yield sub.name, sub


def _load_exposure(rec: Path) -> dict:
    """Strict exposure load — corrupt JSON names the file (WorkflowError), never a traceback (H3)."""
    p = rec / "exposure.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WorkflowError(f"corrupt exposure.json in delegation record: {p} ({e})")
    if not isinstance(data, dict):
        raise WorkflowError(f"corrupt exposure.json in delegation record: {p}")
    return data


def _load_contract(rec: Path) -> dict:
    """Strict contract load — corrupt YAML names the file (WorkflowError), never a traceback (H3)."""
    p = rec / "artifact" / "contract.yaml"
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise WorkflowError(f"corrupt contract.yaml in delegation record: {p} ({e})")
    if not isinstance(data, dict):
        raise WorkflowError(f"corrupt contract.yaml in delegation record: {p}")
    return data


def _active_delegation_for_task(root: Path, task_id: str) -> tuple[str, str] | None:
    """Owner-lock scan. Fail-safe on corruption (H3): a record whose state cannot be read, or whose
    task binding cannot be read, is treated as HOLDING the lock — the refusal names the corrupt file
    and the clearing path (discard) instead of guessing the record terminal."""
    for did, sub in _iter_delegations(root):
        st = _read_status_raw(sub)
        state = st.get("state") if st is not None else None
        if state in TERMINAL_STATES:
            continue
        try:
            tid = _load_exposure(sub).get("task_id")
        except WorkflowError:
            tid = None
        if tid is not None and tid != task_id:
            continue  # a healthy record of another task never blocks this one
        if st is None or tid is None:
            broken = "status.json" if st is None else "exposure.json"
            raise WorkflowError(
                f"delegation record {did} has a corrupt {broken} — treated as an active lock (fail-safe); "
                f"run `jw delegate discard {did}` to clear it")
        return did, state
    return None


# ---- artifact contract (§8 — harness-computed vs delegate-claimed provenance) -
def _read_report(worktree: Path) -> dict:
    """Read + remove JW_REPORT.yaml from the worktree BEFORE the result snapshot (so it never pollutes
    the patch, S4). present ∈ {True, False, 'invalid'} — a missing/unparseable report is named, not
    silently passed."""
    p = worktree / "JW_REPORT.yaml"
    if not p.exists():
        return {"present": False}
    raw = p.read_bytes()  # bytes: a non-UTF-8 report must surface as invalid, never crash (H1)
    p.unlink()
    try:
        data = yaml.safe_load(raw)  # PyYAML decodes bytes itself; bad UTF-8 -> ReaderError (a YAMLError)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        return {"present": "invalid"}
    return {
        "present": True,
        "verification": data.get("verification", []),
        "limitations": data.get("limitations", []),
        "risks": data.get("risks", []),
        "escalations": data.get("escalations", []),
    }


def _changed_files(root: Path, base: str, result: str) -> list[dict]:
    out = _git_out(root, "diff", "--name-status", "--no-renames", base, result)
    rows = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            rows.append({"path": parts[-1], "status": parts[0][:1]})
    return rows


def _diff_patch(cwd: Path, base: str, result: str) -> bytes:
    """Exact `git diff --binary --no-renames` output as BYTES — a patch is not UTF-8 in general (any
    latin-1 text file), so it must never round-trip through a strict str decode (H1)."""
    p = subprocess.run(["git", "-C", str(cwd), "diff", "--binary", "--no-renames", base, result],
                       capture_output=True, timeout=60)
    if p.returncode != 0:
        raise WorkflowError(f"git diff failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def _write_exposure(record_dir, did, root, packet, task_id, head_sha, base_sha, dirty, binding, fingerprint):
    exposure = {
        "schema": "jw-exposure-1", "delegation_id": did, "at": _now_iso(),
        "project": {"pslug": _project_slug(root), "root": str(root.resolve()), "name": packet["project"]["name"]},
        "task_id": task_id, "packet": "packet.yaml",
        "base": {"head_sha": head_sha, "snapshot_sha": base_sha, "dirty": dirty,
                 "dirty_state_policy": "snapshot-commit-v1"},
        "binding": binding,
        "profile_fingerprint": fingerprint,
        "sandbox": "workspace-write",
        "overlays": [], "guards": None, "waivers": [],
    }
    (record_dir / "exposure.json").write_text(
        json.dumps(exposure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exposure


def _add_worktree(root: Path, worktree_path: Path, base_sha: str) -> None:
    _git_out(root, "worktree", "add", "--detach", str(worktree_path), base_sha)


# ---- run (§§3-10 — the delegation vertical slice) -----------------------------
def run_delegation(root: Path, task_id: str, role: str, accept_flags: list[str]) -> int:
    """Snapshot -> worktree -> env prep -> runner -> artifact. Prints `key: value` progress to stdout;
    raises WorkflowError (exit 1) on any precondition/env/runner failure, _RefusedWrite (exit 2) on a
    plugin-local mkdir failure. Returns 0 on a produced artifact (state=needs-review)."""
    _check_snapshot_preconditions(root)
    profile, fingerprint = _load_profile()
    if role != "implementer":
        raise WorkflowError(f"role {role} not consumed in M1")
    binding = _resolve_binding(profile, role)
    model = _runner_model(binding["backend"])
    cfg = load_config(root)
    packet, _acceptance = _build_packet(load_tasks(root), task_id, accept_flags, root)

    active = _active_delegation_for_task(root, task_id)
    if active:
        raise WorkflowError(
            f"task {task_id} already has active delegation {active[0]} (state {active[1]}) — "
            f"apply or discard it first")

    did = _make_did(task_id)
    base_did, n = did, 2
    while _record_dir(root, did).exists():  # same-second collision: deterministic suffix, never a
        did = f"{base_did}-{n}"             # transition appended to an existing record (H4)
        n += 1
    record_dir = _record_dir(root, did)
    artifact_dir = record_dir / "artifact"
    worktree_path = _worktree_path(root, did)
    _mkdir_or_refuse(artifact_dir)
    _mkdir_or_refuse(worktree_path.parent)

    head_sha = git_full_sha(root, "HEAD")
    base_sha, dirty = _snapshot(root, f"jw delegation snapshot: {task_id} {did}")
    _git_out(root, "update-ref", f"{DELEG_REF_NS}/{did}", base_sha)
    _add_worktree(root, worktree_path, base_sha)
    print(f"base_sha: {base_sha}")
    print(f"dirty: {str(dirty).lower()}")
    print(f"worktree: {worktree_path}")

    (record_dir / "packet.yaml").write_text(
        yaml.safe_dump(packet, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_exposure(record_dir, did, root, packet, task_id, head_sha, base_sha, dirty, binding, fingerprint)
    _set_state(record_dir, "running")

    env_kind, env_commands = _resolve_env_prep(worktree_path, cfg)
    env_rc, env_tail = _run_env_prep(worktree_path, env_commands)
    env_rec = {"prep": env_kind, "commands": env_commands, "rc": env_rc}
    print(f"env_prep: {env_kind} rc={env_rc}")
    if env_rc != 0:
        _set_state(record_dir, "failed-env", env=env_rec, error=env_tail)
        raise WorkflowError(
            f"env prep failed (rc {env_rc}) — worktree preserved at {worktree_path}\n{env_tail}")

    (record_dir / "prompt.txt").write_text(_render_prompt(packet, base_sha), encoding="utf-8")
    runner_rc, duration = _run_codex(worktree_path, model, record_dir / "prompt.txt", record_dir)
    print(f"runner: backend={binding['backend']} rc={runner_rc}")
    if runner_rc != 0:
        _set_state(record_dir, "failed-runner", env=env_rec, error=f"runner rc {runner_rc}")
        raise WorkflowError(
            f"runner exited non-zero (rc {runner_rc}) — see {record_dir / 'runner.stderr'}; worktree preserved")

    # Any artifact-computation failure must not strand the record as `running` (a permanently held
    # owner lock): transition to failed-artifact — worktree preserved as evidence, lock held like the
    # other failed-* states, discard clears it (H1).
    try:
        report = _read_report(worktree_path)
        result_sha, _ = _snapshot(worktree_path, f"jw delegation result: {task_id} {did}")
        _git_out(root, "update-ref", f"{DELEG_REF_NS}/{did}-result", result_sha)
        changed = _changed_files(root, base_sha, result_sha)
        patch = _diff_patch(root, base_sha, result_sha)
        empty = patch.strip() == b""
        if not empty:
            (artifact_dir / "changes.patch").write_bytes(patch)
        contract = {
            "schema": "jw-artifact-1",
            "delegation_id": did, "task_id": task_id,
            "base_sha": base_sha, "result_sha": result_sha,
            "changed_files": changed,
            "patch_file": "changes.patch" if not empty else None,
            "empty": empty,
            "delegate_report": report,
            "env": env_rec,
            "runner": {"backend": binding["backend"], "rc": runner_rc,
                       "duration_s": duration, "last_message": "last_message.md"},
        }
        (artifact_dir / "contract.yaml").write_text(
            yaml.safe_dump(contract, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except Exception as e:
        _set_state(record_dir, "failed-artifact", env=env_rec, error=str(e))
        raise WorkflowError(
            f"artifact computation failed after the runner — worktree preserved at {worktree_path}: {e}")
    _set_state(record_dir, "needs-review", env=env_rec)
    print(f"artifact: {artifact_dir / 'contract.yaml'}")
    return 0


# ---- evaluation path (§12 — apply/discard/show/status) ------------------------
def _load_delegation(root: Path, did: str) -> Path:
    rec = _record_dir(root, did)
    if not (rec / "exposure.json").exists():
        raise WorkflowError(f"unknown delegation {did}")
    return rec


def _cleanup(root: Path, did: str) -> None:
    """Remove the worktree and both gc-survival refs; the record dir (history evidence) is kept."""
    worktree_path = _worktree_path(root, did)
    if worktree_path.exists():
        _git(root, "worktree", "remove", "--force", str(worktree_path))
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    _git(root, "update-ref", "-d", f"{DELEG_REF_NS}/{did}")
    _git(root, "update-ref", "-d", f"{DELEG_REF_NS}/{did}-result")
    _git(root, "worktree", "prune")


def apply_delegation(root: Path, did: str) -> int:
    """Accept a delegation onto the live tree with plain `git apply` (§12/R2 — not 3-way). An empty
    patch is a no-op success. On drift the apply fails atomically (no partial write) and state stays
    needs-review; the raw git rc never leaks (exit 1)."""
    rec = _load_delegation(root, did)
    state = _read_status(rec).get("state")
    if state != "needs-review":
        raise WorkflowError(f"delegation {did} is {state} — only a needs-review delegation can be applied")
    contract = _load_contract(rec)
    if not contract.get("empty"):
        rc, out, err = _git(root, "apply", str(rec / "artifact" / "changes.patch"))
        if rc != 0:
            raise WorkflowError(
                f"cannot apply {did}: live tree has drifted from the delegation base — commit/stash "
                f"your changes and retry, or resolve the patch manually\n{err or out}")
    _cleanup(root, did)
    _set_state(rec, "applied")
    print(f"applied {did}" + (" (empty patch — no-op)" if contract.get("empty") else ""))
    return 0


def discard_delegation(root: Path, did: str) -> int:
    """Reject a delegation: state=discarded + worktree/ref cleanup (record dir kept). Accepts any
    non-terminal state, including a crash-remnant `running` (§4/R1) and a corrupt record — the
    cleanup path must not block itself on the corruption it clears (H3)."""
    rec = _load_delegation(root, did)
    st = _read_status_raw(rec)
    state = st.get("state") if st is not None else None
    if state in TERMINAL_STATES:
        raise WorkflowError(f"delegation {did} is already {state}")
    _cleanup(root, did)
    _set_state(rec, "discarded")
    print(f"discarded {did}")
    return 0


def _status_row(did: str, rec: Path) -> str:
    """One list line. Lenient: a corrupt record renders as [corrupt] instead of killing the whole
    listing (H3) — single-record verbs (show/apply) are the strict, file-naming paths."""
    st = _read_status_raw(rec)
    try:
        exposure = _load_exposure(rec)
    except WorkflowError:
        exposure = None
    if st is None or exposure is None:
        return f"{did}  ?  [corrupt]  ?  ?"
    base7 = (exposure.get("base", {}).get("snapshot_sha") or "")[:7]
    at = (st.get("at_transitions") or [{}])[0].get("at", "?")
    return f"{did}  {exposure.get('task_id', '?')}  [{st.get('state', '?')}]  {base7}  {at}"


def status(root: Path, did: str | None) -> int:
    if did:
        rec = _load_delegation(root, did)
        print(_status_row(did, rec))
        return 0
    for name, rec in _iter_delegations(root):
        print(_status_row(name, rec))
    return 0


def show(root: Path, did: str, opt: str | None) -> int:
    rec = _load_delegation(root, did)
    state = _read_status(rec).get("state")  # strict: show is a single-record path (H3)
    if opt == "exposure":
        _load_exposure(rec)  # validate before dumping — corrupt names the file
        print((rec / "exposure.json").read_text(encoding="utf-8").rstrip())
        return 0
    if opt in ("patch", "report"):
        contract_p = rec / "artifact" / "contract.yaml"
        if not contract_p.exists():
            raise WorkflowError(f"delegation {did} is {state} — no patch/contract was produced")
        if opt == "report":
            print(contract_p.read_text(encoding="utf-8").rstrip())
            return 0
        patch_p = rec / "artifact" / "changes.patch"
        if patch_p.exists():
            data = patch_p.read_bytes()  # the patch is bytes, not UTF-8 in general (H1)
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write(data)
            else:  # a captured/StringIO stdout has no binary buffer
                sys.stdout.write(data.decode("utf-8", errors="replace"))
        return 0
    # summary
    _load_exposure(rec)  # strict: corrupt exposure names the file rather than rendering [corrupt]
    print(_status_row(did, rec))
    contract_p = rec / "artifact" / "contract.yaml"
    if contract_p.exists():
        contract = _load_contract(rec)
        rep = contract.get("delegate_report", {}).get("present")
        rep_str = {True: "present", False: "absent", "invalid": "invalid"}.get(rep, str(rep))
        print(f"changed_files: {len(contract.get('changed_files', []))}")
        print("patch: " + ("empty" if contract.get("empty") else "changes.patch"))
        print(f"delegate_report: {rep_str}")
    return 0


# ---- CLI (hand-rolled parsing; {0,1,2} exit contract, never a raw git rc) -----
def _parse_opts(rest: list[str], *, value=(), boolean=(), repeat=()) -> tuple[list[str], dict]:
    pos: list[str] = []
    opts: dict = {r: [] for r in repeat}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            name = a[2:]
            if name in repeat:
                if i + 1 >= len(rest):
                    raise WorkflowError(f"--{name} requires a value")
                opts[name].append(rest[i + 1])
                i += 2
            elif name in value:
                if i + 1 >= len(rest):
                    raise WorkflowError(f"--{name} requires a value")
                opts[name] = rest[i + 1]
                i += 2
            elif name in boolean:
                opts[name] = True
                i += 1
            else:
                raise WorkflowError(f"unknown option --{name}")
        else:
            pos.append(a)
            i += 1
    return pos, opts


def _resolve_root(explicit: str | None) -> Path:
    root = Path(explicit).resolve() if explicit else find_project_root(Path.cwd())
    if root is None:
        raise WorkflowError("no initialized project (run inside one, or pass --root DIR)")
    return root


def _cli_run(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("role", "root"), repeat=("accept",))
    if not pos:
        raise WorkflowError("run requires a <task-id>")
    return run_delegation(_resolve_root(opts.get("root")), pos[0],
                          opts.get("role", "implementer"), opts.get("accept", []))


def _cli_status(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    return status(_resolve_root(opts.get("root")), pos[0] if pos else None)


def _cli_show(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",), boolean=("patch", "report", "exposure"))
    if not pos:
        raise WorkflowError("show requires a <delegation-id>")
    chosen = [o for o in ("patch", "report", "exposure") if opts.get(o)]
    if len(chosen) > 1:
        raise WorkflowError("show takes at most one of --patch/--report/--exposure")
    return show(_resolve_root(opts.get("root")), pos[0], chosen[0] if chosen else None)


def _cli_apply(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("apply requires a <delegation-id>")
    return apply_delegation(_resolve_root(opts.get("root")), pos[0])


def _cli_discard(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("discard requires a <delegation-id>")
    return discard_delegation(_resolve_root(opts.get("root")), pos[0])


_HANDLERS = {"run": _cli_run, "status": _cli_status, "show": _cli_show,
             "apply": _cli_apply, "discard": _cli_discard}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in _HANDLERS:
        print("jw delegate: expected subcommand (run|status|show|apply|discard)", file=sys.stderr)
        return 1
    try:
        return _HANDLERS[argv[0]](argv[1:])
    except _RefusedWrite as e:
        print(f"jw delegate: {e}", file=sys.stderr)
        return 2
    except WorkflowError as e:
        print(f"jw delegate: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
