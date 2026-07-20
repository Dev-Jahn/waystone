#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Integration tests for the waystone v0.2.0 correctness kernel.

Run: uv run scripts/tests/run_tests.py
Covers the deterministic core: merge-gate computation, review-cycle marker emit/parse/classify,
SHA-bound approval logic, tasks gate counts, remote push verification (real temp git repos),
and config review-mode validation. No network / no gh required.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import cclog  # noqa: E402
import codexlog  # noqa: E402
import common  # noqa: E402
import dashboard  # noqa: E402
import delegate  # noqa: E402
import improve  # noqa: E402
import lanes  # noqa: E402
import overlay  # noqa: E402
import merge  # noqa: E402
import remote  # noqa: E402
import resume  # noqa: E402
import review  # noqa: E402
import roadmap  # noqa: E402
import round  # noqa: E402
import tasks  # noqa: E402
import validate  # noqa: E402
import yaml  # noqa: E402

TEST_CURRENT_DATE = date(2026, 7, 19)
TEST_CURRENT_ROUND_DATE = TEST_CURRENT_DATE.isoformat()
TEST_CLOSE_ROUND_ID = f"{TEST_CURRENT_ROUND_DATE}-close"
TEST_NARRATIVE_DIGEST = "sha256:" + "1" * 64
TEST_RENDERED_REQUEST_DIGEST = "sha256:" + "2" * 64
round._current_date = lambda: TEST_CURRENT_DATE


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def init_repo(root: Path):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("0")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "c0")


def _synthetic_codex_fingerprint(worktree: Path) -> dict:
    """Hermetic fingerprint for tests that exercise transport behavior, not discovery."""
    return {
        "schema": "waystone-codex-runner-proof-3",
        "resolved_codex_path": "/opt/waystone-test/bin/codex",
        "codex_version": {"stdout": "codex-cli 9.9.9", "stderr": "test build"},
        "codex_executable": {"size": 1234, "mtime_ns": 5678},
        "hostname": "test-machine",
        "host_identity": {
            "source": "/etc/machine-id", "value": "0123456789abcdef0123456789abcdef",
        },
        "platform": {"system": "TestOS", "machine": "test-arch"},
        "kernel": {"release": "1.2.3", "version": "test-kernel"},
        "sandbox_invocation_contract": "codex-exec:workspace-write:v1",
        "host_sandbox_observation": {
            "source": "none", "status": "not-observed", "platform": "TestOS",
        },
        "execution_principal": {
            "effective_uid": 1000, "effective_gid": 1000,
            "supplementary_groups": [20, 1000],
        },
        "codex_config_root": {
            "source": "default", "configured_path": "~/.codex",
            "resolved_path": "/home/waystone-test/.codex", "status": "not-present",
            "config_toml": {
                "path": "/home/waystone-test/.codex/config.toml",
                "status": "not-present",
            },
        },
        "process_context": {
            "Seccomp": {"source": "/proc/self/status", "status": "observed", "value": "2"},
            "NoNewPrivs": {
                "source": "/proc/self/status", "status": "observed", "value": "1",
            },
            "CapEff": {
                "source": "/proc/self/status", "status": "observed",
                "value": "0000000000000000",
            },
            "security_label": {
                "source": "/proc/self/attr/current", "status": "observed",
                "value": "waystone-test (enforce)",
            },
        },
        "worktree_cache_mount": delegate._worktree_mount_identity(worktree),
    }


def write_legacy_round_request_binding(
        root: Path, round_id: str, target_sha: str, base_sha: str | None,
        reviewers: list[str], *, mode: str = "packet") -> Path:
    directory = root / "docs/reviews"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{round_id}-request.binding.json"
    path.write_text(_json.dumps({
        "schema": review.ROUND_REQUEST_BINDING_V1_SCHEMA,
        "round_id": round_id,
        "target_sha": target_sha,
        "base_sha": base_sha,
        "reviewers": reviewers,
        "mode": mode,
        "canonical_store": "local-packet" if mode == "packet" else "github-pr-comment",
        "at": "2026-01-01T00:00:00+00:00",
    }) + "\n")
    return path


def write_legacy_pr_freeze_binding(
        root: Path, round_id: str, pr: int, cycle: int, target_sha: str,
        base_sha: str, reviewers: list[str], *, suffix: str = "") -> Path:
    directory = root / "docs/reviews"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{round_id}-freeze-{cycle}.binding{suffix}.json"
    path.write_text(_json.dumps({
        "schema": review.PR_FREEZE_BINDING_V1_SCHEMA,
        "round_id": round_id,
        "pr": pr,
        "cycle": cycle,
        "target_sha": target_sha,
        "base_sha": base_sha,
        "reviewers": reviewers,
        "profile_fingerprint": None,
        "mode": "pr",
        "canonical_store": "local-freeze-evidence",
        "at": "2026-07-18T00:00:00+00:00",
    }) + "\n")
    return path


def set_binding_timestamp(path: Path, at: str) -> None:
    row = _json.loads(path.read_text())
    row["at"] = at
    path.write_text(_json.dumps(row) + "\n")








_PR_NARRATIVE = "\n".join([
    "## What changed and why", "The round made packet rendering deterministic.",
    "## Read these first", "scripts/review.py",
    "## Claims to attack", "Freeze republishes only rendered bytes.",
    "## Evidence already produced (mine — inspect, don't trust)", "Full suite green.",
    "## Known weak spots", "None recorded.",
    "## Domain lens", "Fail-loud protocol boundaries.", ""])


def _pr_prepared_round(base: Path, reviewers: str, *, pre_close=None) -> tuple[Path, str, str]:
    """Closed + prepared pr-mode fixture for round-bound freeze paths: (root, head, round_id)."""
    import contextlib
    import io

    root = base / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text(
        "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
        f"review:\n  mode: pr\n  reviewers: [{reviewers}]\n"
        "state:\n  last_round_commit: null\n")
    (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    if pre_close is not None:
        pre_close(root)
    rid = f"{TEST_CURRENT_ROUND_DATE}-prfix"
    with contextlib.redirect_stdout(io.StringIO()):
        assert round.close(root, rid, done=[], touched=[], commit="HEAD") == 0
        narrative = base / "narrative.md"
        narrative.write_text(_PR_NARRATIVE)
        assert review.prepare_review_request(root, rid, narrative) == 0
    return root, head, rid




PASS = dict(cycle_fresh=True, require_ci=True, ci="passing", want_codex=True, codex_fresh=True,
            findings_resolved=True, want_pro=True, pro_result_at_head=True, open_blockers=[],
            open_decisions=[], approved_at_head=True, remote_contains_head=None)





















TASKS_FIXTURE = """# registry — comments must be preserved
version: 1
project: x
tasks:
  - id: feat/alpha
    title: "first task"
    status: active
    deps: []
  - id: gate/beta
    title: "a gate blocked on alpha"
    status: blocked
    deps: [feat/alpha]
"""
























def _registry(n_done, n_active=2):
    rows = []
    for i in range(n_done):
        rows.append(f'  - id: fix/done-{i:03d}\n    title: "done task number {i}"\n'
                    f"    status: done\n    round: 2026-01-01-r\n")
    for i in range(n_active):
        rows.append(f'  - id: feat/active-{i:03d}\n    title: "active task number {i}"\n    status: active\n')
    return "version: 1\nproject: x\ntasks:\n" + "".join(rows)












# ============================================================ v0.7.0 M1: cclog / improve
import json as _json  # noqa: E402

_UUID = "0123abcd-1234-1234-1234-0123456789ab"


def _write_jsonl(path: Path, records, trailing_newline: bool = True) -> None:
    """Write records (dicts or raw strings) as JSONL. The final line omits its newline when
    trailing_newline=False (simulating a truncated active-session tail)."""
    parts = [r if isinstance(r, str) else _json.dumps(r) for r in records]
    text = "\n".join(parts)
    if trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _parse(path: Path, **kw):
    defaults = dict(file_id="f1", server=None, project="proj", session_id="sess",
                    agent_id=None, workflow_id=None, is_sidechain_file=False)
    defaults.update(kw)
    return cclog.parse_transcript_file(path, **defaults)


def _run_with_home(home: Path, fn, *, isolate_storage: bool = True):
    import os
    names = ("HOME", "CODEX_HOME", "WAYSTONE_HOME")
    before = {name: os.environ.get(name) for name in names}
    os.environ["HOME"] = str(home)
    if isolate_storage:
        os.environ["CODEX_HOME"] = str(home / ".codex")
        os.environ["WAYSTONE_HOME"] = str(home / ".waystone")
    try:
        return fn()
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value












# historical feedback file exactly as review.ingest wrote it: metadata header, byte-exact reviewer body
# (which itself contains `### JW-GPT-NNN` blocks + `- Severity:` lines we must NOT parse), then an
# APPENDED triage table under `## Findings (triage skeleton …)` — the only thing improve reviews reads.
_TRIAGE_FEEDBACK = """<!-- waystone feedback: verbatim body below; triage skeleton appended. -->
round: 2026-07-01-alpha
reviewer: gpt-5.5-pro
ingested: 2026-07-01
source: /tmp/review.md

---

### JW-GPT-001 — some finding
- Severity: blocker

### JW-GPT-002 — another finding
- Severity: minor


---

## Findings (triage skeleton — verify each before registering)

| finding | severity | verdict (REAL/REJECTED/NEEDS-RULING) | evidence | task id |
|---|---|---|---|---|
| JW-GPT-001 — some finding | blocker | REAL | confirmed in code | fix/thing |
| JW-GPT-002 — another finding | minor | REJECTED | wrong, see SSOT | |
| JW-GPT-003 — unscored finding | ? |  |  |  |
"""


















_PROFILE_BODY = ('schema: waystone-profile-1\nbindings:\n'
                 '  implementer: {execution: external-runner, backend: "codex:gpt-5.4-codex"}\n')


def _write_profile(root: Path, body: str = _PROFILE_BODY):
    config = root / ".waystone.yml"
    if not config.exists():
        config.write_text("version: 1\nproject: x\n")
    (common.ensure_project_state_dir(root) / "profile.yml").write_text(body, encoding="utf-8")




def _packet_registry():
    return {"project": "demo", "tasks": [
        {"id": "feat/xyz", "title": "implement the xyz feature", "status": "active",
         "milestone": None, "deps": ["feat/dep"], "anchor": "SSOT §2", "notes": "do the thing",
         "accept": ["registry criterion one"]},
        {"id": "feat/dep", "title": "a dependency task", "status": "done"},
        {"id": "feat/blk", "title": "a blocked task here", "status": "blocked"},
        {"id": "feat/dn", "title": "an already done task", "status": "done"},
    ]}






def _deleg_project(d) -> tuple[Path, Path]:
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
        '    accept:\n      - "criterion alpha here"\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    _write_profile(root)
    return root, home


def _deleg_fake(changes, report=None, rc=0):
    def fake(worktree, model, prompt_path, record_dir, **_kwargs):
        for name, content in changes.items():
            (worktree / name).write_text(content)
        (record_dir / "last_message.md").write_text("s", encoding="utf-8")
        if report is not None:
            (worktree / "WAYSTONE_REPORT.yaml").write_text(report, encoding="utf-8")
        return (rc, 0.1)
    return fake


def _deleg_run(root, home, fake, task="feat/xyz", accept=None):
    import contextlib
    import io
    orig = delegate._run_codex
    delegate._run_codex = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return _run_with_home(
                home, lambda: delegate.run_delegation(root, task, "implementer", accept or []))
    finally:
        delegate._run_codex = orig


def _latest_rec(root, home):
    return _run_with_home(home, lambda: sorted(delegate._delegations_dir(root).iterdir())[-1])




def _write_apply_verdict(rec):
    """Install a valid verdict fixture for tests whose subject is the apply mechanics, not judging."""
    packet = yaml.safe_load((rec / "packet.yaml").read_text())
    exposure = _json.loads((rec / "exposure.json").read_text())
    verify_paths = delegate._verify_paths(rec)
    verify_number = (delegate._artifact_number(verify_paths[-1], "verify")
                     if verify_paths else None)
    contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
    verdict = {
        "schema": "waystone-verdict-1",
        "decision": "apply",
        "decided_by": "main-session",
        "criteria": [{"criterion": criterion, "met": True, "evidence": ["agent_checks[0]"]}
                     for criterion in packet["acceptance"]],
        "agent_checks": [{"cmd": "fixture", "exit": 0, "summary": "fixture"}],
        "warnings_seen": [],
        "rationale": "apply mechanics fixture",
        "limitations": [],
        "judged_at": "2026-07-15T00:00:00+00:00",
        "provenance": "main-session",
        "verify_number": verify_number,
        "profile_fingerprint": exposure["profile_fingerprint"],
        "artifact_digests": delegate._current_artifact_digests(
            rec, contract, verify_number),
    }
    (rec / "artifact" / "verdict-1.json").write_text(
        _json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")










_FANOUT_PROFILE = (
    "schema: waystone-profile-1\nbindings:\n"
    "  orchestrator: {execution: deterministic-workflow, backend: 'claude:fable-5', effort: high}\n"
    "  clerk: {execution: clean-subagent, backend: 'claude:haiku-4.5', effort: low}\n"
    "  implementer: {execution: external-runner, backend: 'codex:gpt-5.6-sol', effort: ultra}\n")

_FANOUT_TASKS = (
    "version: 1\nproject: demo\ntasks:\n"
    '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
    '    milestone: "milestone one"\n    round: "round one"\n'
    '    anchor: "src/a.py:10"\n    notes: "original dispatch note"\n'
    '    scope: [src/a]\n    accept:\n      - "criterion alpha here"\n'
    '  - id: feat/two\n    title: "the second task here"\n    status: active\n'
    '    scope: [src/b]\n    accept:\n      - "criterion beta here"\n')


def _fanout_project(d, tasks_yaml=_FANOUT_TASKS, profile=_FANOUT_PROFILE):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
    (root / "tasks.yaml").write_text(tasks_yaml)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    _write_profile(root, profile)
    return root, home


def _run_plan(root, home, args):
    import contextlib
    import io
    import json as _json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run_with_home(home, lambda: delegate.main(["plan", *args, "--root", str(root)]))
    return rc, buf.getvalue(), (_json.loads(buf.getvalue()) if rc == 0 else None)


















# ============================================================ v0.8.0 M2: overlay (C1 store+rules)
def _overlay_project(d):
    root = Path(d) / "proj"
    root.mkdir()
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
    home = Path(d) / "home"
    home.mkdir()
    return root, home


def _add_delta(root, home, delta_id="verification_debt/skip", rule="delegation-verification-evidence-v1",
               **kw):
    kw.setdefault("summary", "observed 3/5 delegations without verification")
    return _run_with_home(home, lambda: overlay.add_delta(root, delta_id, rule=rule, **kw))




def _rule2_project(d):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        "  - id: fix/finding-a\n    title: open severe finding task\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n"
        "  - id: fix/finding-b\n    title: closed finding task\n    status: done\n"
        "    severity: major\n    origin: review-2026-01-01-r1\n"
        "  - id: fix/finding-c\n    title: rejected but open finding\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n"
        "  - id: fix/finding-d\n    title: open minor finding\n    status: active\n"
        "    severity: minor\n    origin: review-2026-01-01-r1\n")
    rdir = root / "docs" / "reviews"
    rdir.mkdir(parents=True)
    (rdir / "2026-01-01-r1-feedback.md").write_text(
        "meta\n\n## Findings (triage skeleton v1)\n"
        "| Finding | Severity | Verdict | Evidence | Task |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| JW-GPT-001 — a | `blocker` | REAL | ev | `fix/finding-a` |\n"
        "| JW-GPT-002 — b | `major` | REAL | ev | `fix/finding-b` |\n"
        "| JW-GPT-003 — c | `blocker` | REJECTED | ev | `fix/finding-c` |\n"
        "| JW-GPT-004 — u | `major` | NEEDS-RULING | ev | |\n")
    home = Path(d) / "home"
    home.mkdir()
    return root, home




# ==================================================== v0.8.0 M2: boundary warn engine + exposure (C2)
def _force_status(root, home, delta_id, status):
    p = _run_with_home(home, lambda: overlay._delta_path(root, delta_id))
    delta = _json.loads(p.read_text())
    delta["status"] = status
    p.write_text(_json.dumps(delta))


def _read_warnings(root, home):
    wp = _run_with_home(home, lambda: overlay._warnings_path(root))
    if not wp.exists():
        return []
    return [_json.loads(ln) for ln in wp.read_text().splitlines() if ln.strip()]


_M2_TRIAGE_FEEDBACK = (
    "meta\n\n## Findings (triage skeleton v1)\n"
    "| Finding | Severity | Verdict | Evidence | Task |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| WS-GPT-001 — a | `blocker` | REAL | ev | `fix/finding-a` |\n")


def _check_project(d):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        "  - id: feat/xyz\n    title: task one here\n    status: active\n    accept:\n      - c1\n"
        "  - id: feat/two\n    title: task two here\n    status: active\n    accept:\n      - c2\n"
        "  - id: feat/three\n    title: task three here\n    status: active\n    accept:\n      - c3\n"
        "  - id: fix/finding-a\n    title: open severe finding\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n")
    rdir = root / "docs" / "reviews"
    rdir.mkdir(parents=True)
    (rdir / "2026-01-01-r1-feedback.md").write_text(_M2_TRIAGE_FEEDBACK)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    _write_profile(root)
    return root, home


def _round_review_project(d):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text(
        "version: 1\nproject: demo\nreviews_dir: docs/reviews\nstate:\n  last_round_commit: null\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        "  - id: chore/close-me\n    title: a task to close now\n    status: active\n    deps: []\n"
        "  - id: fix/finding-a\n    title: open severe finding\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n")
    rdir = root / "docs" / "reviews"
    rdir.mkdir(parents=True)
    (rdir / "2026-01-01-r1-feedback.md").write_text(_M2_TRIAGE_FEEDBACK)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    home.mkdir()
    return root, home




























# ============================================================ v0.8.3: Codex host compatibility






















__all__ = [name for name in globals() if not name.startswith("__")]
