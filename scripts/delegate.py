#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Delegation primitive — `waystone delegate` (0.8.0 M1).

Delegate a single implementation task to an external runner (Codex or Claude) in an isolated git worktree,
then bring the result back through an explicit, harness-computed artifact contract. The dirty working
tree is fixed as an immutable snapshot commit (no history pollution) and used as the delegation base,
so what the delegate sees is exactly what the user sees now. The harness computes the patch and
changed-files list from git directly (explicit provenance); the delegate's own report (verification,
limitations, risks) is carried through labeled delegate-claimed and never promoted to fact — an
independent verifier (main) accepts or discards via `apply`/`discard`.

See dev_docs/0.8.0-m1-implementation-notes.md for the binding spec.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import signal
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from common import (
    WorkflowError, _project_slug, canonical_scope_prefixes, ensure_project_state_dir,
    find_project_root, git_full_sha, hold_lock, hold_project_lock, load_config, load_tasks,
    migrate_project_state, project_state_path, worktrees_cache_dir, write_text_atomic,
)  # noqa: E402

DELEG_REF_NS = "refs/waystone/delegations"
TERMINAL_STATES = ("applied", "discarded")
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "delegate-prompt.md"
_VERIFY_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "adversarial-review-prompt.md")
_VERIFY_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "templates" / "verifier-output-schema.json"
_VERDICT_INPUT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "verdict-input-schema.json")
_VERDICT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "templates" / "verdict-schema.json"
_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra")
PROFILE_ROLES = ("main", "orchestrator", "implementer", "clerk", "verifier", "reviewer")
PROFILE_EXECUTIONS = (
    "main-session", "clean-subagent", "forked-subagent",
    "deterministic-workflow", "external-runner",
)
# Honest execution boundary (design revision R3): waystone directly starts a process only for an
# external-runner. The other four executions are host-guided through routing-contract injection;
# Claude Code/Codex owns their main session, subagent, or workflow process. In particular this table
# does not imply a clerk runner. A listed pair is schema-valid, not necessarily waystone-executable.
WAYSTONE_EXECUTABLE_EXECUTIONS = ("external-runner",)
HOST_GUIDED_EXECUTIONS = tuple(
    execution for execution in PROFILE_EXECUTIONS
    if execution not in WAYSTONE_EXECUTABLE_EXECUTIONS)
VALID_ROLE_EXECUTIONS = {
    "main": ("main-session",),
    "orchestrator": (
        "main-session", "clean-subagent", "forked-subagent", "deterministic-workflow",
    ),
    "implementer": (
        "clean-subagent", "forked-subagent", "deterministic-workflow", "external-runner",
    ),
    "clerk": (
        "clean-subagent", "forked-subagent", "deterministic-workflow", "external-runner",
    ),
    "verifier": (
        "clean-subagent", "forked-subagent", "deterministic-workflow", "external-runner",
    ),
    "reviewer": (
        "clean-subagent", "forked-subagent", "deterministic-workflow", "external-runner",
    ),
}
_BACKEND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*:[^\s:]+$")
_LEGACY_VERIFIER_EXECUTIONS = ("codex-cli", "codex-companion")
_EXTERNAL_RUNNERS = ("codex", "claude")
_CLAUDE_EFFORT_VALUES = ("low", "medium", "high", "xhigh")
_VERIFIER_SESSION_ENV = "WAYSTONE_VERIFIER_SESSION"
_CODEX_RUNNER_VERIFIED_MARKER = "codex-runner-verified"
_CODEX_RUNNER_VERIFICATION_LOCK = "codex-runner-verification.lock"
_CODEX_RUNNER_PROOF_SCHEMA = "waystone-codex-runner-proof-2"
_CODEX_SANDBOX_INVOCATION_CONTRACT = "codex-exec:workspace-write:v1"
_IOPLATFORM_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
# Fan-out carrier attribution (§4.2 / ADR-0001): recorded in the immutable packet as an
# improve/cclog join key. v1 has a single carrier; the instance id is a pre-generated correlation id.
_CARRIER_NAMES = ("claude-workflow",)
_CARRIER_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$")
FANOUT_PLAN_SCHEMA = "waystone-fanout-plan-1"
# Claude implementer execution is intentionally refused unless the user records an explicit
# unsandboxed-runner override. These flags reduce surfaces after that override, but are not described
# as confinement: bare Bash can cross filesystem, process, repository, and network boundaries.
_CLAUDE_COMMON_ARGS = (
    "--safe-mode", "--no-chrome", "--disable-slash-commands",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--permission-mode", "dontAsk", "--no-session-persistence",
)
_CLAUDE_NETWORK_DENY = "WebFetch,WebSearch"
# HEURISTIC HINT ONLY (ruling 2026-07-17): these stderr patterns feed diagnostics and failure
# hints, never a gate — the authoritative env-failure decision is the conjunctive check
# (rc + empty patch + missing report + write-proof). Free-text classification cannot be made
# exact; imperfect matches on mixed lines are accepted, not chased.
_SANDBOX_MECHANISM_RE = re.compile(
    r"(?:bwrap|bubblewrap|landlock|userns|user namespace|unprivileged_userns|apparmor|"
    r"seatbelt|sandbox-exec|sandbox_apply|RTM_NEWADDR)", re.IGNORECASE)
_SANDBOX_FAILURE_TERM_RE = re.compile(
    r"(?:failed|failure|error|unable|cannot|could not|denied|not permitted|permission denied|"
    r"read-only file system)", re.IGNORECASE)
_SANDBOX_STRONG_FAILURE_TERM_RE = re.compile(
    r"(?:failed|failure|error|unable|cannot|could not|not permitted|permission denied|"
    r"read-only file system)", re.IGNORECASE)
_SANDBOX_SUCCESS_NARRATIVE_RE = re.compile(
    r"(?:correctly|successfully|expectedly)\s+denied|(?:all\s+)?(?:sandbox\s+)?tests?\s+passed",
    re.IGNORECASE)
_SANDBOX_WRITE_OPERATION_FAILURE_RE = re.compile(
    r"(?:"
    r"(?:failed|unable|cannot|could not)(?:\s+to)?\s+"
    r"(?:write|modify|patch|mkdir|touch|apply_patch|create (?:file|directory))[^\n]*"
    r"(?:operation not permitted|permission denied|read-only file system)|"
    r"(?:write|mkdir|touch|apply_patch|create (?:file|directory))[^\n]*"
    r"(?:operation not permitted|permission denied|read-only file system)"
    r")", re.IGNORECASE)
_PROBE_AUTH_FAILURE_RE = re.compile(
    r"(?:401\s+unauthorized|403\s+forbidden|unauthorized|authentication\s+failed|"
    r"failed\s+to\s+authenticate|invalid\s+(?:api\s+)?key|api\s+key|token\s+expired|"
    r"login\s+required|please\s+log\s+in)", re.IGNORECASE)
_PROBE_NETWORK_FAILURE_RE = re.compile(
    r"(?:network\s+(?:request\s+)?failed|"
    r"network\s+is\s+unreachable|connection\s+(?:refused|reset)|"
    r"(?:failed|unable|could not)\s+to\s+(?:connect|resolve)|"
    r"name or service not known|temporary failure in name resolution|dns\s+error|"
    r"tls\s+error|proxy\s+error)", re.IGNORECASE)
CLAUDE_CONFINEMENT_WARN = (
    "waystone warn: UNSANDBOXED claude implementer — allowed Bash can read/write filesystem "
    "paths outside the worktree, operate on other repositories, spawn or affect processes, and "
    "access the network; cwd and Claude tool permissions are not a security boundary")
CLAUDE_VERIFIER_DELTA_WARN = (
    "waystone warn: claude verifier has no OS-level filesystem/process/network sandbox; "
    "effective tools are Read/Glob/Grep only, Bash/Edit/Write and network-native tools are denied, "
    "and tracked plus untracked worktree state is checked after execution")

# lockfile -> (prep command, kind), first match wins (S7). None env_prep in config falls through here.
_LOCKFILE_DETECT = (
    ("uv.lock", "uv sync --frozen", "uv"),
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile", "pnpm"),
    ("package-lock.json", "npm ci", "npm"),
    ("Cargo.toml", "cargo fetch", "cargo"),
    ("go.mod", "go mod download", "go"),
)

_PROFILE_EXAMPLE = (
    "schema: waystone-profile-1\n"
    "bindings:\n"
    "  implementer: {execution: external-runner, backend: \"codex:gpt-5.6-sol\", effort: xhigh}\n"
    "  verifier: {execution: external-runner, backend: \"codex:gpt-5.6-sol\"}\n"
    "  reviewer: {execution: external-runner, backend: \"codex:gpt-5.6-sol\"}\n"
)


def _profile_example() -> str:
    return _PROFILE_EXAMPLE


class _RefusedWrite(WorkflowError):
    """A plugin-local directory could not be created — maps to exit 2 (refused write, §2)."""


class _RunnerProbeFailure(WorkflowError):
    """A structured preflight failure; probe_result remains recordable on every exit path."""

    def __init__(self, message: str, probe_result: dict | None = None):
        super().__init__(message)
        self.probe_result = probe_result


class _RunnerSandboxUnusable(_RunnerProbeFailure):
    """Concrete sandbox evidence shows that the preflight write was denied."""


class _RunnerProbeTransportFailure(_RunnerProbeFailure):
    """The runner transport failed before the probe could establish sandbox behavior."""


class _RunnerProbeEvidenceFailure(_RunnerProbeFailure):
    """The harness could not collect or persist the evidence needed to decide the probe."""


class _RunnerProbeLifecycleFailure(_RunnerProbeFailure):
    """The disposable probe worktree could not be created or proven cleaned."""


class _RunnerEnvironmentFailure(WorkflowError):
    """A nominal runner success contains evidence that its environment prevented the work."""


# ---- git plumbing (private; common.git_rc has no env/cwd-index support) ----
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


def _snapshot(cwd: Path, message: str, *, exclude_uv_cache: bool = False) -> tuple[str, bool]:
    """Fix cwd's current tracked+staged+untracked(non-ignored) state as an immutable commit object,
    seeded from HEAD via a throwaway index (§3 verified sequence — the live index/worktree are never
    touched). If the resulting tree equals HEAD's tree the state is clean: return (HEAD, False) and
    create no commit (clean-tree shortcut). Otherwise commit-tree the snapshot parented on HEAD and
    return (snapshot_sha, True). Works identically in the main repo and a linked worktree (HEAD there
    is the detached base, so `-p HEAD` parents the result on the base)."""
    head = _git_out(cwd, "rev-parse", "HEAD")
    head_tree = _git_out(cwd, "rev-parse", "HEAD^{tree}")
    tmpdir = tempfile.mkdtemp(prefix="waystone-snap-")
    try:
        env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
        _git_out(cwd, "read-tree", "HEAD", env=env)          # seed (S1 — not an index copy)
        add_args = ("add", "-A", "--", ".", ":(exclude).waystone-uv-cache") if exclude_uv_cache else (
            "add", "-A")
        _git_out(cwd, *add_args, env=env)                      # tracked mods + untracked(non-ignored)
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


# ---- residence (§9 — project state + machine worktree cache) -----------------
def _delegations_dir(root: Path) -> Path:
    return project_state_path(root) / "delegations"


def _worktrees_dir(root: Path) -> Path:
    return worktrees_cache_dir() / _project_slug(root)


def _record_dir(root: Path, did: str) -> Path:
    return _delegations_dir(root) / did


def _worktree_path(root: Path, did: str) -> Path:
    return _worktrees_dir(root) / did


def _profile_path(root: Path) -> Path:
    return project_state_path(root) / "profile.yml"


def _mkdir_or_refuse(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise _RefusedWrite(f"cannot create plugin-local directory {path}: {e}")


def _ensure_project_state_or_refuse(root: Path) -> None:
    try:
        ensure_project_state_dir(root)
    except OSError as e:
        raise _RefusedWrite(f"cannot create project state directory {project_state_path(root)}: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- profile / binding (§11 — fail-loud, no default-model guessing) -----------
def _canonical_execution(role: str, binding: dict) -> str:
    execution = binding.get("execution")
    # v0.9 verifier profiles omitted execution or carried the derived transport name. Preserve
    # those files while normalizing their role/execution axis to external-runner.
    if role == "verifier" and (execution is None or execution in _LEGACY_VERIFIER_EXECUTIONS):
        return "external-runner"
    if execution not in PROFILE_EXECUTIONS:
        raise WorkflowError(
            f"binding execution {execution!r} must be one of {', '.join(PROFILE_EXECUTIONS)}")
    return execution


def _validate_profile_binding(role: str, binding: object) -> str:
    if role not in PROFILE_ROLES:
        raise WorkflowError(
            f"profile binding role {role!r} must be one of {', '.join(PROFILE_ROLES)}")
    if not isinstance(binding, dict):
        raise WorkflowError(f"profile binding for role {role!r} must be a mapping")
    unknown = set(binding) - {"execution", "backend", "use_for", "effort", "entry"}
    if unknown:
        raise WorkflowError(
            f"profile binding for role {role!r} has unknown field(s): {', '.join(sorted(unknown))}")
    execution = _canonical_execution(role, binding)
    if execution not in VALID_ROLE_EXECUTIONS[role]:
        raise WorkflowError(
            f"binding execution {execution!r} is not valid for role {role!r}; valid: "
            f"{', '.join(VALID_ROLE_EXECUTIONS[role])}")
    backend = binding.get("backend")
    if not isinstance(backend, str) or not _BACKEND_RE.fullmatch(backend):
        raise WorkflowError(f"binding backend must be '<runner>:<model>', got {backend!r}")
    use_for = binding.get("use_for")
    if use_for is not None and (
            not isinstance(use_for, str) or not use_for.strip() or "\n" in use_for
            or "\r" in use_for):
        raise WorkflowError("binding field use_for must be one non-empty line")
    effort = binding.get("effort")
    if effort is not None and (not isinstance(effort, str) or effort not in _EFFORT_VALUES):
        raise WorkflowError(
            f"binding field effort must be one of {', '.join(_EFFORT_VALUES)}, got {effort!r}")
    entry = binding.get("entry")
    if entry is not None and not isinstance(entry, str):
        raise WorkflowError(f"binding field entry must be a string, got {entry!r}")
    return execution


def _validate_profile(profile: dict, path: Path) -> None:
    schema = profile.get("schema")
    if schema is not None and schema not in ("waystone-profile-1", "jw-profile-1"):
        raise WorkflowError(
            f"profile {path} schema must be 'waystone-profile-1' (or legacy 'jw-profile-1'), "
            f"got {schema!r}")
    bindings = profile.get("bindings")
    if not isinstance(bindings, dict):
        raise WorkflowError(f"profile {path} bindings must be a mapping")
    if not bindings:
        raise WorkflowError(f"profile {path} bindings must contain at least one role")
    for role, binding in bindings.items():
        _validate_profile_binding(role, binding)


def _load_profile(root: Path) -> tuple[dict, str]:
    """Load the project's profile.yml and its byte fingerprint. Raises WorkflowError with a
    creation guide if the file is absent (the harness never guesses a default model)."""
    path = _profile_path(root)
    if not path.is_file():
        raise WorkflowError(
            f"no delegation profile at {path} — create it with a role binding, e.g.:\n\n"
            f"{_profile_example()}")
    raw = path.read_bytes()
    fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()[:12]
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise WorkflowError(f"profile {path} is unreadable/unparseable: {e}") from e
    if not isinstance(data, dict):
        raise WorkflowError(f"profile {path} is not a mapping")
    _validate_profile(data, path)
    return data, fingerprint


def _validate_external_runner_effort(runner: str, effort: str | None) -> None:
    if runner == "claude" and effort is not None and effort not in _CLAUDE_EFFORT_VALUES:
        raise WorkflowError(
            f"claude external-runner effort must be one of {', '.join(_CLAUDE_EFFORT_VALUES)}, "
            f"got {effort!r}")
    if effort == "ultra" and runner != "codex":
        raise WorkflowError(
            f"effort 'ultra' requires the Codex runner, got {runner!r}; "
            "no substitute effort will be selected")


def _resolve_binding(profile: dict, role: str, root: Path) -> dict:
    """Resolve the binding for `role`; validate execution axis and backend shape (S13/§11)."""
    bindings = profile.get("bindings")
    b = bindings.get(role) if isinstance(bindings, dict) else None
    if not isinstance(b, dict):
        raise WorkflowError(
            f"profile has no binding for role {role!r} — add it to {_profile_path(root)}, e.g.:\n\n"
            f"{_profile_example()}")
    execution = _validate_profile_binding(role, b)
    backend = b.get("backend")
    if role != "implementer" or execution != "external-runner":
        raise WorkflowError(
            f"binding {role!r}/{execution!r} is schema-valid but not executable by waystone: "
            "it is host-guided. Follow the injected routing contract in the main session, use "
            "skill routing to dispatch the bound role/execution/backend, and preserve role-based "
            "observation attribution; delegate run starts only implementer/external-runner")
    runner, _model = _runner_parts(backend)
    effort = b.get("effort")
    _validate_external_runner_effort(runner, effort)
    binding = {"role": role, "execution": execution, "backend": backend, "source": "profile"}
    for field in ("effort", "use_for"):
        if field in b:
            binding[field] = b[field]
    return binding


def _unsandboxed_runner_override(binding: dict, allow: bool,
                                 reason: str | None) -> dict | None:
    runner, _model = _runner_parts(binding["backend"])
    if allow and (reason is None or not reason.strip()):
        raise WorkflowError("--allow-unsandboxed-runner requires a non-empty --reason")
    if reason is not None and not allow:
        raise WorkflowError("--reason for delegate run is only valid with --allow-unsandboxed-runner")
    if runner == "claude":
        if not allow:
            raise WorkflowError(
                "claude implementer has no verified worktree/process/network confinement and is "
                "refused by default; use --allow-unsandboxed-runner --reason <why> to record an "
                "explicit exposure override")
        return {"kind": "allow-unsandboxed-runner", "reason": reason.strip(),
                "provenance": "user"}
    if allow:
        raise WorkflowError("--allow-unsandboxed-runner is only valid for a claude backend")
    return None


def _resolve_verifier_binding(profile: dict, root: Path) -> dict:
    """Resolve a host-independent Codex exec or headless Claude verifier transport."""
    bindings = profile.get("bindings")
    b = bindings.get("verifier") if isinstance(bindings, dict) else None
    if not isinstance(b, dict):
        raise WorkflowError(
            f"profile has no binding for role 'verifier' — add it to {_profile_path(root)}, e.g.:\n\n"
            f"{_profile_example()}")
    canonical = _validate_profile_binding("verifier", b)
    if canonical != "external-runner":
        raise WorkflowError(
            f"binding 'verifier'/{canonical!r} is schema-valid but not executable by waystone: "
            "it is host-guided (the verifier consumer starts only external-runner)")
    execution = b.get("execution")
    backend = b.get("backend")
    runner, _model = _runner_parts(backend)
    effort = b.get("effort")
    entry = b.get("entry")
    if entry not in (None, "adversarial-review"):
        raise WorkflowError(
            f"entry {entry!r} is not a known verifier entry — the only (deprecated) value is "
            "'adversarial-review'; new profiles omit the entry field")
    legacy_fields = []
    if execution in _LEGACY_VERIFIER_EXECUTIONS:
        legacy_fields.append(f"execution {execution!r}")
    if entry == "adversarial-review":
        legacy_fields.append("entry 'adversarial-review'")
    if runner == "claude":
        if execution in _LEGACY_VERIFIER_EXECUTIONS:
            raise WorkflowError(
                f"verifier execution {execution!r} is a legacy Codex transport and conflicts "
                f"with backend {backend!r}; use external-runner")
        resolved_execution = "claude-cli"
    else:
        resolved_execution = "codex-exec"
    if legacy_fields:
        print(
            "waystone delegate: verifier profile field(s) "
            f"{', '.join(legacy_fields)} are deprecated — remove them; Waystone owns the "
            "verification entrypoint and transport",
            file=sys.stderr,
        )
    _validate_external_runner_effort(runner, effort)
    binding = {"role": "verifier", "execution": resolved_execution, "backend": backend,
               "source": "profile"}
    if effort is not None:
        binding["effort"] = effort
    return binding


def _runner_parts(backend: str) -> tuple[str, str]:
    """Return a shipped external runner token and model; never substitute an unsupported runner."""
    runner, _, model = backend.partition(":")
    if runner not in _EXTERNAL_RUNNERS or not model:
        raise WorkflowError(
            f"backend {backend!r} is not waystone-executable; external-runner supports "
            f"{', '.join(f'{name}:<model>' for name in _EXTERNAL_RUNNERS)}")
    return runner, model


def _runner_model(backend: str) -> str:
    """Extract the explicit model from a shipped external-runner backend."""
    _runner, model = _runner_parts(backend)
    return model


# ---- task packet (§7 — assemble the fields a delegate needs, not a raw copy) --
def _build_packet(data: dict, task_id: str, accept_flags: list[str], root: Path,
                  retry_note: str | None = None,
                  routing_note: str | None = None) -> tuple[dict, list[str]]:
    """Assemble packet.yaml from the registry + --accept flags. Fail loud on non-delegable status,
    unmet dependencies, or an empty acceptance set (#3 — the harness never invents criteria)."""
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
    deps = []
    for dep_id in task.get("deps") or []:
        dep = by_id.get(dep_id)
        dep_status = dep.get("status", "pending") if dep is not None else "unknown"
        deps.append({"id": dep_id, "status": dep_status})
    unmet_deps = [dep for dep in deps if dep["status"] != "done"]
    if unmet_deps:
        diagnostics = ", ".join(f"{dep['id']} ({dep['status']})" for dep in unmet_deps)
        raise WorkflowError(
            f"task {task_id} has unmet dependencies — every dependency must exist with status "
            f"done: {diagnostics}")
    acceptance: list[str] = []
    accept_provenance: list[dict] = []
    for source, values in (("task --accept-add", list(task.get("accept") or [])),
                           ("delegate run --accept", list(accept_flags))):
        for criterion in values:
            if criterion not in acceptance:
                acceptance.append(criterion)
                accept_provenance.append({"criterion": criterion, "source": source})
    if not acceptance:
        raise WorkflowError(
            f"task {task_id} has no acceptance criteria — add `accept:` (YAML list) to the task or pass --accept")
    declared_scope = canonical_scope_prefixes(task.get("scope", []))
    packet = {
        "schema": "waystone-packet-1",
        "task": {
            "id": task_id, "title": task.get("title"), "status": status,
            "milestone": task.get("milestone"), "round": task.get("round"),
            "deps": deps, "anchor": task.get("anchor"), "notes": task.get("notes"),
        },
        "acceptance": acceptance,
        "accept_provenance": accept_provenance,
        "declared_scope": declared_scope,
        "project": {"name": data.get("project"), "root": str(root.resolve())},
    }
    if retry_note is not None:
        if not retry_note.strip():
            raise WorkflowError("--note must be non-empty")
        packet["retry_context"] = {"provenance": "main-session", "note": retry_note}
    if routing_note is not None:
        if not routing_note.strip() or "\n" in routing_note or "\r" in routing_note:
            raise WorkflowError("--routing-note must be one non-empty line")
        packet["routing_note"] = {
            "provenance": "main-session", "note": routing_note.strip(),
        }
    return packet, acceptance


def _packet_core_digest(packet: dict) -> str:
    """Machine- and worktree-stable digest of the decision-bearing packet core (§4.1 / Major 4).

    `_registry_task_prompt_lines` and `_acceptance_prompt_text` are the shared projection used here
    and by `_render_prompt`: every registry-derived field visible to the implementer (id, title,
    status, milestone, round, anchor, notes, deps, and acceptance) therefore enters the digest in
    exactly the representation the prompt sees. Routing/carrier/retry metadata (routing_note,
    retry_context, carrier, runner_override) is deliberately excluded so retries and carrier
    instances never perturb the digest. `project` is the config project NAME (never an absolute
    root), so the same task hashes identically across machines and worktrees. `delegate plan` pins
    this value; `delegate run --expect-packet-sha` recomputes it on the freshly rebuilt packet and
    refuses a mismatch before any record is written."""
    task = packet.get("task") or {}
    task_id = task.get("id")
    core = {
        "implementer_prompt": {
            "task_lines": _registry_task_prompt_lines(task),
            "acceptance": _acceptance_prompt_text(packet.get("acceptance") or []),
        },
        "type": task_id.partition("/")[0] if isinstance(task_id, str) else None,
        "accept_provenance": list(packet.get("accept_provenance") or []),
        "declared_scope": list(packet.get("declared_scope") or []),
        "project": (packet.get("project") or {}).get("name"),
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validate_carrier(carrier: str | None, instance: str | None) -> dict | None:
    """Validate the both-or-neither --carrier/--carrier-instance pair (§4.2). Returns the packet
    carrier record, or None when neither is given."""
    if carrier is None and instance is None:
        return None
    if carrier is None or instance is None:
        raise WorkflowError("--carrier and --carrier-instance must be given together")
    if carrier not in _CARRIER_NAMES:
        raise WorkflowError(
            f"--carrier must be one of {', '.join(_CARRIER_NAMES)}, got {carrier!r}")
    if not _CARRIER_INSTANCE_RE.fullmatch(instance):
        raise WorkflowError(
            "--carrier-instance must match ^[A-Za-z0-9][A-Za-z0-9.:_-]*$")
    return {"name": carrier, "instance_id": instance}


def _registry_task_prompt_lines(task: dict) -> list[str]:
    """Render every registry-derived task field exposed in the implementer prompt.

    The packet digest consumes this exact projection; add any future prompt-visible registry task
    field here rather than independently in `_render_prompt` or `_packet_core_digest`.
    """
    lines = [f"- id: {task['id']}", f"- title: {task.get('title')}",
             f"- status: {task['status']}"]
    for field in ("milestone", "round", "anchor", "notes"):
        if task.get(field):
            lines.append(f"- {field}: {task[field]}")
    if task.get("deps"):
        lines.append("- deps: " + ", ".join(
            f"{dep['id']} ({dep['status']})" for dep in task["deps"]))
    return lines


def _acceptance_prompt_text(acceptance: list) -> str:
    """Render acceptance identically for the implementer prompt and packet digest."""
    return "\n".join(f"{i}. {criterion}" for i, criterion in enumerate(acceptance, 1))


def _render_prompt(packet: dict, base_sha: str) -> str:
    task = packet["task"]
    lines = _registry_task_prompt_lines(task)
    routing_note = packet.get("routing_note")
    if (isinstance(routing_note, dict)
            and routing_note.get("provenance") == "main-session"
            and isinstance(routing_note.get("note"), str)
            and routing_note["note"] and "\n" not in routing_note["note"]
            and "\r" not in routing_note["note"]):
        lines.append(f"- routing_note: {routing_note['note']}")
    acceptance = _acceptance_prompt_text(packet["acceptance"])
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
    env = {**os.environ, "UV_CACHE_DIR": str(worktree / ".waystone-uv-cache")}
    env.pop(_VERIFIER_SESSION_ENV, None)  # prep belongs to writable implementer RUN scope
    for cmd in commands:
        try:
            p = subprocess.run(shlex.split(cmd), cwd=str(worktree),
                               capture_output=True, text=True, timeout=600, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, f"{cmd}: {e}"
        if p.returncode != 0:
            return p.returncode, "\n".join(p.stderr.strip().splitlines()[-20:])
    return 0, ""


# ---- runners (§6 — isolated and transport-injectable for tests) ---------------
def _codex_exec_command(
        codex_path: str, worktree: Path, model: str, effort: str | None) -> list[str]:
    """Build the shared Codex command prefix so probe and task use the same sandbox policy."""
    cmd = [codex_path, "exec", "-C", str(worktree), "-m", model, "-s", "workspace-write"]
    if effort is not None:
        cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
    return cmd


def _sandbox_failure_line(text: str) -> str | None:
    """Return one concrete sandbox/write failure line, independent of token order."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        success_narrative = _SANDBOX_SUCCESS_NARRATIVE_RE.search(line) is not None
        strong_failure = _SANDBOX_STRONG_FAILURE_TERM_RE.search(line) is not None
        if success_narrative and not strong_failure:
            continue
        if (_SANDBOX_MECHANISM_RE.search(line)
                and _SANDBOX_FAILURE_TERM_RE.search(line)):
            return line
        if _SANDBOX_WRITE_OPERATION_FAILURE_RE.search(line):
            return line
    return None


def _sandbox_probe_worktree_path(worktree: Path, record_dir: Path) -> Path:
    """A deterministic, owned sibling path on the same cache filesystem as the task worktree."""
    suffix = hashlib.sha256(str(record_dir.resolve()).encode()).hexdigest()[:12]
    return worktree.resolve().parent / f".{worktree.name}-sandbox-probe-{suffix}"


def _probe_worktree_registered(worktree: Path, probe_worktree: Path) -> bool:
    rc, listed, err = _git(worktree, "worktree", "list", "--porcelain")
    if rc != 0:
        raise WorkflowError(
            f"cannot inspect sandbox probe worktree registration: {err or listed or f'rc {rc}'}")
    target = os.path.realpath(probe_worktree)
    paths = [line.removeprefix("worktree ") for line in listed.splitlines()
             if line.startswith("worktree ")]
    return any(os.path.realpath(path) == target for path in paths)


def _cleanup_sandbox_probe_worktree(worktree: Path, probe_worktree: Path) -> None:
    """Remove an owned probe sibling, including partial or stale Git registrations, then prove it."""
    if _probe_worktree_registered(worktree, probe_worktree):
        rc, out, err = _git(
            worktree, "worktree", "remove", "--force", str(probe_worktree), timeout=30)
        if rc != 0 and (_probe_worktree_registered(worktree, probe_worktree)
                        or os.path.lexists(probe_worktree)):
            raise WorkflowError(
                "sandbox probe cleanup failed at git worktree remove: "
                f"{err or out or f'rc {rc}'}")
    if os.path.lexists(probe_worktree):
        try:
            if probe_worktree.is_symlink() or not probe_worktree.is_dir():
                probe_worktree.unlink()
            else:
                shutil.rmtree(probe_worktree)
        except OSError as e:
            raise WorkflowError(
                f"sandbox probe cleanup could not remove partial path {probe_worktree}: {e}") from e
    rc, out, err = _git(worktree, "worktree", "prune", timeout=30)
    if rc != 0:
        raise WorkflowError(
            f"sandbox probe cleanup failed at git worktree prune: {err or out or f'rc {rc}'}")
    if os.path.lexists(probe_worktree) or _probe_worktree_registered(worktree, probe_worktree):
        raise WorkflowError(
            f"sandbox probe cleanup postcondition failed: sibling remains at {probe_worktree}")


def _write_sandbox_probe_result(record_dir: Path, result: dict) -> str | None:
    """Best-effort atomic persistence; return an error string instead of leaking an OSError."""
    try:
        write_text_atomic(
            record_dir / "sandbox-probe-result.json",
            json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        return str(e)
    return None


def _read_sandbox_probe_result(record_dir: Path) -> dict | None:
    path = record_dir / "sandbox-probe-result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(f"cannot read sandbox probe result {path}: {e}") from e
    if not isinstance(value, dict):
        raise WorkflowError(f"invalid sandbox probe result {path}: expected object")
    return value


def _probe_failure_exception(classification: str):
    return {
        "sandbox": _RunnerSandboxUnusable,
        "transport": _RunnerProbeTransportFailure,
        "evidence": _RunnerProbeEvidenceFailure,
        "lifecycle": _RunnerProbeLifecycleFailure,
    }.get(classification, _RunnerProbeFailure)


def _probe_transport_kind(rc: int | None, stderr: str) -> str:
    if _PROBE_AUTH_FAILURE_RE.search(stderr):
        return "authentication"
    if _PROBE_NETWORK_FAILURE_RE.search(stderr):
        return "network"
    if rc == 124:
        return "timeout"
    if rc == 127:
        return "launch"
    return "runner-exit"


def _run_codex_sandbox_probe(
        worktree: Path, model: str, record_dir: Path, *, effort: str | None = None,
        fingerprint: dict | None = None) -> dict:
    """Prove one sandboxed write in a disposable sibling and record every failure path."""
    if fingerprint is None:
        fingerprint = _codex_runner_fingerprint(worktree)
    start = time.monotonic()
    probe_worktree = _sandbox_probe_worktree_path(worktree, record_dir)
    probe_name = f".waystone-sandbox-write-probe-{record_dir.name}"
    probe_path = probe_worktree / probe_name
    expected = b"waystone-sandbox-write-probe\n"
    stderr_path = record_dir / "sandbox-probe.stderr"
    result = {
        "schema": "waystone-sandbox-probe-1",
        "worktree": str(probe_worktree),
        "outcome": "running",
        "classification": None,
        "transport_kind": None,
        "detail": None,
        "rc": None,
        "duration_s": 0.0,
        "stale_detected": False,
        "stale_cleanup_state": "not-needed",
        "creation_state": "not-attempted",
        "cleanup_state": "not-needed",
    }
    failure: tuple[str, str] | None = None

    def persist_before_mutation() -> bool:
        nonlocal failure
        error = _write_sandbox_probe_result(record_dir, result)
        if error is None:
            return True
        failure = ("evidence", f"cannot record sandbox probe lifecycle: {error}")
        return False

    try:
        try:
            stale = (os.path.lexists(probe_worktree)
                     or _probe_worktree_registered(worktree, probe_worktree))
        except WorkflowError as e:
            failure = ("lifecycle", str(e))
            stale = False
        if stale and failure is None:
            result["stale_detected"] = True
            result["stale_cleanup_state"] = "attempted"
            if persist_before_mutation():
                try:
                    _cleanup_sandbox_probe_worktree(worktree, probe_worktree)
                    result["stale_cleanup_state"] = "cleaned"
                except WorkflowError as e:
                    result["stale_cleanup_state"] = "failed"
                    failure = ("lifecycle", f"stale sandbox probe worktree: {e}")

        if failure is None:
            result["creation_state"] = "attempted"
            persist_before_mutation()
        if failure is None:
            try:
                base_sha = _git_out(worktree, "rev-parse", "HEAD")
            except WorkflowError as e:
                failure = ("lifecycle", f"sandbox probe base resolution failed: {e}")
        if failure is None:
            rc, out, err = _git(
                worktree, "worktree", "add", "--detach", str(probe_worktree), base_sha,
                timeout=30)
            if rc != 0:
                result["creation_state"] = "partial-or-failed"
                failure = (
                    "lifecycle",
                    f"sandbox probe sibling creation failed: {err or out or f'rc {rc}'}")
            else:
                result["creation_state"] = "created"

        if failure is None:
            try:
                probed_mount = _worktree_mount_identity(probe_worktree)
            except WorkflowError as e:
                failure = ("evidence", str(e))
            else:
                result["worktree_cache_mount"] = probed_mount
                if probed_mount != fingerprint.get("worktree_cache_mount"):
                    failure = (
                        "evidence",
                        "sandbox probe target mount does not match the runtime fingerprint; "
                        "refusing to prove one mount and record another",
                    )

        if failure is None:
            codex_path = fingerprint.get("resolved_codex_path")
            if not isinstance(codex_path, str) or not Path(codex_path).is_absolute():
                failure = ("evidence", "runtime fingerprint has no resolved absolute Codex path")

        if failure is None:
            cmd = _codex_exec_command(codex_path, probe_worktree, model, effort)
            cmd.extend([
                "--ephemeral", "--color", "never", "--output-last-message",
                str(record_dir / "sandbox-probe-last-message.md"), "--json",
                f"Use the shell tool to run exactly: "
                f"printf 'waystone-sandbox-write-probe\\n' > {probe_name}",
            ])
            env = {**os.environ, "UV_CACHE_DIR": str(probe_worktree / ".waystone-uv-cache")}
            env.pop(_VERIFIER_SESSION_ENV, None)
            try:
                with open(record_dir / "sandbox-probe.jsonl", "w", encoding="utf-8") as jout, \
                     open(stderr_path, "w", encoding="utf-8") as jerr:
                    try:
                        proc = subprocess.run(
                            cmd, stdout=jout, stderr=jerr, timeout=180, env=env)
                        result["rc"] = proc.returncode
                    except subprocess.TimeoutExpired:
                        result["rc"] = 124
                    except OSError as e:
                        jerr.write(str(e))
                        result["rc"] = 127
            except OSError as e:
                failure = ("evidence", f"cannot collect sandbox probe process output: {e}")

        stderr = ""
        actual = None
        if failure is None:
            try:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                failure = ("evidence", f"cannot read sandbox probe stderr {stderr_path}: {e}")
        if failure is None:
            try:
                actual = probe_path.read_bytes() if probe_path.is_file() else None
            except OSError as e:
                failure = ("evidence", f"cannot read sandbox probe file {probe_path}: {e}")
        if failure is None:
            sandbox_line = _sandbox_failure_line(stderr)
            if result["rc"] != 0:
                if sandbox_line is not None:
                    failure = ("sandbox", sandbox_line)
                else:
                    detail = stderr.strip() or f"sandbox probe exited rc {result['rc']}"
                    failure = ("transport", detail)
            elif actual != expected:
                if sandbox_line is not None:
                    failure = ("sandbox", sandbox_line)
                elif (_PROBE_AUTH_FAILURE_RE.search(stderr)
                      or _PROBE_NETWORK_FAILURE_RE.search(stderr)):
                    failure = ("transport", stderr.strip())
                else:
                    failure = (
                        "write-proof", "sandbox probe did not create the expected worktree file")
    finally:
        result["cleanup_state"] = "attempted"
        _write_sandbox_probe_result(record_dir, result)
        try:
            _cleanup_sandbox_probe_worktree(worktree, probe_worktree)
            result["cleanup_state"] = "cleaned"
        except WorkflowError as e:
            result["cleanup_state"] = "failed"
            prior = f"; prior failure: {failure[1]}" if failure is not None else ""
            failure = ("lifecycle", f"sandbox probe sibling cleanup failed: {e}{prior}")
        result["duration_s"] = round(time.monotonic() - start, 3)

    if failure is None:
        result["outcome"] = "passed"
    else:
        result["outcome"] = "failed"
        result["classification"], result["detail"] = failure
        if result["classification"] == "transport":
            result["transport_kind"] = _probe_transport_kind(result["rc"], stderr)
    record_error = _write_sandbox_probe_result(record_dir, result)
    if record_error is not None and failure is None:
        failure = ("evidence", f"cannot record final sandbox probe result: {record_error}")
        result["outcome"] = "failed"
        result["classification"], result["detail"] = failure
        _write_sandbox_probe_result(record_dir, result)
    if failure is not None:
        classification, detail = failure
        prefix = {
            "sandbox": "runner sandbox unusable",
            "transport": "runner preflight transport failed",
            "evidence": "runner preflight evidence failed",
            "lifecycle": "runner preflight lifecycle failed",
            "write-proof": "runner sandbox write proof failed",
        }.get(classification, "runner preflight failed")
        raise _probe_failure_exception(classification)(f"{prefix}: {detail}", result)
    return result


def _worktree_mount_identity(worktree: Path) -> dict:
    """Identify the storage boundary containing the disposable runner worktree."""
    try:
        resolved = worktree.resolve(strict=True)
        device = resolved.stat().st_dev
        device_boundary = resolved
        while device_boundary.parent != device_boundary:
            if device_boundary.parent.stat().st_dev != device:
                break
            device_boundary = device_boundary.parent
        filesystem = os.statvfs(resolved)
    except OSError as e:
        raise WorkflowError(
            f"cannot identify Codex runner worktree cache mount for {worktree}: {e}") from e
    # This deliberately identifies only a device/fsid boundary. A bind-mount reconfiguration that
    # preserves both values is outside this lightweight proof's detection scope.
    return {
        "device_boundary": str(device_boundary),
        "device": device,
        "filesystem_id": int(filesystem.f_fsid),
        "readonly": bool(filesystem.f_flag & os.ST_RDONLY),
    }


def _stable_host_identity(system: str) -> dict:
    """Return a stable host identifier stronger than a mutable hostname."""
    if system == "Linux":
        source = Path("/etc/machine-id")
        try:
            value = source.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise WorkflowError(f"cannot read Linux machine identity from {source}: {e}") from e
        if not value or value.casefold() == "uninitialized":
            raise WorkflowError(
                f"cannot read Linux machine identity from {source}: invalid sentinel or empty "
                "value")
        if re.fullmatch(r"[0-9a-f]{32}", value) is None or value == "0" * 32:
            raise WorkflowError(
                f"cannot read Linux machine identity from {source}: expected a non-zero "
                "32-character lowercase hexadecimal value")
        return {"source": str(source), "value": value}
    if system == "Darwin":
        try:
            process = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorkflowError(f"cannot query macOS IOPlatformUUID: {e}") from e
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', process.stdout)
        if process.returncode != 0 or match is None:
            detail = process.stderr.strip() or f"rc {process.returncode}"
            raise WorkflowError(f"cannot query macOS IOPlatformUUID: {detail}")
        value = match.group(1)
        if _IOPLATFORM_UUID_RE.fullmatch(value) is None:
            raise WorkflowError("cannot query macOS IOPlatformUUID: invalid UUID format")
        return {"source": "IOPlatformUUID", "value": value}
    raise WorkflowError(
        f"cannot fingerprint Codex runner: no stable host identity source for {system}")


def _host_sandbox_observation(system: str) -> dict:
    """Record only cheap host sandbox state that is directly observable.

    Linux's LSM inventory does not describe the complete policy applied to a future Codex process.
    It is kept separate from the invocation contract and is therefore best-effort evidence only.
    """
    if system != "Linux":
        return {"source": "none", "status": "not-observed", "platform": system}
    source = Path("/sys/kernel/security/lsm")
    try:
        value = source.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {"source": str(source), "status": "not-present"}
    except OSError:
        return {"source": str(source), "status": "unavailable"}
    return {"source": str(source), "status": "observed", "value": value}


def _execution_principal_identity() -> dict:
    """Return the effective POSIX principal; an incomplete identity is not reusable proof."""
    try:
        effective_uid = os.geteuid()
        effective_gid = os.getegid()
        supplementary_groups = os.getgroups()
        if (not isinstance(effective_uid, int) or isinstance(effective_uid, bool)
                or effective_uid < 0):
            raise ValueError(f"invalid effective UID {effective_uid!r}")
        if (not isinstance(effective_gid, int) or isinstance(effective_gid, bool)
                or effective_gid < 0):
            raise ValueError(f"invalid effective GID {effective_gid!r}")
        if not isinstance(supplementary_groups, list):
            raise ValueError("supplementary groups are not a list")
        if any(not isinstance(group, int) or isinstance(group, bool) or group < 0
               for group in supplementary_groups):
            raise ValueError(f"invalid supplementary groups {supplementary_groups!r}")
    except (AttributeError, OSError, TypeError, ValueError) as e:
        raise WorkflowError(
            f"cannot fingerprint Codex runner execution principal: {e}") from e
    return {
        "effective_uid": effective_uid,
        "effective_gid": effective_gid,
        "supplementary_groups": sorted(set(supplementary_groups)),
    }


def _codex_config_root_identity() -> dict:
    """Resolve the effective Codex config root and record its filesystem identity."""
    configured = os.environ.get("CODEX_HOME")
    source = "CODEX_HOME" if configured else "default"
    configured_path = configured if configured else "~/.codex"
    try:
        candidate = Path(configured).expanduser() if configured else Path.home() / ".codex"
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        return {
            "source": source,
            "configured_path": configured_path,
            "resolved_path": None,
            "status": "not-observed",
            "reason": type(e).__name__,
        }
    identity = {
        "source": source,
        "configured_path": configured_path,
        "resolved_path": str(resolved),
    }
    try:
        info = resolved.stat()
    except FileNotFoundError:
        return {**identity, "status": "not-present"}
    except OSError as e:
        return {**identity, "status": "not-observed", "reason": type(e).__name__}
    return {
        **identity,
        "status": "present",
        "stat": {
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": info.st_mode,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        },
    }


def _not_observed_process_axis(source: Path, reason: str) -> dict:
    return {"source": str(source), "status": "not-observed", "reason": reason}


def _process_security_context(system: str) -> dict:
    """Record cheap process-local Linux security axes, with explicit observation gaps."""
    status_source = Path("/proc/self/status")
    label_source = Path("/proc/self/attr/current")
    status_fields = ("Seccomp", "NoNewPrivs", "CapEff")
    if system != "Linux":
        return {
            field: _not_observed_process_axis(status_source, "unsupported-platform")
            for field in status_fields
        } | {
            "security_label": _not_observed_process_axis(
                label_source, "unsupported-platform"),
        }

    try:
        status_text = status_source.read_text(encoding="utf-8")
    except FileNotFoundError:
        status_reason = "not-present"
        status_values = None
    except OSError:
        status_reason = "unavailable"
        status_values = None
    except UnicodeError:
        status_reason = "invalid-encoding"
        status_values = None
    else:
        status_reason = "missing"
        status_values = {}
        for line in status_text.splitlines():
            name, separator, value = line.partition(":")
            if separator and name in status_fields and value.strip():
                status_values[name] = value.strip()

    context = {}
    for field in status_fields:
        if status_values is not None and field in status_values:
            context[field] = {
                "source": str(status_source),
                "status": "observed",
                "value": status_values[field],
            }
        else:
            context[field] = _not_observed_process_axis(status_source, status_reason)

    try:
        label = label_source.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        context["security_label"] = _not_observed_process_axis(label_source, "not-present")
    except OSError:
        context["security_label"] = _not_observed_process_axis(label_source, "unavailable")
    except UnicodeError:
        context["security_label"] = _not_observed_process_axis(
            label_source, "invalid-encoding")
    else:
        context["security_label"] = (
            {"source": str(label_source), "status": "observed", "value": label}
            if label else _not_observed_process_axis(label_source, "empty")
        )
    return context


def _codex_runner_fingerprint(worktree: Path) -> dict:
    """Collect the machine/runtime axes that make one sandbox probe reusable."""
    executable = shutil.which("codex")
    if executable is None:
        raise WorkflowError("cannot fingerprint Codex runner: codex executable is not on PATH")
    try:
        resolved = Path(executable).resolve(strict=True)
        info = resolved.stat()
    except OSError as e:
        raise WorkflowError(
            f"cannot resolve Codex runner executable {executable}: {e}") from e
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise WorkflowError(
            f"cannot fingerprint Codex runner: resolved executable is not an executable file: "
            f"{resolved}")
    try:
        version_process = subprocess.run(
            [str(resolved), "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkflowError(f"cannot query Codex runner version from {resolved}: {e}") from e
    version = {
        "stdout": version_process.stdout.strip(),
        "stderr": version_process.stderr.strip(),
    }
    if version_process.returncode != 0 or not any(version.values()):
        detail = "\n".join(value for value in version.values() if value)
        raise WorkflowError(
            f"cannot query Codex runner version from {resolved}: "
            f"{detail or f'rc {version_process.returncode}'}")
    uname = platform.uname()
    if not all((uname.node, uname.system, uname.machine, uname.release, uname.version)):
        raise WorkflowError("cannot fingerprint Codex runner: platform identity is incomplete")
    return {
        "schema": _CODEX_RUNNER_PROOF_SCHEMA,
        "resolved_codex_path": str(resolved),
        "codex_version": version,
        "codex_executable": {"size": info.st_size, "mtime_ns": info.st_mtime_ns},
        "machine": uname.node,
        "host_identity": _stable_host_identity(uname.system),
        "platform": {"system": uname.system, "machine": uname.machine},
        "kernel": {"release": uname.release, "version": uname.version},
        "sandbox_invocation_contract": _CODEX_SANDBOX_INVOCATION_CONTRACT,
        "host_sandbox_observation": _host_sandbox_observation(uname.system),
        "execution_principal": _execution_principal_identity(),
        "codex_config_root": _codex_config_root_identity(),
        "process_context": _process_security_context(uname.system),
        "worktree_cache_mount": _worktree_mount_identity(worktree),
    }


def _codex_runner_proof_text(fingerprint: dict) -> str:
    return json.dumps(
        fingerprint, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _codex_runner_comparison_view(fingerprint: dict) -> dict:
    """Return deterministic proof axes; version stderr is retained only as diagnostics."""
    comparable = json.loads(json.dumps(fingerprint, ensure_ascii=False))
    version = comparable.get("codex_version")
    if isinstance(version, dict):
        # Presence and string type remain contractual; only the dynamic diagnostic value is ignored.
        # Residual: a static stderr-only version shim can replace its child without detection when
        # the shim's own size and mtime_ns also stay fixed. Comparing stderr would instead make
        # ordinary timestamp/PID diagnostics invalidate every proof, so stderr remains record-only.
        if isinstance(version.get("stderr"), str):
            version["stderr"] = "<recorded-diagnostic>"
    process_context = comparable.get("process_context")
    if isinstance(process_context, dict):
        for axis in ("Seccomp", "NoNewPrivs", "CapEff", "security_label"):
            value = process_context.get(axis)
            if isinstance(value, dict) and value.get("status") == "not-observed":
                # An unavailable observation has no security value to compare. Preserve the
                # source and state, but do not turn equivalent observation failures into a
                # permanent probe loop merely because their diagnostic reason changed.
                process_context[axis] = {
                    "source": value.get("source"), "status": "not-observed",
                }
    return comparable


def _codex_runner_mismatch_axes(recorded: dict, current: dict) -> list[str]:
    recorded_view = _codex_runner_comparison_view(recorded)
    current_view = _codex_runner_comparison_view(current)
    return sorted(
        key for key in set(recorded_view) | set(current_view)
        if key not in recorded_view or key not in current_view
        or json.dumps(recorded_view.get(key), ensure_ascii=False, sort_keys=True)
        != json.dumps(current_view.get(key), ensure_ascii=False, sort_keys=True))


def _codex_runner_reuse_blockers(fingerprint: dict) -> list[str]:
    """Name malformed or unavailable context axes that cannot support reusable proof."""
    blockers = []
    principal = fingerprint.get("execution_principal")
    if (not isinstance(principal, dict)
            or not isinstance(principal.get("effective_uid"), int)
            or isinstance(principal.get("effective_uid"), bool)
            or principal.get("effective_uid", -1) < 0
            or not isinstance(principal.get("effective_gid"), int)
            or isinstance(principal.get("effective_gid"), bool)
            or principal.get("effective_gid", -1) < 0
            or not isinstance(principal.get("supplementary_groups"), list)
            or any(not isinstance(group, int) or isinstance(group, bool) or group < 0
                   for group in principal.get("supplementary_groups", []))):
        blockers.append("execution_principal")

    config_root = fingerprint.get("codex_config_root")
    if (not isinstance(config_root, dict)
            or config_root.get("status") not in ("present", "not-present")):
        blockers.append("codex_config_root")

    process_context = fingerprint.get("process_context")
    for axis in ("Seccomp", "NoNewPrivs", "CapEff", "security_label"):
        value = process_context.get(axis) if isinstance(process_context, dict) else None
        status_value = value.get("status") if isinstance(value, dict) else None
        if (not isinstance(value, dict)
                or not isinstance(value.get("source"), str) or not value["source"]
                or status_value not in ("observed", "not-observed")
                or (status_value == "observed"
                    and (not isinstance(value.get("value"), str) or not value["value"]))
                or (status_value == "not-observed"
                    and (not isinstance(value.get("reason"), str) or not value["reason"]))):
            blockers.append(f"process_context.{axis}")
    return blockers


def _record_codex_runner_verified(marker_path: Path, fingerprint: dict) -> None:
    """Record the checkout-local, runtime-bound Codex runner proof atomically."""
    try:
        write_text_atomic(marker_path, _codex_runner_proof_text(fingerprint))
    except OSError as e:
        raise WorkflowError(f"cannot record Codex runner proof at {marker_path}: {e}") from e


def _codex_runner_marker_recorded(
        marker_path: Path, fingerprint: dict, *, diagnose: bool = True) -> bool:
    try:
        info = marker_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise WorkflowError(f"cannot inspect Codex runner proof at {marker_path}: {e}") from e
    project_root = marker_path.parent.parent
    try:
        relative = marker_path.relative_to(project_root).as_posix()
    except ValueError as e:
        raise WorkflowError(
            f"Codex runner proof is outside its project root: {marker_path}") from e
    rc, tracked, error = _git(project_root, "ls-files", "--stage", "--", relative)
    if rc != 0:
        raise WorkflowError(
            f"cannot verify that Codex runner proof is checkout-local with git ls-files: "
            f"{error or f'rc {rc}'}")
    if tracked:
        if diagnose:
            print(
                f"waystone: checkout-local Codex runner proof {marker_path} is git-tracked "
                f"and is ignored; from {project_root}, run git rm --cached -- {relative}; "
                "a fresh preflight probe will run",
                file=sys.stderr,
            )
        return False
    if not stat.S_ISREG(info.st_mode):
        if diagnose:
            print(
                f"waystone: invalid checkout-local Codex runner proof at {marker_path} is "
                "ignored; a fresh preflight probe will run and replace it with the versioned "
                "JSON contract",
                file=sys.stderr,
            )
        return False
    try:
        content = marker_path.read_bytes()
    except OSError as e:
        raise WorkflowError(f"cannot read Codex runner proof at {marker_path}: {e}") from e
    if content == b"verified\n":
        if diagnose:
            print(
                f"waystone: legacy fixed-string Codex runner proof at {marker_path} is ignored; "
                "a fresh preflight probe will run and replace it with the versioned JSON contract",
                file=sys.stderr,
            )
        return False
    try:
        recorded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        recorded = None
    if not isinstance(recorded, dict):
        if diagnose:
            print(
                f"waystone: invalid checkout-local Codex runner proof at {marker_path} is "
                "ignored; a fresh preflight probe will run and replace it with the versioned "
                "JSON contract",
                file=sys.stderr,
            )
        return False
    axes = _codex_runner_mismatch_axes(recorded, fingerprint)
    if axes:
        if diagnose:
            print(
                f"waystone: checkout-local Codex runner proof fingerprint mismatch at "
                f"{marker_path} ({', '.join(axes)}); a fresh preflight probe will run and "
                "replace it with the current runtime contract",
                file=sys.stderr,
            )
        return False
    blockers = sorted(set(
        _codex_runner_reuse_blockers(recorded)
        + _codex_runner_reuse_blockers(fingerprint)))
    if blockers:
        if diagnose:
            print(
                f"waystone: checkout-local Codex runner proof at {marker_path} cannot be reused "
                f"because runtime context is incomplete ({', '.join(blockers)}); a fresh "
                "preflight probe will run",
                file=sys.stderr,
            )
        return False
    return True


def _codex_runner_verification_marker(
        record_dir: Path, fingerprint: dict) -> tuple[Path, bool, dict] | None:
    """The checkout-local marker and whether its one-time runner proof is already recorded."""
    if not (record_dir / "exposure.json").exists():
        return None  # low-level probe contracts have no project exposure and always exercise probe
    exposure = _load_exposure(record_dir)
    project = exposure.get("project")
    root = project.get("root") if isinstance(project, dict) else None
    if not isinstance(root, str) or not root:
        raise WorkflowError(f"corrupt exposure project root: {record_dir / 'exposure.json'}")
    project_root = Path(root)
    load_config(project_root)  # validate config and surface legacy-key removal guidance
    marker_path = ensure_project_state_dir(project_root) / _CODEX_RUNNER_VERIFIED_MARKER
    return marker_path, _codex_runner_marker_recorded(marker_path, fingerprint), fingerprint


def _run_codex(worktree: Path, model: str, prompt_path: Path, record_dir: Path,
               *, effort: str | None = None) -> tuple[int, float]:
    """Invoke `codex exec` in the worktree (workspace-write sandbox hardcoded, S8). Returns (rc,
    duration_s). The full --json stream and last message are preserved as local evidence.

    RUN is an implementer session, not a verifier session: lifecycle hooks may seed ignored
    `.waystone` project state here. Result capture still includes only Git material."""
    start = time.monotonic()
    try:
        runtime_fingerprint = _codex_runner_fingerprint(worktree)

        def probe_if_needed(verification: tuple[Path, bool, dict] | None) -> None:
            nonlocal runtime_fingerprint
            if verification is not None and verification[1]:
                runtime_fingerprint = verification[2]
                return
            expected_fingerprint = verification[2] if verification is not None \
                else runtime_fingerprint
            probe = _run_codex_sandbox_probe(
                worktree, model, record_dir, effort=effort,
                fingerprint=expected_fingerprint)
            current_fingerprint = _codex_runner_fingerprint(worktree)
            axes = _codex_runner_mismatch_axes(expected_fingerprint, current_fingerprint)
            if axes:
                raise _RunnerProbeEvidenceFailure(
                    "runner preflight passed but the Codex runtime fingerprint changed during "
                    f"the probe ({', '.join(axes)}); refusing to record stale proof",
                    probe,
                )
            probed_mount = probe.get("worktree_cache_mount")
            if probed_mount != current_fingerprint.get("worktree_cache_mount"):
                raise _RunnerProbeEvidenceFailure(
                    "runner preflight passed but its write target mount differs from the "
                    "current runtime fingerprint; refusing to record stale proof",
                    probe,
                )
            # Source the recorded mount axis from the path where the probe actually wrote.
            current_fingerprint["worktree_cache_mount"] = probed_mount
            runtime_fingerprint = current_fingerprint
            if verification is None:
                return
            try:
                _record_codex_runner_verified(verification[0], current_fingerprint)
            except WorkflowError as e:
                raise _RunnerProbeEvidenceFailure(
                    f"runner preflight passed but its checkout-local proof could not be "
                    f"recorded: {e}", probe) from e

        verification = _codex_runner_verification_marker(record_dir, runtime_fingerprint)
        if verification is None:
            probe_if_needed(None)
        else:
            lock_path = verification[0].parent / _CODEX_RUNNER_VERIFICATION_LOCK
            lock_stream = None
            try:
                lock_stream = lock_path.open("a+", encoding="utf-8")
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                if lock_stream is not None:
                    lock_stream.close()
                raise _RunnerProbeEvidenceFailure(
                    f"cannot lock checkout-local Codex runner proof {lock_path}: {e}") from e
            try:
                locked_fingerprint = _codex_runner_fingerprint(worktree)
                probe_if_needed(
                    (verification[0], _codex_runner_marker_recorded(
                        verification[0], locked_fingerprint, diagnose=True), locked_fingerprint))
            finally:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
                lock_stream.close()
    except _RunnerProbeFailure:
        raise
    except Exception as e:  # noqa: BLE001 — a harness defect must not masquerade as transport
        prior = None
        try:
            prior = _read_sandbox_probe_result(record_dir)
        except WorkflowError:
            pass
        result = prior if isinstance(prior, dict) else {"schema": "waystone-sandbox-probe-1"}
        result.update({"outcome": "failed", "classification": "internal",
                       "detail": f"{type(e).__name__}: {e}"})
        _write_sandbox_probe_result(record_dir, result)
        raise _RunnerProbeFailure(
            f"runner preflight internal error (harness defect, not transport): "
            f"{type(e).__name__}: {e}", result) from e
    codex_path = runtime_fingerprint["resolved_codex_path"]
    cmd = _codex_exec_command(codex_path, worktree, model, effort)
    cmd.extend(["--color", "never", "--output-last-message",
                str(record_dir / "last_message.md"), "--json"])
    env = {**os.environ, "UV_CACHE_DIR": str(worktree / ".waystone-uv-cache")}
    env.pop(_VERIFIER_SESSION_ENV, None)
    try:
        with open(prompt_path, encoding="utf-8") as pin, \
             open(record_dir / "runner.jsonl", "w", encoding="utf-8") as jout, \
             open(record_dir / "runner.stderr", "w", encoding="utf-8") as jerr:
            p = subprocess.run(cmd, stdin=pin, stdout=jout, stderr=jerr, timeout=3600, env=env)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:
        (record_dir / "runner.stderr").write_text(str(e), encoding="utf-8")
        rc = 127
    return rc, round(time.monotonic() - start, 3)


def _runner_environment_failure_reason(record_dir: Path, empty: bool, report: dict) -> str | None:
    """Classify only the rc0 empty/missing-report contract with concrete stderr failure evidence."""
    if not empty or report.get("present") is not False:
        return None
    stderr_path = record_dir / "runner.stderr"
    try:
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise WorkflowError(f"cannot read runner stderr {stderr_path}: {e}") from e
    failure_line = _sandbox_failure_line(stderr)
    if failure_line is None:
        return None
    return (
        "runner environment failure despite rc 0: empty patch and missing delegate report; "
        f"runner.stderr indicates sandbox/tool write failure: {failure_line}")


def _runner_sandbox_diagnostic_hint(text: str) -> str | None:
    failure_line = _sandbox_failure_line(text)
    if failure_line is None:
        return None
    verified_hint = (
        "; remove .waystone/codex-runner-verified to "
        "require a fresh preflight probe after runner-environment changes"
    )
    lower = failure_line.lower()
    if "landlock" in lower:
        return (
            "Landlock sandbox initialization failed; check kernel Landlock support and "
            "the host sandbox denial logs" + verified_hint)
    if any(token in lower for token in (
            "bwrap", "bubblewrap", "userns", "user namespace",
            "unprivileged_userns", "rtm_newaddr")):
        return (
            "AppArmor may be blocking bwrap user namespaces; check "
            "kernel.apparmor_restrict_unprivileged_userns and /etc/apparmor.d/bwrap"
            + verified_hint)
    if any(token in lower for token in ("seatbelt", "sandbox-exec", "sandbox_apply")):
        return (
            "macOS seatbelt (sandbox-exec) denied the operation; check the Codex sandbox "
            "profile and host sandbox denial logs" + verified_hint)
    return None


def _run_claude(worktree: Path, model: str, prompt_path: Path, record_dir: Path,
                *, effort: str | None = None, runner=None) -> tuple[int, float]:
    """Invoke the explicitly overridden unsandboxed Claude implementer transport.

    Like Codex RUN, this intentionally does not set the verifier-session guard; ignored `.waystone`
    state seeded by implementer lifecycle hooks is outside the captured Git result."""
    cmd = ["claude", "-p", "--model", model, *_CLAUDE_COMMON_ARGS]
    if effort is not None:
        cmd.extend(["--effort", effort])
    cmd.extend([
        "--tools", "Read,Edit,Write,Glob,Grep,Bash",
        "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
        "--disallowedTools", _CLAUDE_NETWORK_DENY,
        "--output-format", "stream-json", "--verbose",
    ])
    start = time.monotonic()
    invoke = runner or subprocess.run
    stream_path = record_dir / "runner.jsonl"
    env = dict(os.environ)
    env.pop(_VERIFIER_SESSION_ENV, None)
    try:
        with open(prompt_path, encoding="utf-8") as pin, \
             open(stream_path, "w", encoding="utf-8") as jout, \
             open(record_dir / "runner.stderr", "w", encoding="utf-8") as jerr:
            proc = invoke(
                cmd, cwd=str(worktree), stdin=pin, stdout=jout, stderr=jerr,
                timeout=3600, env=env, text=True,
            )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:
        (record_dir / "runner.stderr").write_text(str(e), encoding="utf-8")
        rc = 127

    last_message = ""
    try:
        if stream_path.is_file():
            for line_number, line in enumerate(
                    stream_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"invalid stream-json at line {line_number}: {e}") from e
                if not isinstance(event, dict):
                    raise ValueError(
                        f"invalid stream-json event at line {line_number}: expected object")
                if event.get("type") == "result" and isinstance(event.get("result"), str):
                    last_message = event["result"]
        (record_dir / "last_message.md").write_text(last_message, encoding="utf-8")
    except (OSError, ValueError) as e:
        try:
            with open(record_dir / "runner.stderr", "a", encoding="utf-8") as stream:
                stream.write(f"\nclaude transport output failure: {e}\n")
        except OSError:
            pass
        if rc == 0:
            rc = 125
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


def _set_state(record_dir: Path, state: str, *, env: dict | None = None, error: str | None = None,
               reason: str | None = None, overrides: list[str] | None = None,
               verification_required: bool | None = None,
               probe: dict | None = None) -> dict:
    st = _read_status_raw(record_dir) or {}  # a corrupt file is superseded — discard IS the recovery path
    transition = {"state": state, "at": _now_iso()}
    if reason is not None:
        transition["reason"] = reason
    if overrides is not None:
        transition["overrides"] = overrides
    st.setdefault("at_transitions", []).append(transition)
    st["state"] = state
    if state == "applied":
        st["accepted_at"] = transition["at"]
    if env is not None:
        st["env"] = env
    if error is not None:
        st["error"] = error
    if probe is not None:
        st["probe"] = probe
    if verification_required is not None:
        st["verification_required"] = verification_required
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
        if sub.is_dir() and ((sub / "claim.json").exists() or (sub / "exposure.json").exists()):
            yield sub.name, sub


def _load_claim(rec: Path) -> dict:
    p = rec / "claim.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WorkflowError(f"corrupt claim.json in delegation record: {p} ({e})")
    if (not isinstance(data, dict) or data.get("schema") != "waystone-delegation-claim-1"
            or not isinstance(data.get("task_id"), str)):
        raise WorkflowError(f"corrupt claim.json in delegation record: {p}")
    return data


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
            claim_only = not (sub / "exposure.json").exists()
            tid = (_load_claim(sub) if claim_only else _load_exposure(sub)).get("task_id")
        except WorkflowError:
            tid = None
            claim_only = not (sub / "exposure.json").exists()
        if tid is not None and tid != task_id:
            continue  # a healthy record of another task never blocks this one
        if st is None or tid is None:
            broken = "status.json" if st is None else ("claim.json" if claim_only else "exposure.json")
            raise WorkflowError(
                f"delegation record {did} has a corrupt {broken} — treated as an active lock (fail-safe); "
                f"run `waystone delegate discard {did}` to clear it")
        return did, "claimed" if claim_only and state is None else state
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


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _artifact_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WorkflowError(f"{label} must be a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as e:
        raise WorkflowError(f"cannot read {label} {path}: {e}") from e


def _artifact_digest(path: Path, label: str) -> str:
    return _sha256_bytes(_artifact_bytes(path, label))


def _active_overlays(root: Path, composition: dict | None = None) -> list[dict]:
    """Effective composed policies at run time; corrupt layer inputs fail exposure capture loud."""
    if composition is None:
        import overlay
        composition = overlay.compose_policy(root)
    return [{
        "identity": policy["identity"], "status": policy["stage"],
        **({"origin_delta_id": policy["origin_delta_id"]}
           if isinstance(policy.get("origin_delta_id"), str) else {}),
    } for policy in composition["effective"]]


def _write_exposure(record_dir, did, root, packet, task_id, head_sha, base_sha, dirty, binding,
                    fingerprint, overlays, runner_override=None, policy_composition=None):
    runner = str(binding.get("backend", "")).partition(":")[0]
    start_level = load_config(root)["policy"]["start_level"]
    exposure = {
        "schema": "waystone-exposure-1", "delegation_id": did, "at": _now_iso(),
        "project": {"pslug": _project_slug(root), "root": str(root.resolve()), "name": packet["project"]["name"]},
        "task_id": task_id, "packet": "packet.yaml",
        "base": {"head_sha": head_sha, "snapshot_sha": base_sha, "dirty": dirty,
                 "dirty_state_policy": "snapshot-commit-v1"},
        "binding": binding,
        "profile_fingerprint": fingerprint,
        "start_level": start_level,
        "sandbox": "workspace-write" if runner == "codex" else "none",
        "overlays": overlays,
        # Adapt & Enforce has not shipped: null/[] are the truthful effective values until that arc
        # supplies enforceable guards and recorded waivers.
        "guards": None, "waivers": [],
    }
    if policy_composition is not None:
        exposure["policy_composition"] = policy_composition
    if runner_override is not None:
        exposure["runner_override"] = runner_override
    path = record_dir / "exposure.json"
    if path.exists():
        raise WorkflowError(f"delegation exposure is immutable and already exists: {path}")
    write_text_atomic(path, json.dumps(exposure, ensure_ascii=False, indent=2) + "\n")
    return exposure


def _add_worktree(root: Path, worktree_path: Path, base_sha: str) -> None:
    _git_out(root, "worktree", "add", "--detach", str(worktree_path), base_sha)


# ---- run (§§3-10 — the delegation vertical slice) -----------------------------
def _prepare_run(root: Path, task_id: str, role: str, accept_flags: list[str],
                 retry_note: str | None = None, routing_note: str | None = None, *,
                 allow_unsandboxed_runner: bool = False,
                 unsandboxed_reason: str | None = None,
                 expect_packet_sha: str | None = None, expect_profile: str | None = None,
                 carrier: str | None = None, carrier_instance: str | None = None,
                 json_events: bool = False) -> dict:
    _ensure_project_state_or_refuse(root)
    _check_snapshot_preconditions(root)
    profile, fingerprint = _load_profile(root)
    if expect_profile is not None and expect_profile != fingerprint:
        raise WorkflowError(
            f"profile fingerprint mismatch: expected {expect_profile}, live profile is "
            f"{fingerprint} — re-plan from the current profile before dispatch")
    if role != "implementer":
        raise WorkflowError(f"role {role} not consumed in M1")
    binding = _resolve_binding(profile, role, root)
    runner_override = _unsandboxed_runner_override(
        binding, allow_unsandboxed_runner, unsandboxed_reason)
    carrier_record = _validate_carrier(carrier, carrier_instance)
    model = _runner_model(binding["backend"])
    cfg = load_config(root)
    if (cfg.get("delegation") or {}).get("enabled") is not True:
        raise WorkflowError(
            "delegation is disabled by .waystone.yml delegation.enabled; re-run init consent "
            "before enabling worktree/runner execution")
    packet, _acceptance = _build_packet(
        load_tasks(root), task_id, accept_flags, root, retry_note=retry_note,
        routing_note=routing_note)
    if runner_override is not None:
        packet["runner_override"] = runner_override
    if carrier_record is not None:
        packet["carrier"] = carrier_record
    if expect_packet_sha is not None:
        actual = _packet_core_digest(packet)
        if actual != expect_packet_sha:
            raise WorkflowError(
                f"packet digest mismatch: expected {expect_packet_sha}, rebuilt {actual} — the task "
                f"changed since planning; re-plan from current state before dispatch")
    bindings = profile.get("bindings")
    verifier_bound = isinstance(bindings, dict) and "verifier" in bindings
    return {"task_id": task_id, "binding": binding, "model": model, "cfg": cfg,
            "packet": packet, "fingerprint": fingerprint, "accept_flags": list(accept_flags),
            "retry_note": retry_note, "routing_note": routing_note,
            "verifier_bound": verifier_bound,
            "runner_override": runner_override, "carrier": carrier_record,
            "expect_packet_sha": expect_packet_sha, "json_events": json_events}


def _claim_run(root: Path, plan: dict) -> tuple[str, Path]:
    task_id = plan["task_id"]
    try:
        current_packet, _acceptance = _build_packet(
            load_tasks(root), task_id, plan["accept_flags"], root,
            retry_note=plan["retry_note"], routing_note=plan["routing_note"])
        if plan["runner_override"] is not None:
            current_packet["runner_override"] = plan["runner_override"]
        if plan.get("carrier") is not None:
            current_packet["carrier"] = plan["carrier"]
    except WorkflowError as e:
        raise WorkflowError(
            f"task {task_id} changed while preparing delegation — retry from current state: {e}") from e
    # Dict equality is order-insensitive, but the packet core digest is now order-sensitive (it
    # hashes the exact prompt projection). Compare both so the prepare->claim drift check catches a
    # prompt-visible reorder of a mapping-typed field, matching the --expect-packet-sha gate.
    if (current_packet != plan["packet"]
            or _packet_core_digest(current_packet) != _packet_core_digest(plan["packet"])):
        raise WorkflowError(
            f"task {task_id} changed while preparing delegation — retry from current state")
    active = _active_delegation_for_task(root, task_id)
    if active:
        raise WorkflowError(
            f"task {task_id} already has active delegation {active[0]} (state {active[1]}) — "
            f"apply or discard it first")
    import overlay
    plan["policy_composition"] = overlay.compose_policy(root)
    plan["overlays"] = _active_overlays(root, plan["policy_composition"])

    did = _make_did(task_id)
    base_did, n = did, 2
    _mkdir_or_refuse(_delegations_dir(root))
    while True:
        record_dir = _record_dir(root, did)
        try:
            record_dir.mkdir(exist_ok=False)
            break
        except FileExistsError:
            did = f"{base_did}-{n}"
            n += 1
        except OSError as e:
            raise _RefusedWrite(f"cannot claim delegation record {record_dir}: {e}") from e
    claim = {"schema": "waystone-delegation-claim-1", "task_id": task_id, "at": _now_iso()}
    try:
        with (record_dir / "claim.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(claim, ensure_ascii=False) + "\n")
    except OSError as e:
        try:
            record_dir.rmdir()
        except OSError:
            pass
        raise _RefusedWrite(f"cannot write delegation claim {record_dir / 'claim.json'}: {e}") from e
    return did, record_dir


def _emit_event(event: dict) -> None:
    """Emit one NDJSON run event on stdout (pure machine stream under --json-events, §4.2)."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _run_claimed(root: Path, plan: dict, did: str, record_dir: Path) -> int:
    task_id = plan["task_id"]
    packet = plan["packet"]
    json_events = plan.get("json_events", False)
    # Under --json-events every human line moves to stderr so stdout stays pure NDJSON (§4.2).
    human = sys.stderr if json_events else sys.stdout
    # `claimed` is emitted immediately (the claim record is already on disk) and BEFORE any
    # snapshot/ref/worktree creation, so the did is recoverable even if a later step fails.
    if json_events:
        _emit_event({"event": "claimed", "delegation_id": did, "task_id": task_id,
                     "packet_sha256": _packet_core_digest(packet)})
    try:
        return _run_claimed_body(root, plan, did, record_dir, human)
    except WorkflowError as e:
        if json_events:
            state = (_read_status_raw(record_dir) or {}).get("state") or "failed-runner"
            _emit_event({"event": "finished", "delegation_id": did, "state": state,
                         "artifact": None, "base_sha": None, "changed_file_count": None,
                         "patch_sha256": None, "patch_empty": None,
                         "delegate_report_present": None, "error": str(e)})
        raise


def _run_claimed_body(root: Path, plan: dict, did: str, record_dir: Path, human) -> int:
    task_id = plan["task_id"]
    binding = plan["binding"]
    model = plan["model"]
    cfg = plan["cfg"]
    packet = plan["packet"]
    fingerprint = plan["fingerprint"]
    overlays = plan["overlays"]
    json_events = plan.get("json_events", False)
    artifact_dir = record_dir / "artifact"
    worktree_path = _worktree_path(root, did)
    _mkdir_or_refuse(artifact_dir)
    _mkdir_or_refuse(worktree_path.parent)

    head_sha = git_full_sha(root, "HEAD")
    base_sha, dirty = _snapshot(root, f"waystone delegation snapshot: {task_id} {did}")
    _git_out(root, "update-ref", f"{DELEG_REF_NS}/{did}", base_sha, "")
    _add_worktree(root, worktree_path, base_sha)
    print(f"base_sha: {base_sha}", file=human)
    print(f"dirty: {str(dirty).lower()}", file=human)
    print(f"worktree: {worktree_path}", file=human)

    (record_dir / "packet.yaml").write_text(
        yaml.safe_dump(packet, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_exposure(record_dir, did, root, packet, task_id, head_sha, base_sha, dirty, binding,
                    fingerprint, overlays, plan["runner_override"], plan["policy_composition"])
    _set_state(record_dir, "running")

    env_kind, env_commands = _resolve_env_prep(worktree_path, cfg)
    env_rc, env_tail = _run_env_prep(worktree_path, env_commands)
    env_rec = {"prep": env_kind, "commands": env_commands, "rc": env_rc}
    print(f"env_prep: {env_kind} rc={env_rc}", file=human)
    if env_rc != 0:
        _set_state(record_dir, "failed-env", env=env_rec, error=env_tail)
        raise WorkflowError(
            f"env prep failed (rc {env_rc}) — worktree preserved at {worktree_path}\n{env_tail}")

    (record_dir / "prompt.txt").write_text(_render_prompt(packet, base_sha), encoding="utf-8")
    runner_name, _model = _runner_parts(binding["backend"])
    run_external = _run_codex if runner_name == "codex" else _run_claude
    if runner_name == "claude":
        print(f"{CLAUDE_CONFINEMENT_WARN}; override reason: "
              f"{plan['runner_override']['reason']}", file=sys.stderr)
    runner_kwargs = {"effort": binding["effort"]} if "effort" in binding else {}
    try:
        runner_rc, duration = run_external(
            worktree_path, model, record_dir / "prompt.txt", record_dir, **runner_kwargs)
    except _RunnerProbeFailure as e:
        probe = e.probe_result
        classification = probe.get("classification") if isinstance(probe, dict) else None
        state = "failed-runner" if classification in ("transport", "internal") else "failed-env"
        _set_state(record_dir, state, env=env_rec, error=str(e), probe=probe)
        raise WorkflowError(
            f"{e} — implementation runner was not started; worktree preserved at "
            f"{worktree_path}") from e
    except Exception as e:  # noqa: BLE001 — every runner transport failure releases running state
        _set_state(record_dir, "failed-runner", env=env_rec,
                   error=f"runner transport exception: {e}")
        raise WorkflowError(
            f"runner transport failed: {e} — worktree preserved at {worktree_path}") from e
    print(f"runner: backend={binding['backend']} rc={runner_rc}", file=human)
    if runner_rc != 0:
        _set_state(record_dir, "failed-runner", env=env_rec, error=f"runner rc {runner_rc}")
        raise WorkflowError(
            f"runner exited non-zero (rc {runner_rc}) — see {record_dir / 'runner.stderr'}; worktree preserved")

    # Any artifact-computation failure must not strand the record as `running` (a permanently held
    # owner lock): transition to failed-artifact — worktree preserved as evidence, lock held like the
    # other failed-* states, discard clears it (H1).
    try:
        report = _read_report(worktree_path)
        result_sha, _ = _snapshot(
            worktree_path, f"waystone delegation result: {task_id} {did}", exclude_uv_cache=True)
        changed = _changed_files(root, base_sha, result_sha)
        patch = _diff_patch(root, base_sha, result_sha)
        empty = patch.strip() == b""
        environment_failure = _runner_environment_failure_reason(record_dir, empty, report)
        if environment_failure is not None:
            raise _RunnerEnvironmentFailure(environment_failure)
        _git_out(root, "update-ref", f"{DELEG_REF_NS}/{did}-result", result_sha)
        if not empty:
            (artifact_dir / "changes.patch").write_bytes(patch)
        probe = _read_sandbox_probe_result(record_dir) if runner_name == "codex" else None
        runner_record = {"backend": binding["backend"], "rc": runner_rc,
                         "duration_s": duration, "last_message": "last_message.md"}
        if probe is not None:
            runner_record["probe"] = probe
        contract = {
            "schema": "waystone-artifact-1",
            "delegation_id": did, "task_id": task_id,
            "base_sha": base_sha, "result_sha": result_sha,
            "changed_files": changed,
            "patch_file": "changes.patch" if not empty else None,
            "patch_sha256": _sha256_bytes(patch) if not empty else None,
            "empty": empty,
            "delegate_report": report,
            "env": env_rec,
            "runner": runner_record,
        }
        (artifact_dir / "contract.yaml").write_text(
            yaml.safe_dump(contract, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except _RunnerEnvironmentFailure as e:
        _set_state(record_dir, "failed-env", env=env_rec, error=str(e))
        raise WorkflowError(
            f"{e} — worktree preserved at {worktree_path}") from e
    except Exception as e:
        _set_state(record_dir, "failed-artifact", env=env_rec, error=str(e))
        raise WorkflowError(
            f"artifact computation failed after the runner — worktree preserved at {worktree_path}: {e}")
    boundary_context = {"delegation_id": did, "task_id": task_id}
    packet_round = (packet.get("task") or {}).get("round")
    if isinstance(packet_round, str):
        boundary_context["round_id"] = packet_round
    events = _warn_boundary(root, "delegate-run", boundary_context)
    rule1_fired = any(
        event.get("rule") == "delegation-verification-evidence-v1"
        and event.get("event") == "fire"
        and isinstance(event.get("context"), dict)
        and event["context"].get("delegation_id") == did
        for event in events
    )
    _set_state(record_dir, "needs-review", env=env_rec,
               verification_required=plan["verifier_bound"] or rule1_fired)
    if json_events:
        _emit_event({
            "event": "finished", "delegation_id": did, "state": "needs-review",
            "artifact": str(artifact_dir / "contract.yaml"), "base_sha": base_sha,
            "changed_file_count": len(changed),
            "patch_sha256": contract["patch_sha256"], "patch_empty": empty,
            "delegate_report_present": report.get("present") is True,
        })
    print(f"artifact: {artifact_dir / 'contract.yaml'}", file=human)
    return 0


def run_delegation(root: Path, task_id: str, role: str, accept_flags: list[str],
                   retry_note: str | None = None, routing_note: str | None = None, *,
                   allow_unsandboxed_runner: bool = False,
                   unsandboxed_reason: str | None = None,
                   expect_packet_sha: str | None = None, expect_profile: str | None = None,
                   carrier: str | None = None, carrier_instance: str | None = None,
                   json_events: bool = False) -> int:
    """Library entry: prepare, claim, then run without acquiring flock; CLI owns lock placement."""
    plan = _prepare_run(
        root, task_id, role, accept_flags, retry_note=retry_note, routing_note=routing_note,
        allow_unsandboxed_runner=allow_unsandboxed_runner,
        unsandboxed_reason=unsandboxed_reason,
        expect_packet_sha=expect_packet_sha, expect_profile=expect_profile,
        carrier=carrier, carrier_instance=carrier_instance, json_events=json_events)
    did, record_dir = _claim_run(root, plan)
    return _run_claimed(root, plan, did, record_dir)


def _warn_boundary(root: Path, boundary: str, context: dict) -> list[dict]:
    """Best-effort overlay warn at a delegation boundary. evaluate_boundary already swallows its own
    exceptions; the extra guard covers an import failure — a warn must never affect the host exit (S5)."""
    try:
        import overlay
        return overlay.evaluate_boundary(root, boundary, context)
    except Exception as e:  # noqa: BLE001
        print(f"waystone delegate: overlay warning unavailable at {boundary} ({e}) — host command continued",
              file=sys.stderr)
        return []


# ---- independent verifier (§11 — same-base read-only transport) --------------
def _verifier_env(record_dir: Path) -> dict[str, str]:
    """Hermetic verifier env passed directly to the verifier subprocess.

    Runtime cache belongs to the durable record, never to the review worktree.
    """
    return {
        **os.environ,
        "UV_CACHE_DIR": str(record_dir / "runtime" / "uv-cache"),
        _VERIFIER_SESSION_ENV: "1",
    }


def _prepare_codex_verifier_transport(record_dir: Path) -> tuple[Path, Path, Path]:
    """Reset every mutable Codex verifier transport file before a retry.

    The record lock serializes verify attempts. A reset failure therefore stops the attempt before
    Codex starts; continuing could make a prior attempt's output look current.
    """
    output = record_dir / "verify-last-message.json"
    jsonl = record_dir / "verify-codex.jsonl"
    stderr = record_dir / "verify.stderr"
    try:
        output.unlink(missing_ok=True)
    except OSError as e:
        raise WorkflowError(
            f"cannot prepare Codex verifier transport file {output}: {e}") from e
    for path in (jsonl, stderr):
        try:
            with open(path, "w", encoding="utf-8"):
                pass
        except OSError as e:
            raise WorkflowError(
                f"cannot prepare Codex verifier transport file {path}: {e}") from e
    return output, jsonl, stderr


def _append_codex_verifier_stderr(path: Path, message: str) -> None:
    """Record a harness-side Codex transport cause without discarding process stderr."""
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"\n{message}\n")
    except OSError as e:
        raise WorkflowError(f"cannot record Codex verifier transport failure in {path}: {e}") from e


def _run_codex_verifier(worktree: Path, model: str, focus: str,
                        record_dir: Path, *, effort: str | None = None) -> tuple[int, str]:
    """Run the native Codex verifier in a read-only, ephemeral session with schema output."""
    output, jsonl, stderr = _prepare_codex_verifier_transport(record_dir)
    fingerprint = _codex_runner_fingerprint(worktree)
    cmd = [fingerprint["resolved_codex_path"], "exec", "-C", str(worktree),
           "-m", model, "-s", "read-only"]
    if effort is not None:
        cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
    cmd.extend([
        "--ephemeral", "--output-schema", str(_VERIFY_SCHEMA_PATH),
        "--output-last-message", str(output), "--color", "never", "--json", "-",
    ])
    env = _verifier_env(record_dir)
    try:
        with open(jsonl, "a", encoding="utf-8") as jout, \
             open(stderr, "a", encoding="utf-8") as jerr:
            proc = subprocess.run(
                cmd, input=focus, stdout=jout, stderr=jerr, text=True,
                timeout=1800, env=env,
            )
    except subprocess.TimeoutExpired:
        _append_codex_verifier_stderr(stderr, "codex verifier timed out after 1800 seconds")
        return 124, ""
    except OSError as e:
        _append_codex_verifier_stderr(stderr, f"codex verifier transport error: {e}")
        return 127, ""
    if proc.returncode != 0:
        return proc.returncode, ""
    try:
        content = output.read_text(encoding="utf-8")
    except FileNotFoundError:
        _append_codex_verifier_stderr(
            stderr, "empty verifier output: last-message file was not created")
        return 65, ""
    except OSError as e:
        _append_codex_verifier_stderr(stderr, f"cannot read Codex verifier output {output}: {e}")
        return 74, ""
    if not content.strip():
        _append_codex_verifier_stderr(
            stderr, "empty verifier output: last-message contains no non-whitespace content")
        return 65, ""
    return 0, content


def _run_claude_verifier(worktree: Path, model: str, focus: str,
                         record_dir: Path, *, effort: str | None = None,
                         runner=None) -> tuple[int, str]:
    """Run Claude with schema output and no executable or write-capable tools."""
    allowed = "Read,Glob,Grep"
    denied = "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch"
    cmd = ["claude", "-p", "--model", model, *_CLAUDE_COMMON_ARGS]
    if effort is not None:
        cmd.extend(["--effort", effort])
    cmd.extend([
        "--tools", allowed, "--allowedTools", allowed,
        "--disallowedTools", denied,
        "--json-schema", _VERIFY_SCHEMA_PATH.read_text(encoding="utf-8"),
        "--output-format", "json",
    ])
    invoke = runner or subprocess.run
    try:
        proc = invoke(
            cmd, cwd=str(worktree), input=focus, capture_output=True, text=True,
            timeout=1800, env=_verifier_env(record_dir),
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as e:
        try:
            (record_dir / "verify.stderr").write_text(str(e), encoding="utf-8")
        except OSError:
            pass
        return 127, ""
    try:
        (record_dir / "verify.stderr").write_text(proc.stderr or "", encoding="utf-8")
    except OSError:
        pass
    if proc.returncode != 0:
        return proc.returncode, ""
    stdout = proc.stdout or ""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        try:
            with open(record_dir / "verify.stderr", "a", encoding="utf-8") as stream:
                stream.write(f"\ninvalid claude verifier JSON envelope: {e}\n")
        except OSError:
            pass
        return 65, ""
    if (not isinstance(envelope, dict) or envelope.get("subtype") != "success"
            or not isinstance(envelope.get("structured_output"), dict)):
        try:
            with open(record_dir / "verify.stderr", "a", encoding="utf-8") as stream:
                stream.write(
                    "\nclaude verifier requires subtype=success and structured_output object\n")
        except OSError:
            pass
        return 65, ""
    return 0, json.dumps(envelope["structured_output"], ensure_ascii=False)


def _verify_worktree_state(worktree: Path) -> dict:
    """Capture tracked, untracked (including ignored), and HEAD state for the read-only postcondition."""
    def raw(*args: str) -> bytes:
        try:
            proc = subprocess.run(
                ["git", "-C", str(worktree), *args], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorkflowError(
                f"verifier read-only postcondition could not run git {args[0]}: {e}") from e
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise WorkflowError(
                f"verifier read-only postcondition could not capture git {args[0]}: "
                f"{detail or f'rc {proc.returncode}'}")
        return proc.stdout

    untracked = raw("ls-files", "-z", "--others", "--exclude-standard").split(b"\0")
    ignored = raw(
        "ls-files", "-z", "--others", "--ignored", "--exclude-standard").split(b"\0")
    state = {
        "status": raw("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        "untracked": tuple(filter(None, untracked)),
        "ignored_untracked": tuple(filter(None, ignored)),
        "head": raw("rev-parse", "HEAD"),
    }
    manifest = []
    for relative_bytes in sorted(set((*state["untracked"], *state["ignored_untracked"]))):
        relative = os.fsdecode(relative_bytes)
        path = worktree / relative
        try:
            info = path.lstat()
            metadata = f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}".encode()
            if stat.S_ISLNK(info.st_mode):
                content = b"symlink\0" + metadata + b"\0" + os.fsencode(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                content = b"file\0" + metadata + b"\0" + digest.digest()
            else:
                content = b"special\0" + metadata
        except OSError as e:
            raise WorkflowError(
                f"verifier read-only postcondition could not fingerprint untracked {relative!r}: "
                f"{e}") from e
        manifest.append((relative, content.hex()))
    state["untracked_manifest"] = manifest
    return state


def _effective_verifier_tool_policy(binding: dict) -> dict:
    execution = binding["execution"]
    if execution == "claude-cli":
        return {"tools": ["Read", "Glob", "Grep"], "bash": False,
                "filesystem_postcondition": "git-status+untracked-content-unchanged"}
    if execution == "codex-exec":
        return {"tools": ["codex-exec"], "sandbox": "read-only", "bash": False,
                "filesystem_postcondition": "git-status+untracked-content-unchanged"}
    raise WorkflowError(f"unknown verifier execution {execution!r}")


def _validate_verify_contract(rec: Path, contract: dict, exposure: dict) -> Path | None:
    """Validate every contract field trusted by worktree normalization before mutating the tree."""
    if type(contract.get("empty")) is not bool:
        raise WorkflowError("delegation contract field empty must be a bool")
    for field in ("base_sha", "result_sha"):
        value = contract.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise WorkflowError(f"delegation contract field {field} must be a 40-hex sha")
    base = exposure.get("base")
    snapshot_sha = base.get("snapshot_sha") if isinstance(base, dict) else None
    if contract["base_sha"] != snapshot_sha:
        raise WorkflowError(
            "delegation contract field base_sha does not match exposure field base.snapshot_sha")
    if "patch_sha256" not in contract:
        raise WorkflowError(
            "delegation is a pre-digest record; apply is forbidden and only discard is allowed")
    if contract["empty"]:
        if contract.get("patch_file") is not None or contract["patch_sha256"] is not None:
            raise WorkflowError(
                "empty delegation contract must have null patch_file and patch_sha256")
        return None
    patch_name = contract.get("patch_file")
    if (not isinstance(patch_name, str) or not patch_name
            or Path(patch_name).name != patch_name):
        raise WorkflowError(
            "delegation contract field patch_file must name an artifact file when empty is false")
    patch = rec / "artifact" / patch_name
    if not patch.is_file():
        raise WorkflowError(
            f"delegation contract field patch_file does not exist when empty is false: {patch}")
    patch_digest = contract.get("patch_sha256")
    if not isinstance(patch_digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", patch_digest) is None:
        raise WorkflowError("delegation contract field patch_sha256 must be a sha256 digest")
    if _artifact_digest(patch, "delegation patch") != patch_digest:
        raise WorkflowError("delegation patch digest does not match contract patch_sha256")
    return patch


def _normalize_verify_worktree(worktree: Path, contract: dict, patch: Path | None) -> None:
    """Restore HEAD=delegation base and working tree=result, regardless of delegate commits (S21)."""
    base_sha = contract["base_sha"]
    for args in (("checkout", "--force", "--detach", base_sha), ("clean", "-fd")):
        rc, out, err = _git(worktree, *args)
        if rc != 0:
            raise WorkflowError(
                f"verify worktree normalization failed at git {args[0]}: {err or out or f'rc {rc}'}")
    if patch is not None:
        rc, out, err = _git(worktree, "apply", str(patch))
        if rc != 0:
            raise WorkflowError(
                f"verify worktree normalization failed at git apply: {err or out or f'rc {rc}'}")
    tmpdir = tempfile.mkdtemp(prefix="waystone-verify-tree-")
    try:
        env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
        _git_out(worktree, "read-tree", "HEAD", env=env)
        _git_out(worktree, "add", "-A", "--", ".", ":(exclude).waystone-uv-cache", env=env)
        actual_tree = _git_out(worktree, "write-tree", env=env)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    expected_tree = _git_out(worktree, "rev-parse", f"{contract['result_sha']}^{{tree}}")
    if actual_tree != expected_tree:
        raise WorkflowError(
            "verify worktree normalization result does not match contract result_sha")


def _render_verifier_prompt(rec: Path, contract: dict) -> str:
    try:
        packet = yaml.safe_load((rec / "packet.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise WorkflowError(f"cannot read delegation packet for verify: {e}")
    if not isinstance(packet, dict):
        raise WorkflowError("cannot read delegation packet for verify: packet is not a mapping")
    acceptance = packet.get("acceptance") or []
    if not isinstance(acceptance, list) or not all(isinstance(item, str) for item in acceptance):
        raise WorkflowError("cannot read delegation packet for verify: acceptance must be strings")
    changed = contract.get("changed_files") or []
    try:
        template = _VERIFY_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise WorkflowError(f"cannot read verifier prompt template {_VERIFY_PROMPT_PATH}: {e}") from e
    replacements = {
        "{{ACCEPTANCE}}": "\n".join(f"- {item}" for item in acceptance) or "- (none)",
        "{{CHANGED_FILES}}": "\n".join(
            f"- {item.get('status', '?')} {item.get('path', '?')}"
            for item in changed if isinstance(item, dict)
        ) or "- (none)",
    }
    for marker, value in replacements.items():
        if marker not in template:
            raise WorkflowError(f"verifier prompt template is missing marker {marker}")
        template = template.replace(marker, value)
    return template


def _artifact_paths(rec: Path, prefix: str) -> list[Path]:
    """Return a canonical contiguous artifact sequence; ambiguous numbering is corruption."""
    numbered: list[tuple[int, Path]] = []
    for path in sorted((rec / "artifact").glob(f"{prefix}-*.json")):
        match = re.fullmatch(rf"{re.escape(prefix)}-([1-9][0-9]*)\.json", path.name)
        if match is None:
            raise WorkflowError(f"non-canonical {prefix} artifact filename: {path}")
        if path.is_symlink() or not path.is_file():
            raise WorkflowError(f"non-regular {prefix} artifact file: {path}")
        numbered.append((int(match.group(1)), path))
    numbered.sort(key=lambda item: item[0])
    actual = [number for number, _path in numbered]
    expected = list(range(1, len(numbered) + 1))
    if actual != expected:
        raise WorkflowError(
            f"{prefix} artifact numbers must be contiguous from 1; found {actual or 'none'}")
    return [path for _number, path in numbered]


def _verify_paths(rec: Path) -> list[Path]:
    return _artifact_paths(rec, "verify")


def _write_verify_artifact(rec: Path, artifact: dict) -> Path:
    """Create the next verify artifact without overwriting a concurrently appearing filename."""
    paths = _verify_paths(rec)
    n = _artifact_number(paths[-1], "verify") + 1 if paths else 1
    content = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
    while True:
        out = rec / "artifact" / f"verify-{n}.json"
        try:
            with out.open("x", encoding="utf-8") as f:
                f.write(content)
            return out
        except FileExistsError:
            paths = _verify_paths(rec)
            n = _artifact_number(paths[-1], "verify") + 1 if paths else 1
        except OSError as e:
            raise _RefusedWrite(f"cannot write verifier artifact {out}: {e}")


def _artifact_number(path: Path, prefix: str) -> int:
    part = path.stem.removeprefix(f"{prefix}-")
    return int(part) if part.isdigit() else -1


def _verdict_paths(rec: Path) -> list[Path]:
    return _artifact_paths(rec, "verdict")


def _load_json_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(f"cannot read {label} {path}: {e}") from e
    if not isinstance(data, dict):
        raise WorkflowError(f"{label} must be a JSON object: {path}")
    return data


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _verdict_schema_shape(path: Path, label: str) -> tuple[set[str], set[str], set[str]]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        required = set(schema["required"])
        properties = set(schema["properties"])
        override_properties = set(schema["properties"]["overrides"]["items"]["properties"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise WorkflowError(f"cannot load {label} {path}: {e}") from e
    return required, properties, override_properties


def _validate_verdict_fields(verdict: dict, *, stored: bool) -> None:
    """Validate the input or enriched artifact shape, including non-blank executable evidence."""
    schema_path = _VERDICT_SCHEMA_PATH if stored else _VERDICT_INPUT_SCHEMA_PATH
    label = "stored verdict schema" if stored else "verdict input schema"
    required, allowed, allowed_override = _verdict_schema_shape(schema_path, label)
    missing = sorted(required - verdict.keys())
    extra = sorted(verdict.keys() - allowed)
    if missing:
        raise WorkflowError(f"verdict is missing required field(s): {', '.join(missing)}")
    if extra:
        raise WorkflowError(f"verdict contains unsupported field(s): {', '.join(extra)}")
    if verdict.get("schema") != "waystone-verdict-1":
        raise WorkflowError("verdict schema must be 'waystone-verdict-1'")
    if verdict.get("decision") not in ("apply", "discard"):
        raise WorkflowError("verdict decision must be apply|discard")
    if verdict.get("decided_by") not in ("main-session", "user"):
        raise WorkflowError("verdict decided_by must be main-session|user")
    criteria = verdict.get("criteria")
    if not isinstance(criteria, list):
        raise WorkflowError("verdict criteria must be a list")
    for index, item in enumerate(criteria):
        if not isinstance(item, dict) or set(item) != {"criterion", "met", "evidence"}:
            raise WorkflowError(
                f"verdict criteria[{index}] must contain exactly criterion, met, evidence")
        if not isinstance(item["criterion"], str) or type(item["met"]) is not bool \
                or not _string_list(item["evidence"]):
            raise WorkflowError(
                f"verdict criteria[{index}] has invalid criterion/met/evidence types")
    checks = verdict.get("agent_checks")
    if not isinstance(checks, list):
        raise WorkflowError("verdict agent_checks must be a list")
    for index, item in enumerate(checks):
        if not isinstance(item, dict) or set(item) != {"cmd", "exit", "summary"}:
            raise WorkflowError(
                f"verdict agent_checks[{index}] must contain exactly cmd, exit, summary")
        if (not isinstance(item["cmd"], str) or type(item["exit"]) is not int
                or not isinstance(item["summary"], str)):
            raise WorkflowError(f"verdict agent_checks[{index}] has invalid field types")
        if not item["cmd"].strip():
            raise WorkflowError(f"verdict agent_checks[{index}].cmd must be non-empty")
        if not item["summary"].strip():
            raise WorkflowError(f"verdict agent_checks[{index}].summary must be non-empty")
    if not _string_list(verdict.get("warnings_seen")):
        raise WorkflowError("verdict warnings_seen must be a list of strings")
    if not isinstance(verdict.get("rationale"), str):
        raise WorkflowError("verdict rationale must be a string")
    if not _string_list(verdict.get("limitations")):
        raise WorkflowError("verdict limitations must be a list of strings")
    overrides = verdict.get("overrides", [])
    if not isinstance(overrides, list):
        raise WorkflowError("verdict overrides must be a list")
    for index, item in enumerate(overrides):
        if not isinstance(item, dict) or not set(item).issubset(allowed_override):
            raise WorkflowError(f"verdict overrides[{index}] has unsupported fields")
        refuted = item.get("refuted_by")
        if refuted is not None and (not isinstance(refuted, list)
                                    or any(type(value) is not int or value < 0 for value in refuted)):
            raise WorkflowError(f"verdict overrides[{index}].refuted_by must be index integers")
        if refuted is not None and not refuted:
            raise WorkflowError(f"verdict overrides[{index}].refuted_by must be non-empty")
        finding_index = item.get("finding_index")
        if finding_index is not None and (type(finding_index) is not int or finding_index < 0):
            raise WorkflowError(f"verdict overrides[{index}].finding_index must be an index")
        if stored:
            if item.get("gate") not in ("blocker", "unmet"):
                raise WorkflowError(f"verdict overrides[{index}].gate must be blocker|unmet")
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise WorkflowError(f"verdict overrides[{index}].reason must be non-empty")
            verify_number = item.get("verify_number")
            if verify_number is not None and (type(verify_number) is not int or verify_number < 1):
                raise WorkflowError(f"verdict overrides[{index}].verify_number must be positive")
            criterion_indices = item.get("criterion_indices")
            if criterion_indices is not None and (
                    not isinstance(criterion_indices, list)
                    or any(type(value) is not int or value < 0 for value in criterion_indices)):
                raise WorkflowError(
                    f"verdict overrides[{index}].criterion_indices must be index integers")
        elif item.get("gate") not in (None, "blocker"):
            raise WorkflowError(f"verdict overrides[{index}].gate must be blocker")
    if stored:
        if not isinstance(verdict.get("judged_at"), str) or not verdict["judged_at"].strip():
            raise WorkflowError("stored verdict judged_at must be non-empty")
        if verdict.get("provenance") != "main-session":
            raise WorkflowError("stored verdict provenance must be main-session")
        verify_number = verdict.get("verify_number")
        if verify_number is not None and (type(verify_number) is not int or verify_number < 1):
            raise WorkflowError("stored verdict verify_number must be null or positive")
        fingerprint = verdict.get("profile_fingerprint")
        if fingerprint is not None and not isinstance(fingerprint, str):
            raise WorkflowError("stored verdict profile_fingerprint must be string|null")
        digests = verdict.get("artifact_digests")
        if not isinstance(digests, dict) or set(digests) != {
                "contract_sha256", "patch_sha256", "verify_sha256"}:
            raise WorkflowError(
                "stored verdict artifact_digests must contain contract, patch, and verify digests")
        for field in ("contract_sha256", "patch_sha256", "verify_sha256"):
            value = digests[field]
            if value is not None and (not isinstance(value, str)
                                      or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None):
                raise WorkflowError(
                    f"stored verdict artifact_digests.{field} must be a sha256 digest or null")


def _validate_verdict_input(verdict: dict) -> None:
    """Validate only the public user-input schema; harness fields cannot be supplied."""
    _validate_verdict_fields(verdict, stored=False)


def load_canonical_verdict(path: Path) -> dict:
    """Load one stored verdict through the same schema/provenance validator used by apply."""
    verdict = _load_json_object(Path(path), "verdict artifact")
    _validate_verdict_fields(verdict, stored=True)
    return verdict


def latest_canonical_verdict(record: Path) -> tuple[Path, dict] | None:
    """Return the latest member of a canonical contiguous verdict sequence."""
    paths = _verdict_paths(Path(record))
    if not paths:
        return None
    return paths[-1], load_canonical_verdict(paths[-1])


def _load_packet(rec: Path) -> dict:
    path = rec / "packet.yaml"
    try:
        packet = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise WorkflowError(f"cannot read delegation packet {path}: {e}") from e
    if not isinstance(packet, dict) or not _string_list(packet.get("acceptance")):
        raise WorkflowError(f"delegation packet has invalid acceptance criteria: {path}")
    return packet


def _validate_verify_artifact(path: Path, artifact: dict) -> None:
    def invalid(detail: str) -> None:
        raise WorkflowError(f"invalid verify artifact schema {path}: {detail}")

    required = {
        "schema", "at", "transport", "backend", "provenance", "payload",
        "profile_fingerprint", "base_sha", "result_sha", "patch_sha256",
        "requested_effort", "effective_effort", "effective_tool_policy",
    }
    if set(artifact) != required:
        invalid("envelope fields do not match waystone-verify-1")
    if artifact.get("schema") != "waystone-verify-1":
        invalid("schema must be waystone-verify-1")
    if artifact.get("provenance") != "independent-verifier":
        invalid("provenance must be independent-verifier")
    for field in ("at", "transport", "backend"):
        if not isinstance(artifact.get(field), str) or not artifact[field].strip():
            invalid(f"{field} must be a non-empty string")
    fingerprint = artifact.get("profile_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        invalid("profile_fingerprint must be a non-empty string")
    for field in ("base_sha", "result_sha"):
        if not isinstance(artifact.get(field), str) or re.fullmatch(
                r"[0-9a-fA-F]{40}", artifact[field]) is None:
            invalid(f"{field} must be a 40-hex sha")
    patch_digest = artifact.get("patch_sha256")
    if patch_digest is not None and (not isinstance(patch_digest, str)
                                     or re.fullmatch(r"sha256:[0-9a-f]{64}", patch_digest) is None):
        invalid("patch_sha256 must be a sha256 digest or null")
    requested_effort = artifact.get("requested_effort")
    effective_effort = artifact.get("effective_effort")
    for field, value in (
            ("requested_effort", requested_effort), ("effective_effort", effective_effort)):
        if value is not None and value not in _EFFORT_VALUES:
            invalid(f"{field} must be a supported effort or null")
    if effective_effort != requested_effort:
        invalid("effective_effort must equal the exactly forwarded requested_effort")
    policy = artifact.get("effective_tool_policy")
    if not isinstance(policy, dict) or not policy:
        invalid("effective_tool_policy must be a non-empty object")
    payload = artifact.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"summary", "findings", "limitations"}:
        invalid("payload fields do not match verifier-output-schema")
    if not isinstance(payload["summary"], str):
        invalid("payload.summary must be a string")
    findings = payload["findings"]
    if not isinstance(findings, list):
        invalid("payload.findings must be a list")
    finding_fields = {"title", "severity", "evidence", "recommendation"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or set(finding) != finding_fields:
            invalid(f"payload.findings[{index}] fields are invalid")
        if finding.get("severity") not in ("blocker", "major", "minor"):
            invalid(f"payload.findings[{index}].severity is invalid")
        for field in ("title", "evidence", "recommendation"):
            if not isinstance(finding.get(field), str):
                invalid(f"payload.findings[{index}].{field} must be a string")
    if not _string_list(payload["limitations"]):
        invalid("payload.limitations must be a list of strings")


def _verify_artifacts(rec: Path) -> dict[int, dict]:
    artifacts: dict[int, dict] = {}
    for path in _verify_paths(rec):
        artifact = _load_json_object(path, "verify artifact")
        _validate_verify_artifact(path, artifact)
        artifacts[_artifact_number(path, "verify")] = artifact
    return artifacts


def _evidence_ref_resolves(reference: str, checks: list[dict],
                           verify_artifacts: dict[int, dict]) -> bool:
    check_match = re.fullmatch(r"agent_checks\[([0-9]+)\]", reference)
    if check_match is not None:
        return int(check_match.group(1)) < len(checks)
    verify_match = re.fullmatch(r"verify-([1-9][0-9]*)(?:\.json)?#(.+)", reference)
    if verify_match is None:
        return False
    artifact = verify_artifacts.get(int(verify_match.group(1)))
    if artifact is None:
        return False
    fragment = verify_match.group(2)
    payload = artifact["payload"]
    if fragment == "summary":
        return bool(payload["summary"].strip())
    finding_match = re.fullmatch(r"finding-([0-9]+)", fragment)
    if finding_match is not None:
        number = int(finding_match.group(1))
        return 1 <= number <= len(payload["findings"])
    limitation_match = re.fullmatch(r"limitation-([0-9]+)", fragment)
    if limitation_match is not None:
        number = int(limitation_match.group(1))
        return 1 <= number <= len(payload["limitations"])
    return False


def _verdict_gate_context(rec: Path, did: str, verdict: dict) -> dict:
    state = _read_status(rec)
    if state.get("state") != "needs-review":
        raise WorkflowError(
            f"delegation {did} is {state.get('state')} — only a needs-review delegation can receive a verdict")
    verification_required = state.get("verification_required")
    if type(verification_required) is not bool:
        raise WorkflowError(
            f"delegation {did} lacks harness-computed verification_required in status.json")
    packet = _load_packet(rec)
    packet_acceptance = packet["acceptance"]
    verdict_acceptance = [item["criterion"] for item in verdict["criteria"]]
    if (len(verdict_acceptance) != len(packet_acceptance)
            or set(verdict_acceptance) != set(packet_acceptance)):
        raise WorkflowError(
            "verdict criteria must exactly match the packet acceptance criterion set (original text)")
    verify_artifacts = _verify_artifacts(rec)
    verify_number = max(verify_artifacts) if verify_artifacts else None
    verify_artifact = verify_artifacts.get(verify_number) if verify_number is not None else None
    contract = _load_contract(rec)
    exposure = _load_exposure(rec)
    _validate_verify_contract(rec, contract, exposure)
    if verify_artifact is not None and any(
            verify_artifact[field] != contract[field]
            for field in ("base_sha", "result_sha", "patch_sha256")):
        raise WorkflowError(
            "latest verify artifact base/result/patch digest does not match the delegation contract")
    if verification_required and verify_artifact is None:
        raise WorkflowError(
            "verdict requires at least one verify-N.json because the run recorded verification_required")
    if not verification_required and not verdict["agent_checks"]:
        raise WorkflowError("verdict requires at least one agent_checks entry when verify is not required")
    blockers = []
    if verify_artifact is not None:
        blockers = [
            (index, finding)
            for index, finding in enumerate(verify_artifact["payload"]["findings"])
            if finding["severity"] == "blocker"
        ]
    return {
        "packet": packet,
        "verification_required": verification_required,
        "verify_artifacts": verify_artifacts,
        "verify_number": verify_number,
        "contract": contract,
        "blockers": blockers,
    }


def _validate_verdict_gates(rec: Path, did: str, verdict: dict) -> dict:
    """Re-run G1-G5 over one enriched verdict; both verdict and apply call this function."""
    _validate_verdict_fields(verdict, stored=True)
    context = _verdict_gate_context(rec, did, verdict)
    if verdict["verify_number"] != context["verify_number"]:
        raise WorkflowError(
            "stored verdict verify_number does not match the latest verify artifact; record a new verdict")
    if verdict["decision"] != "apply":
        if verdict.get("overrides"):
            raise WorkflowError("discard verdict must not contain apply gate overrides")
        return context

    checks = verdict["agent_checks"]
    for index, criterion in enumerate(verdict["criteria"]):
        if criterion["met"] and not any(
                _evidence_ref_resolves(reference, checks, context["verify_artifacts"])
                for reference in criterion["evidence"] if reference.strip()):
            raise WorkflowError(
                f"apply verdict criteria[{index}] met=true requires at least one resolvable evidence reference")

    overrides = verdict.get("overrides", [])
    blocker_overrides = [item for item in overrides if item["gate"] == "blocker"]
    if context["blockers"]:
        by_finding = {item.get("finding_index"): item for item in blocker_overrides}
        if (len(by_finding) != len(blocker_overrides)
                or set(by_finding) != {index for index, _finding in context["blockers"]}):
            raise WorkflowError("stored verdict does not override every latest unresolved blocker exactly once")
        for finding_index, _finding in context["blockers"]:
            item = by_finding[finding_index]
            if item.get("verify_number") != context["verify_number"]:
                raise WorkflowError("blocker override verify_number does not match latest verify artifact")
            refuted_by = item.get("refuted_by")
            if not refuted_by or any(index >= len(checks) for index in refuted_by):
                raise WorkflowError(
                    "blocker override refuted_by must reference non-empty existing agent_checks")
    elif blocker_overrides:
        raise WorkflowError("stored verdict has blocker overrides but latest verify has no blockers")

    unmet = [index for index, item in enumerate(verdict["criteria"]) if not item["met"]]
    unmet_overrides = [item for item in overrides if item["gate"] == "unmet"]
    if unmet:
        if len(unmet_overrides) != 1 or unmet_overrides[0].get("criterion_indices") != unmet:
            raise WorkflowError("stored verdict does not override the exact unmet criterion indices")
    elif unmet_overrides:
        raise WorkflowError("stored verdict has an unmet override but every criterion is met")
    return context


def _write_verdict_artifact(rec: Path, verdict: dict) -> Path:
    paths = _verdict_paths(rec)
    n = _artifact_number(paths[-1], "verdict") + 1 if paths else 1
    content = json.dumps(verdict, ensure_ascii=False, indent=2) + "\n"
    while True:
        out = rec / "artifact" / f"verdict-{n}.json"
        try:
            with out.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return out
        except FileExistsError:
            paths = _verdict_paths(rec)
            n = _artifact_number(paths[-1], "verdict") + 1 if paths else 1
        except OSError as e:
            raise _RefusedWrite(f"cannot write verdict artifact {out}: {e}") from e


def _current_artifact_digests(rec: Path, contract: dict, verify_number: int | None) -> dict:
    patch_digest = None
    if contract.get("empty") is False:
        patch_digest = _artifact_digest(
            rec / "artifact" / str(contract.get("patch_file")), "delegation patch")
    verify_digest = None
    if verify_number is not None:
        verify_digest = _artifact_digest(
            rec / "artifact" / f"verify-{verify_number}.json", "verify artifact")
    return {
        "contract_sha256": _artifact_digest(
            rec / "artifact" / "contract.yaml", "delegation contract"),
        "patch_sha256": patch_digest,
        "verify_sha256": verify_digest,
    }


def _assert_digest_record(rec: Path) -> None:
    contract = _load_contract(rec)
    if "patch_sha256" not in contract:
        raise WorkflowError(
            "delegation is a pre-digest record; apply is forbidden and only discard is allowed")
    verdict_paths = _verdict_paths(rec)
    if verdict_paths:
        raw = _load_json_object(verdict_paths[-1], "verdict artifact")
        if "artifact_digests" not in raw or "judged_at" not in raw:
            raise WorkflowError(
                "delegation is a pre-digest record; apply is forbidden and only discard is allowed")


def _revalidate_apply_digest_chain(rec: Path, verdict: dict) -> tuple[dict, bytes]:
    contract_path = rec / "artifact" / "contract.yaml"
    contract_bytes = _artifact_bytes(contract_path, "delegation contract")
    try:
        contract = yaml.safe_load(contract_bytes)
    except yaml.YAMLError as e:
        raise WorkflowError(f"corrupt contract.yaml in delegation record: {contract_path} ({e})")
    if not isinstance(contract, dict):
        raise WorkflowError(f"corrupt contract.yaml in delegation record: {contract_path}")
    if "patch_sha256" not in contract:
        raise WorkflowError(
            "delegation is a pre-digest record; apply is forbidden and only discard is allowed")
    digests = verdict["artifact_digests"]
    if _sha256_bytes(contract_bytes) != digests["contract_sha256"]:
        raise WorkflowError("delegation contract digest changed after verdict")
    patch_path = _validate_verify_contract(rec, contract, _load_exposure(rec))
    patch_bytes = b"" if patch_path is None else _artifact_bytes(patch_path, "delegation patch")
    current_patch_digest = None if patch_path is None else _sha256_bytes(patch_bytes)
    if current_patch_digest != digests["patch_sha256"]:
        raise WorkflowError("delegation patch digest changed after verdict")

    verify_paths = _verify_paths(rec)
    latest_verify = _artifact_number(verify_paths[-1], "verify") if verify_paths else None
    if latest_verify != verdict["verify_number"]:
        raise WorkflowError(
            "verify artifact set changed after verdict; record a new verdict")
    if latest_verify is None:
        if digests["verify_sha256"] is not None:
            raise WorkflowError("verdict verify digest is inconsistent with no verify artifact")
    else:
        verify_path = rec / "artifact" / f"verify-{latest_verify}.json"
        verify_bytes = _artifact_bytes(verify_path, "verify artifact")
        if _sha256_bytes(verify_bytes) != digests["verify_sha256"]:
            raise WorkflowError("verify artifact digest changed after verdict")
        try:
            verify_artifact = json.loads(verify_bytes)
        except json.JSONDecodeError as e:
            raise WorkflowError(f"cannot read verify artifact {verify_path}: {e}") from e
        if not isinstance(verify_artifact, dict):
            raise WorkflowError(f"verify artifact must be a JSON object: {verify_path}")
        _validate_verify_artifact(verify_path, verify_artifact)
        if any(verify_artifact[field] != contract[field]
               for field in ("base_sha", "result_sha", "patch_sha256")):
            raise WorkflowError(
                "verify artifact base/result/patch digest does not match the delegation contract")
    return contract, patch_bytes


def _git_apply_bytes(root: Path, patch: bytes) -> tuple[int, str, str]:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), "apply", "-"], input=patch,
            capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return (
        process.returncode,
        process.stdout.decode("utf-8", "replace").strip(),
        process.stderr.decode("utf-8", "replace").strip(),
    )


def _blocker_overrides(input_overrides: list[dict], blockers: list[tuple[int, dict]],
                       checks: list[dict], reason: str, verify_number: int) -> list[dict]:
    candidates = [item for item in input_overrides if item.get("gate") in (None, "blocker")]
    unused = list(candidates)
    recorded: list[dict] = []
    for finding_index, _finding in blockers:
        explicit = next((item for item in unused if item.get("finding_index") == finding_index), None)
        item = explicit or next((item for item in unused if "finding_index" not in item), None)
        if item is None:
            raise WorkflowError(
                "--override-blocker requires one overrides[] refuted_by entry per blocker finding")
        unused.remove(item)
        refuted_by = item.get("refuted_by")
        if not refuted_by:
            raise WorkflowError(
                "--override-blocker requires non-empty refuted_by agent_checks indices")
        if any(index >= len(checks) for index in refuted_by):
            raise WorkflowError("override refuted_by references a missing agent_checks index")
        recorded.append({
            "gate": "blocker", "reason": reason, "verify_number": verify_number,
            "finding_index": finding_index, "refuted_by": list(refuted_by),
        })
    if unused:
        raise WorkflowError("verdict overrides[] contains an entry that does not match a blocker finding")
    return recorded


def record_verdict(root: Path, did: str, input_path: Path, *,
                   override_blocker_reason: str | None = None,
                   override_unmet_reason: str | None = None) -> int:
    """Validate and append a verdict artifact. The CLI owns the record lock; state never changes."""
    _ensure_project_state_or_refuse(root)
    rec = _load_delegation(root, did)
    verdict = _load_json_object(Path(input_path), "verdict input")
    _validate_verdict_input(verdict)
    context = _verdict_gate_context(rec, did, verdict)
    _profile, current_fingerprint = _load_profile(root)

    if override_blocker_reason is not None and not override_blocker_reason.strip():
        raise WorkflowError("--override-blocker --reason must be non-empty")
    if override_unmet_reason is not None and not override_unmet_reason.strip():
        raise WorkflowError("--override-unmet --reason must be non-empty")
    input_overrides = verdict.pop("overrides", [])
    if input_overrides and verdict["decision"] != "apply":
        raise WorkflowError("verdict overrides[] is only valid for an apply decision")
    recorded_overrides: list[dict] = []
    blockers = context["blockers"]
    verify_number = context["verify_number"]
    if verdict["decision"] == "apply" and blockers:
        if override_blocker_reason is None:
            raise WorkflowError(
                "latest verify has unresolved blocker finding(s) — use --override-blocker --reason")
        recorded_overrides.extend(_blocker_overrides(
            input_overrides, blockers, verdict["agent_checks"],
            override_blocker_reason, verify_number))
    elif override_blocker_reason is not None:
        raise WorkflowError("--override-blocker is only valid for an apply verdict with blockers")

    unmet = [index for index, item in enumerate(verdict["criteria"]) if not item["met"]]
    if verdict["decision"] == "apply" and unmet:
        if override_unmet_reason is None:
            raise WorkflowError(
                "apply verdict has unmet acceptance criteria — use --override-unmet --reason")
        recorded_overrides.append({
            "gate": "unmet", "reason": override_unmet_reason,
            "criterion_indices": unmet,
        })
    elif override_unmet_reason is not None:
        raise WorkflowError("--override-unmet is only valid for an apply verdict with unmet criteria")
    if input_overrides and not blockers:
        raise WorkflowError("verdict overrides[] is only valid with --override-blocker")

    _load_exposure(rec)  # verdict remains bound to a complete delegation record
    verdict["judged_at"] = _now_iso()
    verdict["provenance"] = "main-session"
    verdict["verify_number"] = verify_number
    verdict["profile_fingerprint"] = current_fingerprint
    verdict["artifact_digests"] = _current_artifact_digests(
        rec, context["contract"], verify_number)
    if recorded_overrides:
        verdict["overrides"] = recorded_overrides
    _validate_verdict_gates(rec, did, verdict)
    out = _write_verdict_artifact(rec, verdict)
    print(f"verdict_artifact: {out}")
    return 0


def _process_returncode_diagnostic(rc: int) -> str:
    """Render subprocess return codes without mislabeling signal termination as an exit code."""
    if rc >= 0:
        return f"rc {rc}"
    number = -rc
    try:
        name = signal.Signals(number).name
    except ValueError:
        return f"signal {number}"
    return f"signal {number} {name}"


def _codex_verifier_failure_diagnostic(record_dir: Path, rc: int) -> str:
    """Combine the process result with the current attempt's bounded stderr evidence."""
    stderr_path = record_dir / "verify.stderr"
    try:
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        stderr = ""
    except OSError as e:
        raise WorkflowError(f"cannot read Codex verifier stderr {stderr_path}: {e}") from e
    tail = "\n".join(stderr.strip().splitlines()[-20:])
    diagnostic = _process_returncode_diagnostic(rc)
    if tail:
        diagnostic += f"; verify.stderr tail: {tail}"
    return diagnostic


def verify_delegation(root: Path, did: str) -> int:
    _ensure_project_state_or_refuse(root)
    rec = _load_delegation(root, did)
    state = _read_status(rec).get("state")
    if state != "needs-review":
        raise WorkflowError(
            f"delegation {did} is {state} — only a needs-review delegation can be verified")
    profile, fingerprint = _load_profile(root)
    binding = _resolve_verifier_binding(profile, root)
    contract = _load_contract(rec)
    exposure = _load_exposure(rec)
    patch = _validate_verify_contract(rec, contract, exposure)
    worktree = _worktree_path(root, did)
    _normalize_verify_worktree(worktree, contract, patch)
    focus = _render_verifier_prompt(rec, contract)
    model = binding["backend"].split(":", 1)[1]
    runner_kwargs = {"effort": binding["effort"]} if "effort" in binding else {}
    before = _verify_worktree_state(worktree)
    transport_error = None
    try:
        if binding["execution"] == "codex-exec":
            rc, stdout = _run_codex_verifier(
                worktree, model, focus, rec, **runner_kwargs)
            transport = "codex-exec:read-only"
        else:
            print(CLAUDE_VERIFIER_DELTA_WARN, file=sys.stderr)
            rc, stdout = _run_claude_verifier(
                worktree, model, focus, rec, **runner_kwargs)
            transport = "claude-print:read-only"
    except Exception as e:  # noqa: BLE001 — postcondition still runs after any transport failure
        transport_error = e
        rc, stdout = 125, ""
    after = _verify_worktree_state(worktree)
    if after != before:
        changed = ", ".join(key for key in before if before[key] != after[key])
        raise WorkflowError(
            f"independent verifier modified the review worktree ({changed}) — verify artifact "
            "was not recorded; delegation remains needs-review")
    if transport_error is not None:
        raise WorkflowError(
            f"independent verifier transport failed ({transport_error}) — delegation remains "
            "needs-review") from transport_error
    if rc != 0:
        diagnostic = (_codex_verifier_failure_diagnostic(rec, rc)
                      if binding["execution"] == "codex-exec"
                      else _process_returncode_diagnostic(rc))
        raise WorkflowError(
            f"independent verifier failed ({diagnostic}) — delegation remains needs-review")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise WorkflowError(
            f"independent verifier returned invalid JSON ({e}) — delegation remains needs-review")
    artifact = {
        "schema": "waystone-verify-1", "at": _now_iso(),
        "transport": transport, "backend": binding["backend"],
        "provenance": "independent-verifier", "payload": payload,
        "requested_effort": binding.get("effort"),
        "effective_effort": binding.get("effort"),
        "profile_fingerprint": fingerprint,
        "base_sha": contract["base_sha"], "result_sha": contract["result_sha"],
        "patch_sha256": contract["patch_sha256"],
        "effective_tool_policy": _effective_verifier_tool_policy(binding),
    }
    _validate_verify_artifact(rec / "artifact" / "verify-pending.json", artifact)
    out = _write_verify_artifact(rec, artifact)
    print(f"verify_artifact: {out}")
    return 0


# ---- evaluation path (§12 — apply/discard/show/status) ------------------------
def _load_delegation(root: Path, did: str) -> Path:
    rec = _record_dir(root, did)
    if not rec.is_dir() or not ((rec / "claim.json").exists() or (rec / "exposure.json").exists()):
        raise WorkflowError(f"unknown delegation {did}")
    return rec


def _ref_exists(root: Path, ref: str) -> bool:
    rc, out, err = _git(root, "show-ref", "--verify", "--quiet", ref)
    if rc == 0:
        return True
    if rc == 1:
        return False
    raise WorkflowError(
        f"delegation cleanup could not inspect ref {ref}: {err or out or f'rc {rc}'}")


def _cleanup(root: Path, did: str, *, preserve_refs: bool = False) -> None:
    """Remove cache worktree and optional refs, then prove that the requested cleanup completed."""
    worktree_path = _worktree_path(root, did)
    if os.path.lexists(worktree_path):
        rc, out, err = _git(root, "worktree", "remove", "--force", str(worktree_path))
        if rc != 0:
            raise WorkflowError(
                f"delegation cleanup failed at git worktree remove: {err or out or f'rc {rc}'}")
    if not preserve_refs:
        for ref in (f"{DELEG_REF_NS}/{did}", f"{DELEG_REF_NS}/{did}-result"):
            rc, out, err = _git(root, "update-ref", "-d", ref)
            if rc != 0:
                raise WorkflowError(
                    f"delegation cleanup failed at git update-ref -d {ref}: "
                    f"{err or out or f'rc {rc}'}")
    rc, out, err = _git(root, "worktree", "prune")
    if rc != 0:
        raise WorkflowError(
            f"delegation cleanup failed at git worktree prune: {err or out or f'rc {rc}'}")
    if os.path.lexists(worktree_path):
        raise WorkflowError(f"delegation cleanup postcondition failed: worktree remains at {worktree_path}")
    rc, listed, err = _git(root, "worktree", "list", "--porcelain")
    if rc != 0:
        raise WorkflowError(
            f"delegation cleanup failed at git worktree list: {err or listed or f'rc {rc}'}")
    target = os.path.realpath(worktree_path)
    registered = [line.removeprefix("worktree ") for line in listed.splitlines()
                  if line.startswith("worktree ")]
    if any(os.path.realpath(path) == target for path in registered):
        raise WorkflowError(
            f"delegation cleanup postcondition failed: worktree remains registered at {worktree_path}")
    if not preserve_refs:
        remaining = [
            ref for ref in (f"{DELEG_REF_NS}/{did}", f"{DELEG_REF_NS}/{did}-result")
            if _ref_exists(root, ref)
        ]
        if remaining:
            raise WorkflowError(
                f"delegation cleanup postcondition failed: refs remain: {', '.join(remaining)}")


def _latest_verdict(rec: Path) -> dict | None:
    latest = latest_canonical_verdict(rec)
    return latest[1] if latest is not None else None


def apply_delegation(root: Path, did: str) -> int:
    """Accept a delegation onto the live tree with plain `git apply` (§12/R2 — not 3-way). An empty
    patch is a no-op success. On drift the apply fails atomically (no partial write) and state stays
    needs-review; the raw git rc never leaks (exit 1)."""
    _ensure_project_state_or_refuse(root)
    rec = _load_delegation(root, did)
    state = _read_status(rec).get("state")
    if state != "needs-review":
        raise WorkflowError(f"delegation {did} is {state} — only a needs-review delegation can be applied")
    _assert_digest_record(rec)
    verdict = _latest_verdict(rec)
    if verdict is None:
        raise WorkflowError("no verdict recorded — run 'waystone delegate verdict' first")
    _validate_verdict_gates(rec, did, verdict)
    if verdict["decision"] != "apply":
        raise WorkflowError(f"latest verdict decision is {verdict['decision']} — refusing apply")
    contract = _load_contract(rec)
    packet = _load_packet(rec)
    boundary_context = {"delegation_id": did, "task_id": contract.get("task_id")}
    packet_round = (packet.get("task") or {}).get("round")
    if isinstance(packet_round, str):
        boundary_context["round_id"] = packet_round
    _warn_boundary(root, "delegate-apply", boundary_context)
    contract, patch_bytes = _revalidate_apply_digest_chain(rec, verdict)
    if not contract.get("empty"):
        rc, out, err = _git_apply_bytes(root, patch_bytes)
        if rc != 0:
            raise WorkflowError(
                f"cannot apply {did}: live tree has drifted from the delegation base — commit/stash "
                f"your changes and retry, or resolve the patch manually\n{err or out}")
    _cleanup(root, did, preserve_refs=True)
    _set_state(rec, "applied")
    print(f"applied {did}" + (" (empty patch — no-op)" if contract.get("empty") else ""))
    return 0


def discard_delegation(root: Path, did: str, reason: str | None = None) -> int:
    """Reject a delegation: state=discarded + worktree/ref cleanup (record dir kept). Accepts any
    non-terminal state, including a crash-remnant `running` (§4/R1) and a corrupt record — the
    cleanup path must not block itself on the corruption it clears (H3)."""
    _ensure_project_state_or_refuse(root)
    rec = _load_delegation(root, did)
    st = _read_status_raw(rec)
    state = st.get("state") if st is not None else None
    if state in TERMINAL_STATES:
        raise WorkflowError(f"delegation {did} is already {state}")
    if state == "discarding" and (reason is None or not reason.strip()):
        transitions = st.get("at_transitions") if isinstance(st, dict) else None
        reason = transitions[-1].get("reason") if transitions else None
    if reason is None or not reason.strip():
        raise WorkflowError("discard requires a non-empty --reason")
    if state != "discarding":
        _set_state(rec, "discarding", reason=reason)
    _cleanup(root, did)
    _set_state(rec, "discarded", reason=reason)
    print(f"discarded {did}")
    return 0


def discard_orphan(root: Path, did: str, reason: str) -> int:
    """Remove refs/cache left after record loss; a present record must use ordinary discard."""
    if not reason.strip():
        raise WorkflowError("discard --orphan requires a non-empty --reason")
    if _record_dir(root, did).exists():
        raise WorkflowError(
            f"delegation record {did} exists — use ordinary discard so its state transition is recorded")
    worktree = _worktree_path(root, did)
    refs = [f"{DELEG_REF_NS}/{did}", f"{DELEG_REF_NS}/{did}-result"]
    if not os.path.lexists(worktree) and not any(_ref_exists(root, ref) for ref in refs):
        raise WorkflowError(f"no orphaned refs or cache worktree found for {did}")
    _cleanup(root, did)
    print(f"discarded orphan {did}: {reason}")
    return 0


def _status_fields(did: str, rec: Path) -> dict:
    """Structured status of one record (§4.3). Corruption is aligned with the owner-lock scan
    (_active_delegation_for_task / _corrupt_delegation_records): corrupt iff status.json is
    unparseable, or the task binding is unreadable — read from exposure.json when it exists, else
    claim.json. A readable claim.json with no exposure/status yet — the claim→exposure window or a
    crash remnant — is a healthy active hold in state "claimed", never corrupt. task_id is recovered
    from claim.json when the exposure is absent/corrupt (display only) — no slug heuristics."""
    st = _read_status_raw(rec)
    claim_only = not (rec / "exposure.json").exists()
    exposure = None
    if not claim_only:
        try:
            exposure = _load_exposure(rec)
        except WorkflowError:
            exposure = None
    if exposure is not None:
        task_id = exposure.get("task_id")
        base_sha = exposure.get("base", {}).get("snapshot_sha")
        binding_ok = task_id is not None
    else:
        try:
            task_id = _load_claim(rec).get("task_id")
        except WorkflowError:
            task_id = None
        base_sha = None
        # Owner-lock reads the binding from claim only when exposure is absent; a present-but-corrupt
        # exposure is an unreadable binding regardless of what claim.json still holds for display.
        binding_ok = claim_only and task_id is not None
    corrupt = st is None or not binding_ok
    state = st.get("state") if st is not None else None
    if state is None and claim_only and not corrupt:
        state = "claimed"  # owner-lock synthesizes this hold state in the claim→exposure window
    at = (st.get("at_transitions") or [{}])[0].get("at") if st is not None else None
    return {"delegation_id": did, "task_id": task_id, "state": state,
            "base_sha": base_sha, "at": at, "corrupt": corrupt}


def _status_row(did: str, rec: Path) -> str:
    """One list line. Lenient: a corrupt record renders as [corrupt] instead of killing the whole
    listing (H3) — single-record verbs (show/apply) are the strict, file-naming paths."""
    f = _status_fields(did, rec)
    if f["corrupt"]:
        return f"{did}  ?  [corrupt]  ?  ?"
    base7 = (f["base_sha"] or "")[:7]
    at = f["at"] or "?"
    return f"{did}  {f['task_id'] or '?'}  [{f['state'] or '?'}]  {base7}  {at}"


def status(root: Path, did: str | None, *, as_json: bool = False) -> int:
    if as_json:
        if did:
            rows = [_status_fields(did, _load_delegation(root, did))]
        else:
            rows = [_status_fields(name, rec) for name, rec in _iter_delegations(root)]
        print(json.dumps(rows, ensure_ascii=False))
        return 0
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
    if opt == "verify":
        paths = _verify_paths(rec)
        if not paths:
            raise WorkflowError(f"delegation {did} has no independent verifier artifact")
        print(paths[-1].read_text(encoding="utf-8").rstrip())
        return 0
    if opt == "failure":
        error = _read_status(rec).get("error")
        print(f"error: {error if error is not None else '(none recorded)'}")
        diagnostic_parts = [str(error)] if error is not None else []
        for filename in ("sandbox-probe.stderr", "runner.stderr"):
            stderr_path = rec / filename
            try:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            except OSError as e:
                raise WorkflowError(f"cannot read runner stderr {stderr_path}: {e}") from e
            diagnostic_parts.append(stderr)
            lines = stderr.splitlines()
            if lines:
                print(f"{filename} tail:")
                print("\n".join(lines[-50:]))
        hint = _runner_sandbox_diagnostic_hint("\n".join(diagnostic_parts))
        if hint is not None:
            print(f"diagnostic hint: {hint}")
        elif state == "failed-env":
            # Even without a recognized sandbox-mechanism line, an environment failure on a
            # machine that skipped the probe deserves the one-time-verification pointer.
            print("diagnostic hint: this machine hit an environment failure; if "
                  ".waystone/codex-runner-verified exists, remove the marker so "
                  "the next run re-probes this machine")
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
    print(f"verify_artifacts: {len(_verify_paths(rec))}")
    return 0


# ---- fan-out plan (§4.1 — orchestration-safe manifest, all gates fail-loud) ---
def _corrupt_delegation_records(root: Path) -> list[tuple[str, str]]:
    """Project-wide fail-closed scan mirroring the owner-lock corruption test (H3): a non-terminal
    record whose status or task binding cannot be read is corrupt (the owner lock would treat it as
    an active hold). Returns [(did, broken_file), ...]."""
    corrupt: list[tuple[str, str]] = []
    for did, sub in _iter_delegations(root):
        st = _read_status_raw(sub)
        state = st.get("state") if st is not None else None
        if state in TERMINAL_STATES:
            continue
        claim_only = not (sub / "exposure.json").exists()
        try:
            tid = (_load_claim(sub) if claim_only else _load_exposure(sub)).get("task_id")
        except WorkflowError:
            tid = None
        if st is None or tid is None:
            broken = "status.json" if st is None else ("claim.json" if claim_only else "exposure.json")
            corrupt.append((did, broken))
    return corrupt


def _plan_carrier_block(profile: dict, root: Path) -> dict:
    """Resolve and validate the orchestrator/clerk/implementer bindings a fan-out consumes, then copy
    them into the manifest carrier block (§4.1 / ADR-0001): orchestrator must bind
    deterministic-workflow, implementer must be external-runner, and all three must declare an
    explicit effort (no model/effort guessing)."""
    bindings = profile.get("bindings")
    if not isinstance(bindings, dict):
        raise WorkflowError(f"profile {_profile_path(root)} bindings must be a mapping")

    def required(role: str) -> dict:
        b = bindings.get(role)
        if not isinstance(b, dict):
            raise WorkflowError(
                f"fan-out plan requires a '{role}' binding in {_profile_path(root)}")
        _validate_profile_binding(role, b)  # shape/backend/execution-for-role
        effort = b.get("effort")
        if not effort:
            raise WorkflowError(
                f"binding '{role}' must declare an explicit effort for a fan-out plan (ADR-0001 §5)")
        return b

    orchestrator = required("orchestrator")
    if _canonical_execution("orchestrator", orchestrator) != "deterministic-workflow":
        raise WorkflowError(
            "fan-out plan requires orchestrator execution 'deterministic-workflow', got "
            f"{orchestrator.get('execution')!r}")
    clerk = required("clerk")
    implementer = required("implementer")
    if _canonical_execution("implementer", implementer) != "external-runner":
        raise WorkflowError(
            "fan-out plan requires implementer execution 'external-runner', got "
            f"{implementer.get('execution')!r}")
    return {
        "orchestrator": {"execution": "deterministic-workflow",
                         "backend": orchestrator["backend"], "effort": orchestrator["effort"]},
        "clerk": {"backend": clerk["backend"], "effort": clerk["effort"]},
        "implementer": {"execution": "external-runner",
                        "backend": implementer["backend"], "effort": implementer["effort"]},
    }


def _scope_overlap_pairs(tasks_out: list[dict]) -> list[list[str]]:
    """Pairwise declared-scope prefix overlap (same semantics as canonical_scope_prefixes + the
    carrier template): two tasks overlap when any prefix of one is a path-prefix of any prefix of the
    other. Undeclared-scope tasks are handled separately (unknown_scope_tasks), never here."""
    def norm(p: str) -> str:
        return p.rstrip("/") + "/"

    def overlap(a: str, b: str) -> bool:
        x, y = norm(a), norm(b)
        return x.startswith(y) or y.startswith(x)

    pairs: list[list[str]] = []
    for i in range(len(tasks_out)):
        for j in range(i + 1, len(tasks_out)):
            a, b = tasks_out[i], tasks_out[j]
            if a["scope"] and b["scope"] and any(
                    overlap(x, y) for x in a["scope"] for y in b["scope"]):
                pairs.append([a["task_id"], b["task_id"]])
    return pairs


def _make_fanout_correlation_id() -> str:
    """Pre-generated correlation id in the _make_did timestamp style (§4.1). The real host workflow
    run id is joined later in the improve/cclog loop — never a pre-unknown value in the manifest.
    The trailing `-fanout-<6 hex>` mirrors the plan schema's `-fanout-<seq>` shape: the hex is a
    uniqueness discriminator so two plans in the same UTC second cannot collide, since this value
    becomes each delegation's immutable carrier.instance_id (the improve/cclog join key)."""
    return (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-fanout-" + os.urandom(3).hex())


def plan(root: Path, task_ids: list[str], routing_note: str | None) -> int:
    cfg = load_config(root)
    if (cfg.get("delegation") or {}).get("enabled") is not True:
        raise WorkflowError(
            "delegation is disabled by .waystone.yml delegation.enabled; re-run init consent "
            "before planning a fan-out")
    if not task_ids:
        raise WorkflowError("plan requires at least one <task-id>")
    seen: set[str] = set()
    targets: list[str] = []
    for tid in task_ids:
        if tid not in seen:
            seen.add(tid)
            targets.append(tid)
    profile, fingerprint = _load_profile(root)
    carrier = _plan_carrier_block(profile, root)
    corrupt = _corrupt_delegation_records(root)
    if corrupt:
        raise WorkflowError(
            "refusing fan-out plan — corrupt delegation record(s): "
            + "; ".join(f"{did} ({broken})" for did, broken in corrupt)
            + " — the owner lock treats these as active; discard them before planning")
    data = load_tasks(root)
    tasks_out: list[dict] = []
    for tid in targets:
        # _build_packet enforces status ∈ {pending, active}, the dependency gate, and acceptance.
        packet, _acceptance = _build_packet(data, tid, [], root)
        active = _active_delegation_for_task(root, tid)
        if active:
            raise WorkflowError(
                f"task {tid} already has a non-terminal delegation {active[0]} (state {active[1]}) — "
                f"apply or discard it before planning a fan-out")
        tasks_out.append({
            "task_id": tid,
            "packet_sha256": _packet_core_digest(packet),
            "scope": packet["declared_scope"],
            "deps": packet["task"]["deps"],
            "deps_ok": True,
        })
    manifest = {
        "schema": FANOUT_PLAN_SCHEMA,
        "root": str(root.resolve()),
        "correlation_id": _make_fanout_correlation_id(),
        "profile_fingerprint": fingerprint,
        "carrier": carrier,
        "tasks": tasks_out,
        "overlap_pairs": _scope_overlap_pairs(tasks_out),
        "unknown_scope_tasks": [t["task_id"] for t in tasks_out if not t["scope"]],
        "routing_note": routing_note if routing_note is not None else "",
    }
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
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
    with hold_project_lock(root):
        migrate_project_state(root)
    return root


def _cli_plan(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "routing-note"), boolean=("json",))
    if not opts.get("json"):
        raise WorkflowError("plan requires --json (the fan-out manifest is machine output)")
    if not pos:
        raise WorkflowError("plan requires at least one <task-id>")
    return plan(_resolve_root(opts.get("root")), pos, opts.get("routing-note"))


def _cli_run(rest: list[str]) -> int:
    pos, opts = _parse_opts(
        rest, value=("role", "root", "note", "routing-note", "reason",
                     "expect-packet-sha", "expect-profile", "carrier", "carrier-instance"),
        repeat=("accept",),
        boolean=("allow-unsandboxed-runner", "json-events"))
    if not pos:
        raise WorkflowError("run requires a <task-id>")
    allow_unsandboxed = bool(opts.get("allow-unsandboxed-runner"))
    if allow_unsandboxed and not opts.get("reason"):
        raise WorkflowError("--allow-unsandboxed-runner requires --reason")
    if opts.get("reason") and not allow_unsandboxed:
        raise WorkflowError("run --reason is only valid with --allow-unsandboxed-runner")
    root = _resolve_root(opts.get("root"))
    plan_ = _prepare_run(
        root, pos[0], opts.get("role", "implementer"), opts.get("accept", []),
        retry_note=opts.get("note"), routing_note=opts.get("routing-note"),
        allow_unsandboxed_runner=allow_unsandboxed,
        unsandboxed_reason=opts.get("reason"),
        expect_packet_sha=opts.get("expect-packet-sha"),
        expect_profile=opts.get("expect-profile"),
        carrier=opts.get("carrier"), carrier_instance=opts.get("carrier-instance"),
        json_events=bool(opts.get("json-events")))
    # Constant-level lock span: only local task/overlay revalidation, owner scan, and one claim write.
    # Git preflight, profile/packet preparation, snapshot/worktree setup, and the runner stay outside.
    with hold_project_lock(root):
        did, record_dir = _claim_run(root, plan_)
    with hold_lock(record_dir / "record.lock"):
        return _run_claimed(root, plan_, did, record_dir)


def _cli_status(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",), boolean=("json",))
    return status(_resolve_root(opts.get("root")), pos[0] if pos else None,
                  as_json=bool(opts.get("json")))


def _cli_show(rest: list[str]) -> int:
    pos, opts = _parse_opts(
        rest, value=("root",), boolean=("patch", "report", "exposure", "verify", "failure"))
    if not pos:
        raise WorkflowError("show requires a <delegation-id>")
    chosen = [o for o in ("patch", "report", "exposure", "verify", "failure") if opts.get(o)]
    if len(chosen) > 1:
        raise WorkflowError("show takes at most one of --patch/--report/--exposure/--verify/--failure")
    return show(_resolve_root(opts.get("root")), pos[0], chosen[0] if chosen else None)


def _cli_apply(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("apply requires a <delegation-id>")
    root = _resolve_root(opts.get("root"))
    # Required nested order: registry -> project -> record. Apply mutates the live tree, so it must
    # serialize with round close/task mutations for the whole record check -> git apply -> state span.
    with hold_project_lock(root):
        rec = _load_delegation(root, pos[0])
        with hold_lock(rec / "record.lock"):
            return apply_delegation(root, pos[0])


def _cli_discard(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "reason"), boolean=("orphan",))
    if not pos:
        raise WorkflowError("discard requires a <delegation-id>")
    root = _resolve_root(opts.get("root"))
    if opts.get("orphan"):
        if not opts.get("reason"):
            raise WorkflowError("discard requires --reason")
        with hold_project_lock(root):
            return discard_orphan(root, pos[0], opts["reason"])
    rec = _load_delegation(root, pos[0])
    with hold_lock(rec / "record.lock"):
        return discard_delegation(root, pos[0], opts.get("reason"))


def _cli_verify(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("verify requires a <delegation-id>")
    root = _resolve_root(opts.get("root"))
    rec = _load_delegation(root, pos[0])
    with hold_lock(rec / "record.lock"):
            return verify_delegation(root, pos[0])


def _cli_verdict(rest: list[str]) -> int:
    pos, opts = _parse_opts(
        rest, value=("root", "file", "reason"),
        boolean=("override-blocker", "override-unmet"))
    if not pos:
        raise WorkflowError("verdict requires a <delegation-id>")
    if not opts.get("file"):
        raise WorkflowError("verdict requires --file <verdict.json>")
    overrides = [name for name in ("override-blocker", "override-unmet") if opts.get(name)]
    if overrides and not opts.get("reason"):
        raise WorkflowError("verdict override flags require --reason")
    if opts.get("reason") and not overrides:
        raise WorkflowError("verdict --reason is only valid with an override flag")
    root = _resolve_root(opts.get("root"))
    rec = _load_delegation(root, pos[0])
    with hold_lock(rec / "record.lock"):
        return record_verdict(
            root, pos[0], Path(opts["file"]),
            override_blocker_reason=(opts.get("reason") if opts.get("override-blocker") else None),
            override_unmet_reason=(opts.get("reason") if opts.get("override-unmet") else None),
        )


_HANDLERS = {"plan": _cli_plan, "run": _cli_run, "status": _cli_status, "show": _cli_show,
             "apply": _cli_apply, "discard": _cli_discard, "verify": _cli_verify,
             "verdict": _cli_verdict}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in _HANDLERS:
        print("waystone delegate: expected subcommand "
              "(plan|run|status|show|apply|discard|verify|verdict)", file=sys.stderr)
        return 1
    try:
        return _HANDLERS[argv[0]](argv[1:])
    except _RefusedWrite as e:
        print(f"waystone delegate: {e}", file=sys.stderr)
        return 2
    except WorkflowError as e:
        print(f"waystone delegate: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
