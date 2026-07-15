#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""`waystone improve` — mine Claude Code evidence into deterministic, local-only projection tables.

  waystone improve trace   [--source DIR]... [--project SLUG]... [--out DIR] [--user-wide]
  waystone improve reviews [--out DIR] [--user-wide]
  waystone improve audit   [--in DIR] [--user-wide]
  waystone improve metrics [--in DIR] [--user-wide]
  waystone improve decide  <rec-id> accept|reject [--title T] [--note N] [--out DIR] [--user-wide]

`trace` walks each source (default `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`),
streams every transcript file line-by-line through cclog, and emits three regenerable artifacts
into --out (default `<project>/.waystone/improve/`; `--user-wide` uses `~/.waystone/improve/`):
  sessions.jsonl        one row per transcript (main/subagent/workflow-subagent)
  delegations.jsonl     one row per agent_spawn tool_use
  parse_coverage.json   files-by-kind, event-type counts, unknown/skip/error tallies

`reviews` reads the current project by default (or `~/.waystone/projects.json` with `--user-wide`),
resolves each `reviews_dir` via `.waystone.yml`, and projects the review evidence already on disk (it never
re-implements review ingest) into --out:
  reviews.jsonl         one row per review round (findings from the feedback triage table + the
                        finding-derived tasks joined by their `origin: review-<round-id>`)
  reviews_coverage.json projects scanned / skipped (inaccessible roots are reported, not fatal)

`audit` reads ONLY the four projection artifacts above (never raw logs) from --in (default = trace's
--out) and emits deterministic per-lens facts (no model interpretation — that is the skill's job):
  facts.json            versioned lenses, each carrying rule id + provenance + <=5 evidence pointers;
                        missing inputs are reported in `skipped_lenses`.

`metrics` aggregates the audit projections into the four design §15 metric groups, appends a
timestamped longitudinal snapshot, and adds at most five aggregate/pointer facts to `facts.json`:
  metrics.jsonl         named metrics with denominator, coverage, provenance, first-measured version,
                        and a factual comparison with the previous same-scope snapshot.

`decide` appends the user's accept/reject on one recommendation to an append-only log (out dir default
= trace's --out); `decide` and longitudinal `metrics` are the timestamped improve outputs:
  decisions.jsonl       {rec_id, decision, at (ISO-8601), title?, note?} per line; re-decisions append
                        (history preserved, latest row for a rec_id wins). rec_id must be
                        <lens>/<kebab-gist>.

The projection outputs (trace/reviews/audit) are byte-identical across re-runs of the same input
(no run timestamp; stable ordering;
sort_keys on every dump). Semantic labels carry provenance: rule-derived values are `inferred` with a
versioned `rule` id, unresolvable values are `{"provenance": "unknown"}`, and structurally parsed
severities are `explicit` (never keyword-guessed from prose). Content (prompts/commands/file bodies)
is never stored; only bounded 120-char `head` snippets for evidence pointers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

import codexlog  # noqa: E402

from cclog import (  # noqa: E402
    PARSER_VERSION,
    SKIP_DIRS,
    TRANSCRIPT_KINDS,
    coalesce_messages,
    detect_kind,
    parse_transcript_file,
    scope_of,
    stable_id,
)
from common import (  # noqa: E402
    SEVERITIES,
    WorkflowError,
    delegation_scope_drift,
    ensure_project_state_dir,
    find_project_root,
    has_project_config,
    load_config,
    load_tasks,
    load_yaml,
    machine_dir,
    parse_iso_timestamp,
    project_state_path,
    registry_path,
    resolve_project_paths,
    write_text_atomic,
)

HEAD_LEN = 120
CONTEXT_HEAVY_BYTES = 100 * 1024
DELEGATION_DIRECT_WORK_MIN = 5
DELEGATION_CONTEXT_TOKENS_MIN = 100_000
PROJECT_LENS_SCOPE = "project"
USER_HABIT_LENS_SCOPE = "user-habit"
FINDING_TYPES = (
    "correctness", "scope", "architecture", "verification", "reproducibility", "reporting",
)

# Deterministic project maturity thresholds. Tune lifts the exact gate that previously lived only
# in skills/improve/SKILL.md. Calibrate follows design §6.2's deliberately generous boundary: two
# observed rounds plus one traced session and any explicit workflow-feedback signal. Enforce stays
# in the vocabulary for the next arc; no count combination can return it here.
MATURITY_STAGES = ("bootstrap", "calibrate", "tune", "enforce")
CALIBRATE_MIN_TRACED_SESSIONS = 1
CALIBRATE_MIN_ROUNDS = 2
CALIBRATE_MIN_ACTIVITY_SIGNALS = 1
TUNE_MIN_REVIEW_FEEDBACK = 5
TUNE_MIN_FINDINGS = 20

# Lens selection is mode policy, separate from the unchanged lens calculations below. Cross-scope
# lenses run over only the projection directory selected by that mode.
LENS_SCOPES = {
    "main_direct_work": frozenset({USER_HABIT_LENS_SCOPE}),
    "verification_debt": frozenset({PROJECT_LENS_SCOPE}),
    "retry_loops": frozenset({PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE}),
    "context_heavy": frozenset({PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE}),
    "delegation_pattern": frozenset({USER_HABIT_LENS_SCOPE}),
    "delegation_opportunity": frozenset({PROJECT_LENS_SCOPE}),
    "worker_scope_drift": frozenset({PROJECT_LENS_SCOPE}),
    "warn_friction": frozenset({PROJECT_LENS_SCOPE}),
    "adaptive_feedback": frozenset({PROJECT_LENS_SCOPE}),
    "error_landscape": frozenset({PROJECT_LENS_SCOPE}),
    "env_unpreparedness": frozenset({PROJECT_LENS_SCOPE}),
    "review_association": frozenset({PROJECT_LENS_SCOPE}),
    "finding_concentration": frozenset({PROJECT_LENS_SCOPE}),
    "coverage_caveats": frozenset({PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE}),
    "evidence_link": frozenset({PROJECT_LENS_SCOPE}),
}

# ------------------------------------------------------------------ verify-cmd-v2
# Conservative Bash-command classification. Order matters: TEST, then BUILD (so `tsc --build` is a
# build, not a typecheck), then LINT/TYPECHECK. Non-matches are counted as unclassified_shell.
# A test runner/verb is required — a bare `tests/` path mention (`cat tests/x.py`, `git diff tests/`,
# `ls tests/`) is NOT verification. When only a tests/ path is named, the command must START with a
# known test runner to count (so `python tests/run.py` / `pytest tests/` do, but `cat`/`git`/`ls`/`rm`
# do not).
_TEST_RE = re.compile(
    r"\bpytest\b|\bunittest\b|\buv run\b[^\n]*\btest|\bnpm (?:run )?test\b|\bnpx jest\b"
    r"|\bjest\b|\bvitest\b|\bcargo test\b|\bgo test\b|\bmake (?:test|check)\b"
)
_TEST_PATH_RE = re.compile(r"\btests/")
_TEST_RUNNER_START_RE = re.compile(r"^\s*(?:pytest|py\.test|python3?|tox|nox|nosetests)\b")
_BUILD_RE = re.compile(
    r"\bmake build\b|\bnpm run build\b|\byarn build\b|\bcargo build\b|\bgo build\b"
    r"|\btsc (?:-b\b|--build\b)"
)
_LINT_RE = re.compile(r"\bruff\b|\bflake8\b|\beslint\b")
_TYPECHECK_RE = re.compile(r"\bmypy\b|\btsc\b|\btypecheck\b|\bpyright\b|\bty check\b")
_ENV_ERROR_SIGNATURES = (
    ("python-module-missing", re.compile(r"\bModuleNotFoundError\b|\bNo module named\b", re.I)),
    ("js-module-missing", re.compile(r"\bERR_MODULE_NOT_FOUND\b|\bCannot find module\b", re.I)),
    ("dependency-resolution-failed", re.compile(
        r"\bNo matching distribution found\b|\bCould not find a version that satisfies\b"
        r"|\bfailed to select a version for\b", re.I)),
    ("command-not-found", re.compile(r"\bcommand not found\b|\bexecutable file not found\b", re.I)),
)


def classify_verification(command: str) -> str | None:
    """verify-cmd-v2: 'test' | 'build' | 'lint' | 'typecheck' | None (unclassified shell)."""
    if _TEST_RE.search(command):
        return "test"
    if _TEST_PATH_RE.search(command) and _TEST_RUNNER_START_RE.match(command):
        return "test"
    if _BUILD_RE.search(command):
        return "build"
    if _LINT_RE.search(command):
        return "lint"
    if _TYPECHECK_RE.search(command):
        return "typecheck"
    return None


def _head(text: str) -> str:
    return text.strip()[:HEAD_LEN]


def _norm_cmd(command: str) -> str:
    return " ".join(command.split())


def _mode(counter: Counter):
    """Most frequent observed value (deterministic tie-break: count desc, then value asc), or a
    provenance-unknown marker when nothing was observed."""
    if not counter:
        return {"provenance": "unknown"}
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# ------------------------------------------------------------------ discovery (§4)
def discover(sources: list[Path], project_filter: set[str]) -> list[tuple[Path, tuple[str, ...], str]]:
    """Return sorted [(abs_path, rel_parts, kind)] over every source (no cross-source merge)."""
    found: list[tuple[Path, tuple[str, ...], str]] = []
    for src in sources:
        if not src.is_dir():
            continue
        for slug_dir in sorted(src.iterdir()):
            if not slug_dir.is_dir():
                continue
            if project_filter and slug_dir.name not in project_filter:
                continue
            for path in sorted(slug_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(src).parts
                if any(p in SKIP_DIRS for p in rel_parts):
                    continue
                found.append((path, rel_parts, detect_kind(rel_parts)))
    return found


# ------------------------------------------------------------- retry loops (§11)
def _detect_retry_loops(bash_calls: list[dict], result_by_tuid: dict[str, dict]):
    """same-cmd-refail-v1: a chain (>=3) of the same normalized command each re-run after the
    previous run's result was is_error. Returns (loop_count, examples)."""
    chains = []
    cur = None
    for tc in bash_calls:
        norm = _norm_cmd(tc["command"])
        res = result_by_tuid.get(tc["tool_call_id"])
        is_err = bool(res.get("is_error")) if res else False
        if cur and cur["cmd"] == norm and cur["last_err"]:
            cur["runs"] += 1
            cur["last_err"] = is_err
        else:
            if cur:
                chains.append(cur)
            cur = {"cmd": norm, "runs": 1, "last_err": is_err,
                   "line": tc["ordinal"], "head": _head(tc["command"])}
    if cur:
        chains.append(cur)
    loops = [c for c in chains if c["runs"] >= 3]
    examples = [{"line": c["line"], "head": c["head"]} for c in loops[:5]]
    return len(loops), examples


def _env_error_facts(tool_results: list[dict]) -> dict:
    signatures: Counter = Counter()
    examples: list[dict] = []
    for result in tool_results:
        if result.get("is_error") is not True:
            continue
        text = "\n".join(part for part in (
            result.get("content_text"), result.get("stderr_head")) if isinstance(part, str))
        for name, pattern in _ENV_ERROR_SIGNATURES:
            if not pattern.search(text):
                continue
            signatures[name] += 1
            if len(examples) < 5:
                examples.append({"line": result.get("ordinal"), "signature": name})
    return {
        "signatures": dict(sorted(signatures.items())), "examples": examples,
        "provenance": "inferred", "rule": "env-error-signature-v1",
    }


# ------------------------------------------------------------- per-file builders
def _read_agent_meta(path: Path, kind: str):
    if kind not in ("subagent_transcript", "workflow_subagent_transcript"):
        return None
    meta_path = path.with_name(path.stem + ".meta.json")
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {"agentType": data.get("agentType"),
            "description": data.get("description"),
            "spawnDepth": data.get("spawnDepth")}


def _linked_transcript(path: Path, kind: str, agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    subdir = (path.parent / path.stem / "subagents") if kind == "main_transcript" else path.parent
    cand = subdir / f"agent-{agent_id}.jsonl"
    return str(cand) if cand.exists() else None


def _build_delegation(tc: dict, result_by_tuid: dict[str, dict], scope: dict, path: Path, kind: str) -> dict:
    row = {
        "project": scope["project"],
        "session_id": scope["session_id"],
        "file": str(path),
        "line": tc["ordinal"],
        "tool": tc["tool_name_raw"],
        "subagent_type": tc.get("subagent_type"),
        "model_requested": tc.get("model_requested"),
    }
    res = result_by_tuid.get(tc["tool_call_id"])
    if res is not None:
        row["resolved_model"] = res.get("tur_resolved_model")
        row["agent_id"] = res.get("tur_agent_id")
        row["status"] = res.get("tur_status")
        row["is_async"] = res.get("tur_is_async")
        row["linked_transcript"] = _linked_transcript(path, kind, res.get("tur_agent_id"))
    else:
        # no result record joined -> routing evidence is unresolvable, never guessed (invariant #11)
        for k in ("resolved_model", "agent_id", "status", "is_async"):
            row[k] = {"provenance": "unknown"}
        row["linked_transcript"] = None
    return row


def _build_session(path: Path, kind: str, session_kind: str, scope: dict, parsed: dict):
    events = parsed["events"]
    tool_calls = parsed["tool_calls"]
    tool_results = parsed["tool_results"]
    groups = coalesce_messages(events, tool_calls)
    result_by_tuid = {tr["tool_use_id"]: tr for tr in tool_results if tr.get("tool_use_id")}

    models = Counter(g["model_norm"] for g in groups if g.get("model_norm"))
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for g in groups:
        usage["input"] += g.get("input_tokens") or 0
        usage["output"] += g.get("output_tokens") or 0
        usage["cache_read"] += g.get("cache_read_input_tokens") or 0
        usage["cache_creation"] += g.get("cache_creation_input_tokens") or 0

    turns = sum(1 for ev in events if ev["event_type"] == "user_instruction")
    tool_counts = Counter(tc["tool_category"] for tc in tool_calls)

    tool_err = sum(1 for tr in tool_results if tr.get("is_error") is True)
    parse_err = sum(1 for ev in events if ev.get("event_subtype") == "parse_error")
    api_err = 0
    cli_versions: set[str] = set()
    cwds: Counter = Counter()
    branches: Counter = Counter()
    timestamps: list[str] = []
    for ev in events:
        ts = ev.get("timestamp")
        if isinstance(ts, str):
            timestamps.append(ts)
        if ev.get("cwd"):
            cwds[ev["cwd"]] += 1
        if ev.get("git_branch"):
            branches[ev["git_branch"]] += 1
        ej = ev.get("extras_json")
        if ej:
            try:
                extras = json.loads(ej)
            except json.JSONDecodeError:
                extras = {}
            if extras.get("is_api_error"):
                api_err += 1
            cv = extras.get("cli_version")
            if isinstance(cv, str):
                cli_versions.add(cv)

    spawn_calls = [tc for tc in tool_calls if tc["tool_category"] == "agent_spawn"]
    bash_calls = [tc for tc in tool_calls if tc["tool_name_raw"] == "Bash" and tc.get("command")]

    verify_runs = verify_failed = build_runs = build_failed = unclassified = 0
    verify_examples: list[dict] = []
    build_examples: list[dict] = []
    for tc in bash_calls:
        cat = classify_verification(tc["command"])
        res = result_by_tuid.get(tc["tool_call_id"])
        is_err = bool(res.get("is_error")) if res else False
        if cat in ("test", "lint", "typecheck"):
            verify_runs += 1
            verify_failed += int(is_err)
            if len(verify_examples) < 5:
                verify_examples.append({"line": tc["ordinal"], "head": _head(tc["command"])})
        elif cat == "build":
            build_runs += 1
            build_failed += int(is_err)
            if len(build_examples) < 5:
                build_examples.append({"line": tc["ordinal"], "head": _head(tc["command"])})
        else:
            unclassified += 1

    retry_count, retry_examples = _detect_retry_loops(bash_calls, result_by_tuid)

    over = 0
    max_bytes = 0
    for tr in tool_results:
        cb = tr.get("content_bytes") or 0
        max_bytes = max(max_bytes, cb)
        if cb > CONTEXT_HEAVY_BYTES:
            over += 1

    row = {
        "project": scope["project"],
        "session_id": scope["session_id"],
        "kind": session_kind,
        "agent_id": scope["agent_id"],
        "workflow_id": scope["workflow_id"],
        "file": str(path),
        "agent_meta": parsed.get("agent_meta") or _read_agent_meta(path, kind),
        "cwd": _mode(cwds),
        "git_branch": _mode(branches),
        "started_at": min(timestamps) if timestamps else {"provenance": "unknown"},
        "ended_at": max(timestamps) if timestamps else {"provenance": "unknown"},
        "cli_versions": sorted(cli_versions),
        "models": dict(sorted(models.items())),
        "turns": {"value": turns, "provenance": "inferred", "rule": "turn-index-v1"},
        "usage": usage,
        "tools": {"by_category": dict(sorted(tool_counts.items())),
                  "provenance": "inferred", "rule": "tool-category-v1"},
        "errors": {"api": api_err, "tool": tool_err, "parse": parse_err},
        "env_unpreparedness": _env_error_facts(tool_results),
        "delegations": len(spawn_calls),
        "verification": {"runs": verify_runs, "failed": verify_failed,
                         "rule": "verify-cmd-v2", "provenance": "inferred", "examples": verify_examples},
        "build": {"runs": build_runs, "failed": build_failed,
                  "rule": "verify-cmd-v2", "provenance": "inferred", "examples": build_examples},
        "unclassified_shell": unclassified,
        "retry_loops": {"count": retry_count, "rule": "same-cmd-refail-v1",
                        "provenance": "inferred", "examples": retry_examples},
        "context_heavy": {"tool_results_over_100kb": over, "max_result_bytes": max_bytes},
        "parser_version": parsed.get("parser_version", PARSER_VERSION),
    }
    delegations = [_build_delegation(tc, result_by_tuid, scope, path, kind) for tc in spawn_calls]
    return row, delegations


# ------------------------------------------------------------------ trace driver
def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# self-session truncation: when `waystone improve trace` runs inside a live CC session, that session's own
# main transcript is mid-write and includes this very invocation. We stop parsing it at the improve
# invocation so the trace does not pollute itself. Anchor detection re-scans ONLY the one matched file
# (main transcript whose stem == CLAUDE_CODE_SESSION_ID) and copies no content into outputs.
_SELF_CMD_NAME_RE = re.compile(r"<command-name>([^<]*)</command-name>")


def _user_message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _self_session_anchor(path: Path) -> tuple[int | None, str | None, int]:
    """Pre-scan the one matched main transcript for the truncation anchor. Returns
    (anchor_line_no, anchor_kind, lines_excluded); anchor_line_no is 1-based (parsing stops before
    it). Priority: LAST command-tag (a real `/waystone:improve` skill invocation — the tag must
    LEAD the message, distinguishing it from a user pasting the tag mid-text); else, if none, the LAST
    `waystone ... improve trace` Bash tool_use. lines_excluded counts raw lines from the anchor to EOF."""
    cmd_tag_line: int | None = None
    tool_use_line: int | None = None
    total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f, start=1):
                total = line_no
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                rtype = rec.get("type")
                if rtype == "user":
                    stripped = _user_message_text(rec.get("message") or {}).lstrip()
                    if stripped.startswith("<command-name>"):
                        m = _SELF_CMD_NAME_RE.search(stripped)
                        if m and "/waystone:improve" in m.group(1):
                            cmd_tag_line = line_no
                elif rtype == "assistant":
                    content = (rec.get("message") or {}).get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not (isinstance(b, dict) and b.get("type") == "tool_use"
                                    and b.get("name") == "Bash"):
                                continue
                            cmd = (b.get("input") or {}).get("command")
                            if isinstance(cmd, str) and "improve trace" in cmd and "waystone" in cmd:
                                tool_use_line = line_no
                                break
    except OSError:
        return None, None, 0
    if cmd_tag_line is not None:
        return cmd_tag_line, "command-tag", total - cmd_tag_line + 1
    if tool_use_line is not None:
        return tool_use_line, "tool-use", total - tool_use_line + 1
    return None, None, 0


def _cwd_belongs_to_project(cwd, project_roots: tuple[Path, ...]) -> bool | None:
    """Return explicit cwd membership, or None when cwd cannot establish provenance."""
    if not isinstance(cwd, str) or not cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
        return any(resolved.is_relative_to(root) for root in project_roots)
    except (OSError, ValueError):
        return None


def run_trace(sources: list[Path], projects: set[str], out_dir: Path,
              host: str = "claude", project_root: Path | None = None) -> dict:
    project_roots = resolve_project_paths(project_root) if project_root is not None else ()
    canonical_root = project_roots[0] if project_roots else None
    project_name = load_config(canonical_root).get("project") if canonical_root is not None else None
    if canonical_root is not None and (not isinstance(project_name, str) or not project_name.strip()):
        raise WorkflowError(f"project config has no non-empty project name: {canonical_root}")
    if host == "codex":
        return _run_codex_trace(
            sources, projects, out_dir, canonical_root, project_roots, project_name=project_name)
    if host != "claude":
        raise WorkflowError(f"trace host must be claude|codex, got {host!r}")
    # A Claude slug is lossy, so project mode scans every candidate and attributes only by parsed
    # cwd. User-wide --project remains a path-layout filter because it has no canonical root.
    files = discover(sources, set() if canonical_root is not None else projects)
    files_by_kind: Counter = Counter(kind for _, _, kind in files)
    project_exclusions = {"cwd_outside_project": [], "cwd_unknown": []}

    # when running inside a live CC session, truncate that session's own mid-write transcript at the
    # improve invocation (env set by the harness); unset/empty -> byte-identical to a plain run
    self_sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    self_session: dict | None = None
    if self_sid:
        self_session = {"session_id": self_sid, "file_found": False,
                        "anchor": None, "lines_excluded": 0}

    session_rows: list[dict] = []
    delegation_rows: list[dict] = []
    event_type_counts: Counter = Counter()
    unknown_raw_types: Counter = Counter()
    files_unreadable: dict[str, str] = {}
    record_parse_errors = 0
    replayed_records_skipped = 0
    partial_tail_lines = 0

    for path, rel_parts, kind in files:
        if kind not in TRANSCRIPT_KINDS:
            continue  # non-transcript kinds are manifested in files_by_kind only (M1)
        scope = scope_of(rel_parts)
        session_kind = TRANSCRIPT_KINDS[kind]
        stop_before: int | None = None
        if self_sid and kind == "main_transcript" and scope["session_id"] == self_sid:
            self_session["file_found"] = True
            anchor_line, anchor_kind, excluded = _self_session_anchor(path)
            if anchor_line is not None:
                stop_before = anchor_line
                self_session["anchor"] = anchor_kind
                self_session["lines_excluded"] = excluded
        try:
            parsed = parse_transcript_file(
                path,
                file_id=stable_id("file", str(path)),
                server=None,
                project=scope["project"],
                session_id=scope["session_id"],
                agent_id=scope["agent_id"],
                workflow_id=scope["workflow_id"],
                is_sidechain_file=session_kind != "main",
                stop_before_line=stop_before,
            )
        except (OSError, UnicodeDecodeError) as e:
            # an unreadable/vanished INPUT transcript must not abort the run nor be mislabeled as a
            # write failure — record it in coverage (§3.8 evidence integrity) and keep going
            files_unreadable["/".join(rel_parts)] = type(e).__name__
            continue
        row, dels = _build_session(path, kind, session_kind, scope, parsed)
        if canonical_root is not None:
            membership = _cwd_belongs_to_project(row.get("cwd"), project_roots)
            if membership is not True:
                reason = "cwd_unknown" if membership is None else "cwd_outside_project"
                project_exclusions[reason].append(str(path))
                continue
            row["project"] = project_name
            for delegation in dels:
                delegation["project"] = project_name
        session_rows.append(row)
        delegation_rows.extend(dels)

        for ev in parsed["events"]:
            event_type_counts[ev["event_type"]] += 1
            if ev["event_type"] == "unknown_raw":
                if ev.get("event_subtype") == "parse_error":
                    record_parse_errors += 1
                else:
                    unknown_raw_types[ev.get("event_subtype") or "no_type"] += 1
        replayed_records_skipped += parsed["replayed_skipped"]
        partial_tail_lines += parsed["partial_tail_lines"]

    session_rows.sort(key=lambda r: (r["project"] or "", r["file"]))
    delegation_rows.sort(key=lambda r: (r["project"] or "", r["file"], r["line"]))

    coverage = {
        "parser_version": PARSER_VERSION,
        "generated_from": sorted(str(s) for s in sources),
        "sources_missing": sorted(str(s) for s in sources if not s.is_dir()),
        "files_by_kind": dict(sorted(files_by_kind.items())),
        "files_skipped": sum(v for k, v in files_by_kind.items() if k.startswith("unknown_")),
        "files_unreadable": dict(sorted(files_unreadable.items())),
        "files_unreadable_total": len(files_unreadable),
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "unknown_raw_types": dict(sorted(unknown_raw_types.items())),
        "record_parse_errors": record_parse_errors,
        "replayed_records_skipped": replayed_records_skipped,
        "partial_tail_lines": partial_tail_lines,
        "row_totals": {"sessions": len(session_rows), "delegations": len(delegation_rows)},
    }
    if canonical_root is not None:
        coverage["project_filter"] = {
            "project_root": str(canonical_root),
            "sessions_excluded": {
                reason: sorted(paths) for reason, paths in sorted(project_exclusions.items())
            },
        }
    # only present when running inside a live CC session (env set) — absent otherwise (byte-identical)
    if self_session is not None:
        coverage["self_session"] = self_session

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sessions.jsonl").write_text(
        "".join(_dumps(r) + "\n" for r in session_rows), encoding="utf-8")
    (out_dir / "delegations.jsonl").write_text(
        "".join(_dumps(r) + "\n" for r in delegation_rows), encoding="utf-8")
    (out_dir / "parse_coverage.json").write_text(_dumps(coverage) + "\n", encoding="utf-8")
    return coverage


def _run_codex_trace(sources: list[Path], projects: set[str], out_dir: Path,
                     project_root: Path | None = None,
                     project_roots: tuple[Path, ...] = (),
                     project_name: str | None = None) -> dict:
    """Project Codex rollouts through the same session/delegation schema as Claude trace."""
    files = codexlog.discover(sources)
    files_by_kind: Counter = Counter()
    session_rows: list[dict] = []
    delegation_rows: list[dict] = []
    event_type_counts: Counter = Counter()
    unknown_raw_types: Counter = Counter()
    files_unreadable: dict[str, str] = {}
    record_parse_errors = 0
    partial_tail_lines = 0
    unknown_tool_result_status = 0
    project_exclusions = {"cwd_outside_project": [], "cwd_unknown": []}

    self_sid = os.environ.get("CODEX_THREAD_ID") or None
    self_session = ({"session_id": self_sid, "file_found": False,
                     "anchor": None, "lines_excluded": 0}
                    if self_sid else None)

    for path, rel_parts in files:
        stop_before = None
        if self_sid and codexlog.rollout_id(path) == self_sid:
            self_session["file_found"] = True
            anchor, excluded = codexlog.self_session_anchor(path)
            if anchor is not None:
                stop_before = anchor
                self_session["anchor"] = "tool-use"
                self_session["lines_excluded"] = excluded
        try:
            parsed = codexlog.parse_transcript_file(
                path, file_id=stable_id("file", str(path)), stop_before_line=stop_before,
            )
        except (OSError, UnicodeDecodeError) as e:
            files_unreadable["/".join(rel_parts)] = type(e).__name__
            continue
        scope = parsed["scope"]
        if projects and scope.get("project") not in projects:
            continue
        session_kind = parsed["session_kind"]
        kind = f"codex_{session_kind}_transcript"
        row, delegations = _build_session(path, kind, session_kind, scope, parsed)
        if project_root is not None:
            membership = _cwd_belongs_to_project(row.get("cwd"), project_roots)
            if membership is not True:
                reason = "cwd_unknown" if membership is None else "cwd_outside_project"
                project_exclusions[reason].append(str(path))
                continue
            row["project"] = project_name
            for delegation in delegations:
                delegation["project"] = project_name
        files_by_kind[kind] += 1
        session_rows.append(row)
        delegation_rows.extend(delegations)

        for ev in parsed["events"]:
            event_type_counts[ev["event_type"]] += 1
            if ev["event_type"] == "unknown_raw":
                if ev.get("event_subtype") == "parse_error":
                    record_parse_errors += 1
                else:
                    unknown_raw_types[ev.get("event_subtype") or "no_type"] += 1
        partial_tail_lines += parsed["partial_tail_lines"]
        unknown_tool_result_status += sum(
            1 for result in parsed["tool_results"] if result.get("is_error") is None
        )

    session_rows.sort(key=lambda row: (row["project"] or "", row["file"]))
    delegation_rows.sort(key=lambda row: (row["project"] or "", row["file"], row["line"]))
    coverage = {
        "host": "codex",
        "parser_version": codexlog.PARSER_VERSION,
        "generated_from": sorted(str(source) for source in sources),
        "sources_missing": sorted(str(source) for source in sources if not source.is_dir()),
        "files_by_kind": dict(sorted(files_by_kind.items())),
        "files_skipped": 0,
        "files_unreadable": dict(sorted(files_unreadable.items())),
        "files_unreadable_total": len(files_unreadable),
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "unknown_raw_types": dict(sorted(unknown_raw_types.items())),
        "record_parse_errors": record_parse_errors,
        "replayed_records_skipped": 0,
        "partial_tail_lines": partial_tail_lines,
        "unknown_tool_result_status": unknown_tool_result_status,
        "row_totals": {"sessions": len(session_rows), "delegations": len(delegation_rows)},
    }
    if project_root is not None:
        coverage["project_filter"] = {
            "project_root": str(project_root),
            "sessions_excluded": {
                reason: sorted(paths) for reason, paths in sorted(project_exclusions.items())
            },
        }
    if self_session is not None:
        coverage["self_session"] = self_session

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sessions.jsonl").write_text(
        "".join(_dumps(row) + "\n" for row in session_rows), encoding="utf-8",
    )
    (out_dir / "delegations.jsonl").write_text(
        "".join(_dumps(row) + "\n" for row in delegation_rows), encoding="utf-8",
    )
    (out_dir / "parse_coverage.json").write_text(
        _dumps(coverage) + "\n", encoding="utf-8",
    )
    return coverage


# ================================================================== reviews (§8)
# Project review evidence, projected — NOT re-implemented. The on-disk feedback format is exactly
# what `review.ingest` writes: a metadata header, the byte-exact reviewer body, then an APPENDED
# markdown triage table under `## Findings (triage skeleton …)` whose rows are
#   | JW-GPT-NNN — title | <severity> | <verdict> | <evidence> | <task id> |
# We parse ONLY that appended table (the last such heading), never the verbatim body (§3.8: no
# defensive multi-format guessing). Finding-derived tasks carry the review-round link in their
# `origin` field (`review-<round-id>`), set by `waystone task add --origin` in skills/review/SKILL.md — the
# `round` field records the FIXING round (stamped at close), so origin is the correct join key.
_TRIAGE_HEADING = "## Findings (triage skeleton"
_FINDING_ID_RE = re.compile(r"JW-GPT-\d+")
_VERDICT_RE = re.compile(r"\b(REAL|REJECTED|NEEDS-RULING)\b", re.IGNORECASE)
_FINDING_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:\.?[A-Za-z0-9_.-]+/)+(?:[A-Za-z0-9_.-]+))"
    r"(?::(\d+)(?::\d+)?)?")


def _finding_evidence(value: str) -> tuple[list[str], list[str]]:
    paths: set[str] = set()
    pointers: set[str] = set()
    without_urls = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "", value, flags=re.I)
    for match in _FINDING_PATH_RE.finditer(without_urls):
        path = match.group(1).removeprefix("./")
        if "://" not in path and ".." not in path.split("/"):
            paths.add(path)
            if match.group(2):
                pointers.add(f"{path}:{match.group(2)}")
    return sorted(paths), sorted(pointers)


def _finding_paths(value: str) -> list[str]:
    return _finding_evidence(value)[0]


def _parse_triage(feedback_text: str) -> list[dict]:
    """Structured findings from the appended triage table only. Severity and optional taxonomy
    type are read from named table cells, never guessed from prose. A missing, blank, or
    noncanonical type is ``unknown`` (invariant #11)."""
    idx = feedback_text.rfind(_TRIAGE_HEADING)
    if idx < 0:
        return []
    out: list[dict] = []
    columns: dict[str, int] | None = None
    first_line = feedback_text[:idx].count("\n") + 1
    for line_number, line in enumerate(feedback_text[idx:].splitlines(), first_line):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0].strip().lower() == "finding":
            columns = {}
            for index, cell in enumerate(cells):
                name = re.sub(r"\s+", " ", cell.strip().lower())
                if name.startswith("verdict"):
                    name = "verdict"
                elif name in ("task", "task-id"):
                    name = "task id"
                columns[name] = index
            continue
        m = _FINDING_ID_RE.search(cells[0]) if cells else None
        if not m:
            continue  # header row, separator row, or a non-finding table line

        def cell(name: str, fallback: int | None = None) -> str:
            position = columns.get(name) if columns is not None else fallback
            return cells[position] if position is not None and position < len(cells) else ""

        sev = cell("severity", 1).strip().strip("`").lower()
        severity = sev if sev in SEVERITIES else None
        raw_type = cell("type").strip().strip("`").lower()
        finding_type = raw_type if raw_type in FINDING_TYPES else "unknown"
        status = None
        verdict = cell("verdict", 2)
        if verdict:
            vm = _VERDICT_RE.search(verdict)
            status = vm.group(1).upper() if vm else None
        task_id = cell("task id", 4).strip().strip("`")
        finding = {"id": m.group(0), "severity": severity, "type": finding_type, "status": status,
                   "task_id": task_id or None, "line": line_number}
        paths, pointers = _finding_evidence(cell("evidence", 3))
        if paths:
            finding["paths"] = paths
            finding["path_provenance"] = "inferred"
            finding["path_rule"] = "triage-evidence-path-v1"
        if pointers:
            finding["evidence_pointers"] = pointers
        out.append(finding)
    return out


def _finding_tasks_by_round(root: Path) -> dict[str, list[dict]]:
    """Finding-derived tasks grouped by REVIEW round (from `origin: review-<round-id>`), read from
    tasks.yaml + tasks.archive.yaml. Severity comes structurally from the task's `severity` field."""
    docs: list[tuple[str, dict]] = []
    arch = root / "tasks.archive.yaml"
    if arch.is_file():
        try:
            data = load_yaml(arch)
            if isinstance(data, dict):
                docs.append(("archive", data))
        except (OSError, yaml.YAMLError):
            pass
    try:
        docs.append(("live", load_tasks(root)))
    except (OSError, yaml.YAMLError):
        pass
    histories: dict[str, list[tuple[str, dict]]] = {}
    for source, doc in docs:
        for task in doc.get("tasks", []) or []:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                histories.setdefault(task["id"], []).append((source, task))
    by_round: dict[str, list[dict]] = {}
    terminal = {"done", "dropped"}
    for task_id, history in histories.items():
        fixing_rounds = list(dict.fromkeys(
            task.get("round") for _source, task in history if isinstance(task.get("round"), str)))
        status_history = [{
            "source": source, "status": task.get("status"), "fixing_round": task.get("round"),
        } for source, task in history]
        reopen_count = sum(
            before.get("status") in terminal and after.get("status") not in terminal
            for (_before_source, before), (_after_source, after) in zip(history, history[1:]))
        origins = list(dict.fromkeys(
            task.get("origin") for _source, task in history
            if isinstance(task.get("origin"), str) and task["origin"].startswith("review-")))
        effective = history[-1][1]
        for origin in origins:
            if not (isinstance(origin, str) and origin.startswith("review-")):
                continue
            rid = origin[len("review-"):]
            sev = effective.get("severity")
            severity = sev if sev in SEVERITIES else None
            by_round.setdefault(rid, []).append({
                "id": task_id,
                "severity": severity,
                "type": "unknown",
                "status": effective.get("status"),
                "round": effective.get("round"),
                "session_id": effective.get("session_id"),
                "anchor": effective.get("anchor"),
                "review_origin": rid,
                "fixing_rounds": fixing_rounds,
                "status_history": status_history,
                "reopen_count": reopen_count,
                "source": "task",
                "provenance": "explicit" if severity else "unknown",
            })
    return by_round


def _round_exposure_rows(root: Path) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    directory = project_state_path(root) / "exposure"
    if not directory.is_dir():
        return rows, skipped
    for path in sorted(directory.glob("round-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            skipped += 1
            continue
        if (not isinstance(row, dict) or row.get("schema") != "waystone-round-exposure-1"
                or not isinstance(row.get("round_id"), str)
                or not isinstance(row.get("at"), str)):
            skipped += 1
            continue
        rows.append({**row, "_file": str(path)})
    rows.sort(key=lambda row: (row["at"], row["round_id"], row["_file"]))
    return rows, skipped


def _latest_round_exposures(exposures: list[dict]) -> dict[str, dict]:
    def order(row: dict) -> tuple[str, int, str]:
        path = Path(row.get("_file") or "")
        stem = path.stem
        base = f"round-{row.get('round_id')}"
        suffix = stem.removeprefix(base + "-") if stem.startswith(base + "-") else ""
        sequence = int(suffix) if suffix.isdigit() else 1
        return row.get("at") or "", sequence, str(path)

    latest: dict[str, dict] = {}
    for row in exposures:
        round_id = row.get("round_id")
        if not isinstance(round_id, str):
            continue
        current = latest.get(round_id)
        if current is None or order(row) > order(current):
            latest[round_id] = row
    return latest


def _round_session_binding(round_id: str, exposures: list[dict] | dict[str, dict]) -> tuple[object, str, str | None]:
    latest = exposures if isinstance(exposures, dict) else _latest_round_exposures(exposures)
    match = latest.get(round_id)
    if match is not None and isinstance(match.get("session_id"), str):
        return match["session_id"], "explicit", None
    reason = "missing-round-exposure" if match is None else "round-session-unavailable"
    return {"provenance": "unknown"}, "unknown", reason


def _review_binding(request_file: Path | None, round_id: str, mode: str,
                    sidecars: list[dict]) -> dict:
    def result(target_sha=None, base_sha=None, provenance="unknown", reason=None, source=None,
               *, cycle=None, reviewers=None, profile_fingerprint=None) -> dict:
        return {
            "target_sha": target_sha, "base_sha": base_sha,
            "review_cycle": cycle, "reviewers": reviewers,
            "review_profile_fingerprint": profile_fingerprint,
            "review_binding_provenance": provenance,
            "review_binding_reason": reason, "review_binding_source": source,
        }

    if mode == "pr":
        pr_sidecars = [row for row in sidecars if row.get("mode") == "pr"]
        if not pr_sidecars:
            return result(reason="missing-pr-freeze-sidecar")
        latest_cycle = max(row["cycle"] for row in pr_sidecars)
        cycle_rows = [row for row in pr_sidecars if row["cycle"] == latest_cycle]
        bindings = {(
            row["target_sha"], row["base_sha"], tuple(row["reviewers"]),
            row.get("profile_fingerprint"), row["pr"],
        ) for row in cycle_rows}
        if len(bindings) != 1:
            return result(reason="conflicting-pr-freeze-sidecars")
        latest = max(cycle_rows, key=lambda row: (row["at"], row["_file"]))
        return result(
            latest["target_sha"], latest["base_sha"], "explicit", None,
            "pr-freeze-sidecar", cycle=latest_cycle, reviewers=list(latest["reviewers"]),
            profile_fingerprint=latest.get("profile_fingerprint"))
    if request_file is None:
        return result(reason="missing-review-request")
    import review

    packet_binding = review.parse_packet_request_binding(request_file)
    if sidecars:
        def sidecar_order(row: dict) -> tuple[str, int, str]:
            path = Path(row["_file"])
            match = re.search(r"-request\.binding(?:-(\d+))?\.json$", path.name)
            sequence = int(match.group(1)) if match and match.group(1) else 1
            return row["at"], sequence, str(path)

        latest = max(sidecars, key=sidecar_order)
        if packet_binding is None:
            return result(reason="missing-structured-reviewing-line")
        if packet_binding != (latest["target_sha"], latest.get("base_sha")):
            return result(reason="request-binding-sidecar-mismatch")
        return result(
            latest["target_sha"], latest.get("base_sha"), "explicit", None,
            "round-request-sidecar", reviewers=list(latest.get("reviewers") or []))
    try:
        text = request_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result(reason="unreadable-review-request")
    valid = [marker for marker in review.parse_markers(text, "review-cycle")
             if review.marker_valid(marker)]
    markers = [marker for marker in valid if marker.get("round_id") == round_id]
    if not markers:
        reason = "round-mismatched-review-marker" if valid else "missing-valid-review-cycle-marker"
        return result(reason=reason)
    latest_cycle = max(marker["cycle"] for marker in markers)
    bindings = {(marker.get("target_sha"), marker.get("base_sha"))
                for marker in markers if marker["cycle"] == latest_cycle}
    if len(bindings) != 1:
        return result(reason="conflicting-review-cycle-markers")
    target_sha, base_sha = next(iter(bindings))
    latest = max((marker for marker in markers if marker["cycle"] == latest_cycle),
                 key=lambda marker: marker.get("_at") or "")
    return result(
        target_sha, base_sha, "explicit", None, "round-bound-request-marker",
        cycle=latest_cycle,
        reviewers=list(latest["reviewers"]) if isinstance(latest.get("reviewers"), list) else None,
        profile_fingerprint=latest.get("profile_fingerprint"))


def _review_sha_binding(request_file: Path | None, round_id: str, mode: str,
                        sidecars: list[dict]) -> tuple[str | None, str | None, str, str | None, str | None]:
    """Compatibility projection for callers that only consume the historical SHA tuple."""
    binding = _review_binding(request_file, round_id, mode, sidecars)
    return tuple(binding[key] for key in (
        "target_sha", "base_sha", "review_binding_provenance",
        "review_binding_reason", "review_binding_source"))


def _round_review_sidecars(rdir: Path) -> dict[str, list[dict]]:
    import review

    rows: dict[str, list[dict]] = {}
    if not rdir.is_dir():
        return rows
    for path in sorted(rdir.glob("*-request.binding*.json")):
        data = _load_record_mapping(path, "json")
        if (data is None or data.get("schema") != review.ROUND_REQUEST_BINDING_SCHEMA
                or not isinstance(data.get("round_id"), str)
                or not review._is_sha(data.get("target_sha"))
                or (data.get("base_sha") is not None and not review._is_sha(data.get("base_sha")))
                or data.get("mode") != "packet" or data.get("canonical_store") != "local-packet"
                or not isinstance(data.get("at"), str)):
            continue
        rows.setdefault(data["round_id"], []).append({**data, "_file": str(path)})
    for path in sorted(rdir.glob("*-freeze-*.binding*.json")):
        data = _load_record_mapping(path, "json")
        if (data is None or data.get("schema") != review.PR_FREEZE_BINDING_SCHEMA
                or not isinstance(data.get("round_id"), str)
                or type(data.get("pr")) is not int or data["pr"] < 1
                or not review._is_cycle(data.get("cycle"))
                or not review._is_sha(data.get("target_sha"))
                or not review._is_sha(data.get("base_sha"))
                or not review._is_strlist(data.get("reviewers"))
                or (data.get("profile_fingerprint") is not None
                    and not review._nonempty_str(data.get("profile_fingerprint")))
                or data.get("mode") != "pr"
                or data.get("canonical_store") != "local-freeze-evidence"
                or parse_iso_timestamp(data.get("at")) is None):
            continue
        rows.setdefault(data["round_id"], []).append({**data, "_file": str(path)})
    return rows


def _project_review_rows(name: str, root: Path, cfg: dict) -> list[dict]:
    rdir = root / cfg["reviews_dir"]
    request_files: dict[str, Path] = {}
    feedback_files: dict[str, Path] = {}
    if rdir.is_dir():
        for p in sorted(rdir.glob("*-request.md")):
            request_files[p.stem[: -len("-request")]] = p
        for p in sorted(rdir.glob("*-feedback.md")):
            feedback_files[p.stem[: -len("-feedback")]] = p
    request_sidecars = _round_review_sidecars(rdir)
    tasks_by_round = _finding_tasks_by_round(root)
    round_exposures, _exposures_skipped = _round_exposure_rows(root)
    latest_round_exposures = _latest_round_exposures(round_exposures)
    round_ids = sorted(
        set(request_files) | set(feedback_files) | set(tasks_by_round) | set(request_sidecars))

    rows: list[dict] = []
    for rid in round_ids:
        findings: list[dict] = []
        round_tasks = tasks_by_round.get(rid, [])
        tasks_by_id = {t["id"]: t for t in round_tasks if t.get("id")}
        referenced_task_ids: set[str] = set()
        fb = feedback_files.get(rid)
        if fb is not None:
            try:
                text = fb.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for f in _parse_triage(text):
                entry = {
                    "id": f["id"], "severity": f["severity"], "type": f["type"],
                    "status": f["status"],
                    "source": "triage",
                    "provenance": "explicit" if f["severity"] else "unknown",
                    "source_pointer": f"{fb}:{f['line']}",
                }
                if f.get("paths"):
                    entry.update({
                        "paths": f["paths"], "path_provenance": f["path_provenance"],
                        "path_rule": f["path_rule"],
                    })
                if f.get("evidence_pointers"):
                    entry["evidence_pointers"] = f["evidence_pointers"]
                # dedup: a triage row whose task-id cell names a task that also joins via origin is
                # ONE finding — keep the triage row (triage severity), annotate its task_id, and drop
                # the separate task finding so it is not double-counted
                tid = f["task_id"]
                entry["task_id"] = tid or None
                if tid and tid in tasks_by_id:
                    referenced_task_ids.add(tid)
                    task_finding = tasks_by_id[tid]
                    for key in ("review_origin", "fixing_rounds", "status_history", "reopen_count"):
                        entry[key] = task_finding[key]
                findings.append(entry)
        # finding-derived tasks not referenced by any triage row remain as source "task"
        findings.extend(t for t in round_tasks if t.get("id") not in referenced_task_ids)
        findings.sort(key=lambda x: (x["source"], x["id"] or ""))
        counts = {"blocker": 0, "major": 0, "minor": 0, "unknown": 0}
        for f in findings:
            counts[f["severity"] if f["severity"] in SEVERITIES else "unknown"] += 1
        req = request_files.get(rid)
        mode = (cfg.get("review") or {}).get("mode", "packet")
        review_binding = _review_binding(req, rid, mode, request_sidecars.get(rid, []))
        session_id, session_provenance, session_reason = _round_session_binding(
            rid, latest_round_exposures)
        rows.append({
            "project": name,
            "root": str(root),
            "round_id": rid,
            "round_at": (latest_round_exposures.get(rid) or {}).get("at"),
            "request_file": str(req) if req else None,
            "feedback_file": str(fb) if fb else None,
            **review_binding,
            "session_id": session_id,
            "round_session_provenance": session_provenance,
            "round_session_reason": session_reason,
            "routes": (latest_round_exposures.get(rid) or {}).get("routes") or [],
            "findings": findings,
            "counts": counts,
        })
    return rows


def _registry_path() -> Path:
    """Runtime-resolved global registry (honours HOME so tests can override it)."""
    return registry_path()


def _project_entry(root: Path, source: Path | None = None) -> dict:
    root = resolve_project_paths(root, source)[0]
    try:
        name = load_config(root).get("project")
    except (OSError, yaml.YAMLError, ValueError) as e:
        raise WorkflowError(f"project config is unreadable: {root} ({type(e).__name__})") from e
    if not isinstance(name, str) or not name.strip():
        raise WorkflowError(f"project config has no non-empty project name: {root}")
    return {"name": name, "path": str(root)}


def run_reviews(registry_path: Path, out_dir: Path, project_root: Path | None = None) -> dict:
    entries: list = []
    generated_from = str(registry_path)
    if project_root is not None:
        entries = [_project_entry(project_root, registry_path)]
        generated_from = str(project_root.resolve())
    elif registry_path.is_file():
        # a MISSING registry is a fresh install (0 projects, exit 0); an EXISTING but corrupt one is
        # a degraded state that must fail loud (§3.8), not masquerade as "no registered projects"
        try:
            reg = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise WorkflowError(
                f"registry unreadable/unparseable: {registry_path} ({type(e).__name__})")
        if not isinstance(reg, dict):
            raise WorkflowError(
                f"registry has wrong shape (expected a JSON object): {registry_path}")
        projects = reg.get("projects", [])
        if not isinstance(projects, list):
            raise WorkflowError(
                f"registry has wrong shape (`projects` must be a list): {registry_path}")
        entries = [e for e in projects if isinstance(e, dict)]

    rows: list[dict] = []
    scanned: list[str] = []
    skipped: list[dict] = []
    round_exposures_skipped = 0
    for entry in entries:
        name = entry.get("name") or "(unnamed)"
        path = entry.get("path")
        if not path:  # remote-only registry entry — no local tree to read review artifacts from
            skipped.append({"project": name, "reason": "no local path (remote-only registry entry)"})
            continue
        root = resolve_project_paths(Path(path).expanduser(), registry_path)[0]
        if not has_project_config(root):
            skipped.append({"project": name, "reason": "project root or .waystone.yml inaccessible"})
            continue
        try:
            cfg = load_config(root)
        except (OSError, yaml.YAMLError, ValueError) as e:
            skipped.append({"project": name, "reason": f"config unreadable: {type(e).__name__}"})
            continue
        round_exposures_skipped += _round_exposure_rows(root)[1]
        rows.extend(_project_review_rows(name, root, cfg))
        scanned.append(name)

    rows.sort(key=lambda r: (r["project"], r["round_id"]))
    coverage = {
        "generated_from": generated_from,
        "projects_total": len(entries),
        "projects_scanned": sorted(scanned),
        "projects_skipped": sorted(skipped, key=lambda s: s["project"]),
        "review_sha_bindings_unknown": sum(
            1 for row in rows if row.get("review_binding_provenance") == "unknown"),
        "round_session_bindings_unknown": sum(
            1 for row in rows if row.get("round_session_provenance") == "unknown"),
        "review_binding_unknown_reasons": dict(sorted(Counter(
            row.get("review_binding_reason") for row in rows
            if row.get("review_binding_provenance") == "unknown").items())),
        "round_session_unknown_reasons": dict(sorted(Counter(
            row.get("round_session_reason") for row in rows
            if row.get("round_session_provenance") == "unknown").items())),
        "round_exposures_skipped": round_exposures_skipped,
        "row_totals": {"reviews": len(rows), "findings": sum(len(r["findings"]) for r in rows)},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reviews.jsonl").write_text(
        "".join(_dumps(r) + "\n" for r in rows), encoding="utf-8")
    (out_dir / "reviews_coverage.json").write_text(_dumps(coverage) + "\n", encoding="utf-8")
    return coverage


# ================================================================= evidence (§8)
def _registry_entries(registry_path: Path) -> list[dict]:
    if not registry_path.is_file():
        return []
    try:
        reg = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(
            f"registry unreadable/unparseable: {registry_path} ({type(e).__name__})")
    if not isinstance(reg, dict) or not isinstance(reg.get("projects", []), list):
        raise WorkflowError(f"registry has wrong shape: {registry_path}")
    return [e for e in reg.get("projects", []) if isinstance(e, dict)]


def _load_record_mapping(path: Path, kind: str) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if kind == "yaml" else json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _latest_verdict_judgment(record: Path) -> tuple[dict | None, int]:
    import delegate

    try:
        latest = delegate.latest_canonical_verdict(record)
    except WorkflowError:
        return None, 1
    if latest is None:
        return None, 0
    _path, verdict = latest
    return {
        "event": "delegation-verdict", "judged_at": verdict["judged_at"],
        "decision": verdict["decision"],
        "decided_by": verdict["decided_by"], "provenance": "explicit",
    }, 0


def _delegation_acceptance(status: dict, judgment: dict | None) -> dict | None:
    accepted_at = status.get("accepted_at")
    if (status.get("state") != "applied" or judgment is None
            or judgment.get("decision") != "apply"
            or parse_iso_timestamp(accepted_at) is None):
        return None
    return {
        "event": "delegation-apply", "accepted_at": accepted_at,
        "judged_at": judgment["judged_at"], "decision": "apply", "resolved": True,
        "decided_by": judgment["decided_by"], "provenance": "explicit",
    }


def _latest_verdict_acceptance(record: Path) -> tuple[dict | None, int]:
    """Compatibility helper: only an applied transition can project acceptance."""
    judgment, skipped = _latest_verdict_judgment(record)
    if skipped:
        return None, skipped
    status = _load_record_mapping(Path(record) / "status.json", "json")
    return (_delegation_acceptance(status or {}, judgment), 0)


def _verification_run_projection(record: Path) -> tuple[list[dict], int]:
    import delegate

    try:
        artifacts = delegate._verify_artifacts(record)
    except WorkflowError:
        return [], 1
    runs = []
    for number, artifact in sorted(artifacts.items()):
        findings = artifact["payload"]["findings"]
        judgments = sorted({_dumps(finding) for finding in findings})
        digest = hashlib.sha256("\n".join(judgments).encode("utf-8")).hexdigest()
        runs.append({
            "number": number, "judgment_set_hash": f"sha256:{digest}",
            "findings": len(judgments),
        })
    return runs, 0


def _project_delegation_rows(root: Path) -> tuple[list[dict], int, int, int]:
    """Project task-linked delegation facts parsed directly from immutable/local record files."""
    rows: list[dict] = []
    skipped = 0
    verdicts_invalid = 0
    verification_artifacts_invalid = 0
    directory = project_state_path(root) / "delegations"
    if not directory.is_dir():
        return rows, skipped, verdicts_invalid, verification_artifacts_invalid
    for record in sorted(directory.iterdir()):
        if not record.is_dir() or not ((record / "claim.json").exists()
                                      or (record / "exposure.json").exists()):
            continue
        exposure = _load_record_mapping(record / "exposure.json", "json")
        status = _load_record_mapping(record / "status.json", "json")
        if (exposure is None or status is None or not isinstance(exposure.get("task_id"), str)):
            skipped += 1
            continue
        contract_path = record / "artifact" / "contract.yaml"
        contract = _load_record_mapping(contract_path, "yaml") if contract_path.is_file() else None
        if contract_path.is_file() and contract is None:
            skipped += 1
            continue
        report = (contract or {}).get("delegate_report") or {}
        packet = _load_record_mapping(record / "packet.yaml", "yaml")
        raw_routing_note = packet.get("routing_note") if packet is not None else None
        routing_note = None
        if (isinstance(raw_routing_note, dict)
                and raw_routing_note.get("provenance") == "main-session"
                and isinstance(raw_routing_note.get("note"), str)
                and raw_routing_note["note"] and "\n" not in raw_routing_note["note"]
                and "\r" not in raw_routing_note["note"]):
            routing_note = {
                "provenance": "main-session", "note": raw_routing_note["note"],
            }
        judgment, verdict_skipped = _latest_verdict_judgment(record)
        verdicts_invalid += verdict_skipped
        verification_runs, verify_skipped = _verification_run_projection(record)
        verification_artifacts_invalid += verify_skipped
        row = {
            "task_id": exposure["task_id"], "did": record.name, "state": status.get("state"),
            "verification_present": (
                report.get("present") is True and bool(report.get("verification"))),
            "scope_drift": delegation_scope_drift(record),
            "env": status.get("env") or (contract or {}).get("env"),
            "overlays_active": exposure.get("overlays") if isinstance(exposure.get("overlays"), list) else [],
            "binding": exposure.get("binding") if isinstance(exposure.get("binding"), dict) else None,
            "start_level": exposure.get("start_level"),
        }
        if routing_note is not None:
            row["routing_note"] = routing_note
        if verification_runs:
            row["verification_runs"] = verification_runs
        if judgment is not None:
            judgment["resolved"] = (
                judgment["decision"] == "discard"
                or (status.get("state") == "applied" and parse_iso_timestamp(
                    status.get("accepted_at")) is not None))
            row["judgment"] = judgment
        acceptance = _delegation_acceptance(status, judgment)
        if acceptance is not None:
            row["acceptance"] = acceptance
        rows.append(row)
    rows.sort(key=lambda row: row["did"])
    return rows, skipped, verdicts_invalid, verification_artifacts_invalid


def _project_task_rows(root: Path) -> list[dict]:
    documents = [load_tasks(root)]
    archive = root / "tasks.archive.yaml"
    if archive.is_file():
        archived = load_yaml(archive)
        if not isinstance(archived, dict):
            raise ValueError("tasks.archive.yaml is not an object")
        documents.append(archived)
    tasks: dict[str, dict] = {}
    for document in documents:
        for task in document.get("tasks", []) or []:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                tasks.setdefault(task["id"], task)
    return [tasks[task_id] for task_id in sorted(tasks)]


def _load_warning_rows(root: Path) -> tuple[list[dict], int, bool]:
    path = project_state_path(root) / "overlay" / "warnings.jsonl"
    if not path.is_file():
        return [], 0, False
    rows: list[dict] = []
    skipped = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 1, True
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if (not isinstance(row, dict) or not all(isinstance(row.get(field), str)
                for field in ("at", "boundary", "rule", "event"))):
            skipped += 1
            continue
        if parse_iso_timestamp(row["at"]) is None:
            skipped += 1
            continue
        identity = row.get("policy_identity")
        if (not isinstance(identity, dict) or set(identity) != {"layer", "id"}
                or not all(isinstance(identity.get(field), str) and identity[field]
                           for field in ("layer", "id"))):
            identity = None
        rows.append({
            "at": row["at"], "boundary": row["boundary"], "rule": row["rule"],
            "event": row["event"], "policy_identity": identity,
            "origin_delta_id": row.get("origin_delta_id"),
            "params_fingerprint": row.get("params_fingerprint"),
            "policy_source_kind": row.get("policy_source_kind"),
            "delta_status": row.get("delta_status"),
            "start_level": row.get("start_level"),
            "suppressed_by_start_level": row.get("suppressed_by_start_level"),
            "context": row.get("context") if isinstance(row.get("context"), dict) else {},
            "source_pointer": f"{path}:{line_number}",
        })
    rows.sort(key=lambda row: (
        row["at"], row["boundary"], row["rule"], row["event"],
        str(row.get("policy_identity"))))
    return rows, skipped, True


def _warning_observation(name: str, warnings: list[dict], warnings_skipped: int,
                         warnings_present: bool, exposures: list[dict],
                         exposures_skipped: int) -> dict:
    by_rule: dict[str, Counter] = {}
    by_boundary: dict[str, Counter] = {}
    by_rule_boundary: dict[tuple[str, str], Counter] = {}
    counted = [row for row in warnings if row["event"] in ("fire", "conflict")]
    for row in counted:
        by_rule.setdefault(row["rule"], Counter())[row["event"]] += 1
        by_boundary.setdefault(row["boundary"], Counter())[row["event"]] += 1
        by_rule_boundary.setdefault((row["rule"], row["boundary"]), Counter())[row["event"]] += 1

    rounds: list[dict] = []
    chronological = sorted(_latest_round_exposures(exposures).values(), key=lambda row: (
        row["at"], row["round_id"], row.get("_file") or ""))
    counted = sorted(counted, key=lambda row: (
        row["at"], row["boundary"], row["rule"], row["event"],
        str(row.get("policy_identity"))))
    warning_index = 0
    previous_index = 0
    for exposure in chronological:
        while warning_index < len(counted) and counted[warning_index]["at"] <= exposure["at"]:
            warning_index += 1
        selected = counted[previous_index:warning_index]
        rounds.append({
            "round_id": exposure["round_id"], "closed_at": exposure["at"],
            "fire": sum(row["event"] == "fire" for row in selected),
            "conflict": sum(row["event"] == "conflict" for row in selected),
        })
        previous_index = warning_index
    outside = len(counted) - warning_index
    normalize = lambda groups: {key: {event: groups[key].get(event, 0)
                                      for event in ("conflict", "fire")}
                                for key in sorted(groups)}
    cross: dict[str, dict] = {}
    for (rule, boundary), counts in sorted(by_rule_boundary.items()):
        cross.setdefault(rule, {})[boundary] = {
            event: counts.get(event, 0) for event in ("conflict", "fire")}
    return {
        "project": name, "records": len(warnings),
        "fire": sum(row["event"] == "fire" for row in counted),
        "conflict": sum(row["event"] == "conflict" for row in counted),
        "by_rule": normalize(by_rule), "by_boundary": normalize(by_boundary),
        "by_rule_boundary": cross,
        "recent_rounds": rounds[-5:],
        "coverage": {
            "warnings_file_present": warnings_present, "warning_rows_skipped": warnings_skipped,
            "round_exposures_skipped": exposures_skipped,
            "warnings_outside_closed_rounds": outside,
            "round_intervals_unavailable": not bool(exposures),
        },
    }


def _load_decisions(root: Path) -> tuple[list[dict], int]:
    path = project_state_path(root) / "improve" / "decisions.jsonl"
    if not path.is_file():
        return [], 0
    rows: list[dict] = []
    skipped = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 1
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if (not isinstance(row, dict) or not isinstance(row.get("rec_id"), str)
                or row.get("decision") not in ("accept", "reject")
                or parse_iso_timestamp(row.get("at")) is None):
            skipped += 1
            continue
        rows.append({**row, "source_pointer": f"{path}:{line_number}"})
    rows.sort(key=lambda row: (
        parse_iso_timestamp(row["at"]), row["rec_id"], row["source_pointer"]))
    return rows, skipped


def _load_overlay_deltas(root: Path) -> tuple[list[dict], int]:
    import overlay

    rows = []
    skipped = 0
    for delta in overlay.list_deltas(root):
        if delta.get("corrupt"):
            skipped += 1
            continue
        if (not isinstance(delta.get("id"), str) or not isinstance(delta.get("rule"), str)
                or parse_iso_timestamp(delta.get("created_at")) is None):
            skipped += 1
            continue
        rows.append({
            **delta, "identity": {"layer": "project", "id": delta["id"]},
            "origin_delta_id": delta.get("origin_delta_id") or delta["id"],
            "source_pointer": str(overlay._delta_path(root, delta["id"])),
        })
    rows.sort(key=lambda row: row["id"])
    return rows, skipped


def _review_type_timeline(reviews: list[dict]) -> tuple[dict[str, dict[str, int]], dict[str, set[str]]]:
    by_round: dict[str, dict[str, int]] = {}
    type_rounds: dict[str, set[str]] = {}
    for review in sorted(reviews, key=lambda row: str(row.get("round_id"))):
        round_id = review.get("round_id")
        if not isinstance(round_id, str):
            continue
        counts: Counter = Counter()
        for finding in review.get("findings") or []:
            finding_type = finding.get("type")
            if finding.get("status") != "REAL" or finding_type not in FINDING_TYPES:
                continue
            counts[finding_type] += 1
            type_rounds.setdefault(finding_type, set()).add(round_id)
        by_round[round_id] = dict(counts)
    return by_round, type_rounds


def _trend_fact(rows: list[dict], round_findings: dict[str, dict[str, int]],
                recurrence_rounds: dict[str, set[str]], related_types: list[str]) -> dict:
    opportunities = sum(row.get("opportunities", 0) for row in rows)
    fires = sum(row.get("fires", 0) for row in rows)
    round_ids = sorted({row["round_id"] for row in rows if isinstance(row.get("round_id"), str)})
    finding_occurrences = {
        finding_type: sum(round_findings.get(round_id, {}).get(finding_type, 0)
                          for round_id in round_ids)
        for finding_type in related_types
    }
    finding_recurrences = {}
    for finding_type in related_types:
        ordered = sorted(recurrence_rounds.get(finding_type, set()))
        recurrence_ids = set(ordered[1:])
        finding_recurrences[finding_type] = sum(round_id in recurrence_ids for round_id in round_ids)
    return {
        "rounds": len(set(round_ids)), "opportunities": opportunities, "fires": fires,
        "fire_rate": round(fires / opportunities, 4) if opportunities else None,
        "finding_occurrences": finding_occurrences,
        "finding_recurrences": finding_recurrences,
    }


def _recent_round_trend(phase_rows: dict[str, list[dict]],
                        round_findings: dict[str, dict[str, int]],
                        recurrence_rounds: dict[str, set[str]], related_types: list[str]) -> list[dict]:
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for phase, rows in phase_rows.items():
        for row in rows:
            round_id = row.get("round_id")
            if not isinstance(round_id, str):
                continue
            aggregate = grouped.setdefault((phase, round_id), {"opportunities": 0, "fires": 0})
            aggregate["opportunities"] += row.get("opportunities", 0)
            aggregate["fires"] += row.get("fires", 0)
    trend = []
    for (phase, round_id), counts in sorted(grouped.items(), key=lambda item: item[0][1]):
        facts = _trend_fact(
            [{"round_id": round_id, **counts}], round_findings, recurrence_rounds, related_types)
        trend.append({"phase": phase, "round_id": round_id, **facts})
    return trend[-5:]


def _staleness_change_events(root: Path, latest_exposures: list[dict]) \
        -> tuple[list[tuple[str, str]], dict]:
    """Observed fingerprint transitions plus a latest-exposure/current-state mismatch cutoff."""
    fields = (
        ("config_fingerprint", "config-fingerprint-changed",
         Path(root) / ".waystone.yml", "current-config-fingerprint-mismatch"),
        ("committed_policy_fingerprint", "committed-policy-fingerprint-changed",
         Path(root) / "docs" / "waystone-policy.yaml",
         "current-committed-policy-fingerprint-mismatch"),
        ("routing_policy_fingerprint", "routing-policy-fingerprint-changed",
         Path(__file__).resolve().parent.parent / "templates" / "routing-policy.yaml",
         "current-routing-policy-fingerprint-mismatch"),
    )
    events: list[tuple[str, str]] = []
    coverage: dict[str, object] = {}
    for field, transition_reason, path, mismatch_reason in fields:
        sentinel = object()
        previous = sentinel
        for exposure in latest_exposures:
            if field not in exposure:
                continue
            current = exposure.get(field)
            if previous is not sentinel and previous != current:
                events.append((exposure["at"], transition_reason))
            previous = current
        coverage[f"{field}_snapshots"] = sum(field in row for row in latest_exposures)
        try:
            current_fingerprint = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() else None)
            current_known = True
        except OSError:
            current_fingerprint = None
            current_known = False
        coverage[f"current_{field}_known"] = current_known
        if (latest_exposures and current_known and field in latest_exposures[-1]
                and latest_exposures[-1].get(field) != current_fingerprint):
            events.append((latest_exposures[-1]["at"], mismatch_reason))
    return events, coverage


def _adaptive_feedback_observation(name: str, root: Path, reviews: list[dict],
                                   warnings: list[dict], exposures: list[dict],
                                   warning_context_unknown: dict[str, int]) -> dict:
    import delegate
    import overlay

    deltas, deltas_skipped = _load_overlay_deltas(root)
    decisions, decisions_skipped = _load_decisions(root)
    latest_decisions: dict[str, dict] = {}
    for decision in decisions:
        latest_decisions[decision["rec_id"]] = decision
    accepted = {rec_id: row for rec_id, row in latest_decisions.items()
                if row["decision"] == "accept"}
    rejected = {rec_id: row for rec_id, row in latest_decisions.items()
                if row["decision"] == "reject"}
    deltas_by_rec: dict[str, list[dict]] = {}
    for delta in deltas:
        evidence = delta.get("evidence") if isinstance(delta.get("evidence"), dict) else {}
        rec_id = evidence.get("rec_id") if evidence.get("source") == "improve-rec" else None
        if isinstance(rec_id, str):
            deltas_by_rec.setdefault(rec_id, []).append(delta)
    by_rec: dict[str, dict] = {}
    join_conflicts: Counter = Counter()
    quarantined: set[str] = set()
    for rec_id, rec_deltas in deltas_by_rec.items():
        decision = latest_decisions.get(rec_id)
        if len(rec_deltas) != 1:
            join_conflicts["duplicate-rec-delta"] += len(rec_deltas)
            quarantined.update(delta["id"] for delta in rec_deltas)
            continue
        delta = rec_deltas[0]
        if decision is None:
            join_conflicts["missing-rec-decision"] += 1
            quarantined.add(delta["id"])
            continue
        accepted_at = parse_iso_timestamp(decision["at"])
        created_at = parse_iso_timestamp(delta["created_at"])
        if accepted_at is None or created_at is None or accepted_at > created_at:
            join_conflicts["accept-after-delta"] += 1
            quarantined.add(delta["id"])
            continue
        if decision["decision"] != "accept":
            join_conflicts["delta-from-rejected-rec"] += 1
            quarantined.add(delta["id"])
            continue
        by_rec[rec_id] = delta

    latest_exposures = sorted(_latest_round_exposures(exposures).values(), key=lambda row: (
        row["at"], row["round_id"], row.get("_file") or ""))
    exposure_by_round = {row["round_id"]: row for row in latest_exposures}
    round_findings, recurrence_rounds = _review_type_timeline(reviews)
    evaluation_rows = [row for row in warnings if row.get("event") == "evaluation"]
    conflict_rows = [row for row in warnings if row.get("event") == "conflict"]

    delta_facts: list[dict] = []
    delta_examples: list[dict] = []
    for delta in deltas:
        if delta["id"] in quarantined:
            continue
        rule = overlay.RULES.get(delta["rule"]) or {}
        related_types = list(rule.get("finding_types") or [])
        replay = delta.get("replay") if isinstance(delta.get("replay"), dict) else {}
        transitions = delta.get("transitions") if isinstance(delta.get("transitions"), list) else []
        active_at = next((entry.get("at") for entry in transitions
                          if isinstance(entry, dict) and entry.get("to") == "observing"
                          and parse_iso_timestamp(entry.get("at")) is not None), delta["created_at"])
        suspended_at = next((entry.get("at") for entry in transitions
                             if isinstance(entry, dict) and entry.get("to") in ("suspended", "retired")
                             and parse_iso_timestamp(entry.get("at")) is not None), None)

        def phase(at: str) -> str:
            timestamp = parse_iso_timestamp(at)
            start = parse_iso_timestamp(active_at)
            stop = parse_iso_timestamp(suspended_at)
            if timestamp is None or start is None:
                return "unknown"
            if timestamp < start:
                return "pre-active"
            if stop is not None and timestamp >= stop:
                return "post-suspend"
            return "active"

        units: dict[tuple[str, str, str], tuple[str, dict]] = {}
        unassigned_replay_rounds = 0
        replay_rows = replay.get("evaluations") or replay.get("by_round") or []
        for row in replay_rows:
            if not isinstance(row, dict) or not isinstance(row.get("round_id"), str):
                continue
            if type(row.get("opportunities")) is not int or row["opportunities"] < 1:
                continue
            exposure = exposure_by_round.get(row["round_id"])
            if exposure is None:
                unassigned_replay_rounds += 1
                continue
            current_phase = phase(exposure["at"])
            if current_phase == "unknown":
                unassigned_replay_rounds += 1
                continue
            subject = row.get("subject_id") or row["round_id"]
            snapshot = row.get("snapshot") or row["round_id"]
            key = (delta["identity"]["layer"], delta["identity"]["id"],
                   str(subject), str(snapshot))
            units[key] = (current_phase, row)
        unassigned_evaluations = 0
        for warning in evaluation_rows:
            if warning.get("policy_identity") != delta["identity"]:
                continue
            context = warning.get("context") or {}
            round_id = context.get("round_id")
            if not isinstance(round_id, str):
                following = [exposure for exposure in latest_exposures
                             if exposure["at"] >= warning["at"]]
                round_id = following[0]["round_id"] if following else None
            if not isinstance(round_id, str):
                unassigned_evaluations += 1
                continue
            current_phase = phase(warning["at"])
            if current_phase == "unknown":
                unassigned_evaluations += 1
                continue
            subject = (context.get("delegation_id") or round_id or context.get("task_id")
                       or ",".join(context.get("task_ids") or []))
            snapshot = context.get("snapshot") or round_id or subject
            key = (delta["identity"]["layer"], delta["identity"]["id"],
                   str(subject), str(snapshot))
            units[key] = (current_phase, {"round_id": round_id, "opportunities": 1,
                                          "fires": int(context.get("fired") is True)})
        phase_rows = {"pre-active": [], "active": [], "post-suspend": []}
        for current_phase, row in units.values():
            phase_rows[current_phase].append(row)
        pre_active = _trend_fact(
            phase_rows["pre-active"], round_findings, recurrence_rounds, related_types)
        active_phase = _trend_fact(
            phase_rows["active"], round_findings, recurrence_rounds, related_types)
        post_suspend = _trend_fact(
            phase_rows["post-suspend"], round_findings, recurrence_rounds, related_types)
        evidence = delta.get("evidence") if isinstance(delta.get("evidence"), dict) else {}
        rec_id = evidence.get("rec_id") if evidence.get("source") == "improve-rec" else None
        accepted_row = accepted.get(rec_id) if isinstance(rec_id, str) else None
        fact = {
            "identity": delta["identity"], "origin_delta_id": delta["origin_delta_id"],
            "rule": delta["rule"], "status": delta.get("status"),
            "provenance": "observed",
            "related_finding_types": related_types,
            "pre_active": pre_active, "active": active_phase, "post_suspend": post_suspend,
            # Compatibility aliases retain the prior public names with corrected phase semantics.
            "before": pre_active, "after": active_phase,
            "recent_round_trend": _recent_round_trend(
                phase_rows, round_findings, recurrence_rounds, related_types),
            "accepted_rec_id": rec_id if accepted_row is not None else None,
            "decision_follow_through": accepted_row is not None,
            "unassigned_evaluations": unassigned_evaluations,
            "unassigned_replay_rounds": unassigned_replay_rounds,
        }
        delta_facts.append(fact)
        delta_examples.append({"project": name, "identity": delta["identity"],
                               "origin_delta_id": delta["origin_delta_id"],
                               "pointer": delta["source_pointer"]})

    # An environment snapshot change makes only earlier evidence stale. Current-state mismatches are
    # bounded by the latest immutable exposure: deltas newer than that point remain unknown, not stale.
    change_events, setting_coverage = _staleness_change_events(root, latest_exposures)
    for field, reason in (("profile_fingerprint", "profile-fingerprint-changed"),
                          ("env_prep", "delegation-env-prep-changed")):
        sentinel = object()
        previous = sentinel
        for exposure in latest_exposures:
            if field not in exposure:
                continue
            current = exposure.get(field)
            if previous is not sentinel and previous != current:
                change_events.append((exposure["at"], reason))
            previous = current
    current_profile = None
    current_profile_known = True
    try:
        if delegate._profile_path(root).is_file():
            _profile, current_profile = delegate._load_profile(root)
    except WorkflowError:
        current_profile_known = False
    cfg = load_config(root)
    current_env = (cfg.get("delegation") or {}).get("env_prep")
    if latest_exposures:
        latest = latest_exposures[-1]
        if (current_profile_known and "profile_fingerprint" in latest
                and latest.get("profile_fingerprint") != current_profile):
            change_events.append((latest["at"], "current-profile-fingerprint-mismatch"))
        if "env_prep" in latest and latest.get("env_prep") != current_env:
            change_events.append((latest["at"], "current-delegation-env-prep-mismatch"))
    active = [delta for delta in deltas if delta.get("status") in ("observing", "warning")
              and delta["id"] not in quarantined]
    stale_rows = []
    for delta in active:
        reasons = sorted({reason for at, reason in change_events if delta["created_at"] < at})
        if reasons:
            stale_rows.append({"project": name, "identity": delta["identity"],
                               "origin_delta_id": delta["origin_delta_id"], "reasons": reasons,
                               "pointer": delta["source_pointer"], "provenance": "observed"})

    materialized = sum(rec_id in by_rec for rec_id in accepted)
    per_project = {
        "deltas": delta_facts,
        "decision_follow_through": {
            "accepted": len(accepted), "materialized": materialized,
            "accepted_without_delta": len(accepted) - materialized,
            "rejected_decisions": len(rejected),
        },
        "same_scope_conflicts": len(conflict_rows),
        "same_scope_conflicts_by_rule": dict(sorted(Counter(
            row["rule"] for row in conflict_rows).items())),
        "re_review_candidates": len(stale_rows),
        "coverage": {
            "delta_rows_skipped": deltas_skipped, "decision_rows_skipped": decisions_skipped,
            "round_exposures": len(latest_exposures),
            "warning_evaluations": len(evaluation_rows),
            "staleness_basis": "delta-created-before-observed-setting-change",
            "current_mismatch_cutoff": "latest-round-exposure",
            "current_profile_known": current_profile_known,
            "profile_snapshots": sum("profile_fingerprint" in row for row in latest_exposures),
            "env_prep_snapshots": sum("env_prep" in row for row in latest_exposures),
            **setting_coverage,
            "accept_delta_conflicts": dict(sorted(join_conflicts.items())),
            "warning_context_unknown": dict(sorted(warning_context_unknown.items())),
        },
    }
    examples: list[dict] = []
    for rec_id, decision in sorted(accepted.items()):
        delta = by_rec.get(rec_id)
        if delta is not None:
            examples.append({"project": name, "rec_id": rec_id, "identity": delta["identity"],
                             "origin_delta_id": delta["origin_delta_id"],
                             "pointer": decision["source_pointer"]})
    examples.extend({"project": name, "rule": row["rule"],
                     "pointer": row["source_pointer"]} for row in conflict_rows)
    examples.extend(stale_rows)
    examples.extend(delta_examples)
    return {"project": name, "facts": per_project, "examples": examples[:5],
            "stale_candidates": stale_rows}


_WARNING_CONTEXT_BOUNDARIES = {
    "delegation-verification-evidence-v1": {"delegate-run", "delegate-apply", "check"},
    "round-close-open-findings-v1": {"round-close", "review-ingest", "check"},
    "delegation-scope-drift-v1": {"delegate-run", "delegate-apply", "check"},
    "env-manifest-mutation-v1": {"round-close", "check"},
    "review-skipped-closes-v1": {"round-close", "check"},
    "done-without-evidence-v1": {"round-close", "check"},
}

_WARNING_CONTEXT_FIELDS = {
    "delegation-verification-evidence-v1": {
        "delegation_id", "task_id", "task_ids", "round_id", "policy_identities", "resolution",
        "snapshot", "evaluable", "coverage_reason", "fired"},
    "round-close-open-findings-v1": {
        "task_id", "task_ids", "delegation_id", "round_id", "unlinked", "policy_identities",
        "resolution", "snapshot", "evaluable", "coverage_reason", "fired"},
    "delegation-scope-drift-v1": {
        "delegation_id", "task_id", "task_ids", "round_id", "policy_identities", "outside_scope",
        "coverage_reason", "resolution", "snapshot", "evaluable", "fired"},
    "env-manifest-mutation-v1": {
        "round_id", "task_id", "task_ids", "policy_identities", "manifest_paths", "resolution",
        "snapshot", "evaluable", "coverage_reason", "fired"},
    "review-skipped-closes-v1": {
        "round_id", "task_id", "task_ids", "policy_identities", "consecutive", "resolution",
        "snapshot", "evaluable", "coverage_reason", "fired"},
    "done-without-evidence-v1": {
        "round_id", "task_id", "task_ids", "policy_identities", "resolution", "snapshot",
        "evaluable", "coverage_reason", "fired"},
}


def _warning_task_ids(warning: dict, did_to_task: dict[str, str]) -> tuple[set[str], str | None]:
    context = warning.get("context") or {}
    rule = warning.get("rule")
    boundary = warning.get("boundary")
    if not isinstance(context, dict):
        return set(), "invalid-context-schema"
    if rule in _WARNING_CONTEXT_FIELDS:
        if boundary not in _WARNING_CONTEXT_BOUNDARIES[rule]:
            return set(), "invalid-context-schema"
        allowed = _WARNING_CONTEXT_FIELDS[rule]
    else:
        # Unknown-rule evaluation errors are themselves useful friction evidence. Validate their
        # generic attribution fields without pretending the unknown rule has a known boundary schema.
        allowed = {
            "delegation_id", "task_id", "task_ids", "round_id", "policy_identities", "resolution",
            "snapshot", "evaluable", "coverage_reason", "fired",
        }
    if set(context) - allowed:
        return set(), "invalid-context-schema"
    if "task_id" in context and not isinstance(context["task_id"], str):
        return set(), "invalid-context-schema"
    if ("task_ids" in context and (not isinstance(context["task_ids"], list)
            or any(not isinstance(task, str) for task in context["task_ids"]))):
        return set(), "invalid-context-schema"
    if "delegation_id" in context and not isinstance(context["delegation_id"], str):
        return set(), "invalid-context-schema"
    if "round_id" in context and context["round_id"] is not None and not isinstance(context["round_id"], str):
        return set(), "invalid-context-schema"
    if ("policy_identities" in context and (not isinstance(context["policy_identities"], list)
            or any(not isinstance(identity, dict) or set(identity) != {"layer", "id"}
                   or not all(isinstance(identity.get(field), str)
                              for field in ("layer", "id"))
                   for identity in context["policy_identities"]))):
        return set(), "invalid-context-schema"
    if "unlinked" in context and (type(context["unlinked"]) is not int or context["unlinked"] < 0):
        return set(), "invalid-context-schema"
    if "fired" in context and type(context["fired"]) is not bool:
        return set(), "invalid-context-schema"
    if "evaluable" in context and type(context["evaluable"]) is not bool:
        return set(), "invalid-context-schema"
    if ("outside_scope" in context and (not isinstance(context["outside_scope"], list)
            or any(not isinstance(path, str) for path in context["outside_scope"]))):
        return set(), "invalid-context-schema"
    if ("manifest_paths" in context and (not isinstance(context["manifest_paths"], list)
            or any(not isinstance(path, str) for path in context["manifest_paths"]))):
        return set(), "invalid-context-schema"
    if ("coverage_reason" in context and context["coverage_reason"] is not None
            and not isinstance(context["coverage_reason"], str)):
        return set(), "invalid-context-schema"
    if "resolution" in context and not isinstance(context["resolution"], str):
        return set(), "invalid-context-schema"
    if "snapshot" in context and not isinstance(context["snapshot"], str):
        return set(), "invalid-context-schema"
    if "consecutive" in context and (type(context["consecutive"]) is not int
                                      or context["consecutive"] < 1):
        return set(), "invalid-context-schema"

    direct: set[str] = set()
    if isinstance(context.get("task_id"), str):
        direct.add(context["task_id"])
    if isinstance(context.get("task_ids"), list):
        direct.update(context["task_ids"])
    did = context.get("delegation_id")
    delegated = {did_to_task[did]} if isinstance(did, str) and did in did_to_task else set()
    if direct and delegated and direct != delegated:
        return set(), "conflicting-context"
    task_ids = direct or delegated
    if isinstance(did, str) and not delegated and not direct:
        return set(), "unresolved-delegation-context"
    return task_ids, None


def _normalize_warning_rows(warnings: list[dict], did_to_task: dict[str, str]
                            ) -> tuple[list[dict], dict[str, int]]:
    """Validate/attribute warning context once; every downstream consumer gets this projection."""
    normalized: list[dict] = []
    coverage: Counter = Counter()
    for warning in warnings:
        task_ids, reason = _warning_task_ids(warning, did_to_task)
        if reason is not None:
            coverage[reason] += 1
            continue
        normalized.append({**warning, "task_ids": sorted(task_ids)})
    return normalized, dict(sorted(coverage.items()))


def _round_exposure_projection(task: dict, exposures: list[dict] | dict[str, dict]) -> dict | None:
    latest = exposures if isinstance(exposures, dict) else _latest_round_exposures(exposures)
    match = latest.get(task.get("round"))
    if match is None:
        return None
    return {key: match.get(key) for key in (
        "round_id", "at", "session_id", "head_sha", "start_level", "overlays_active",
        "bindings", "routes")}


def _task_acceptance(task: dict, delegations: list[dict],
                     exposures: list[dict] | dict[str, dict]) -> dict:
    applied = [dict(delegation["acceptance"], delegation_id=delegation["did"])
               for delegation in delegations
               if isinstance(delegation.get("acceptance"), dict)
               and delegation["acceptance"].get("event") == "delegation-apply"
               and delegation["acceptance"].get("resolved") is True
               and parse_iso_timestamp(delegation["acceptance"].get("accepted_at")) is not None]
    if applied:
        return max(applied, key=lambda row: (
            row["accepted_at"], row.get("delegation_id") or ""))
    latest = exposures if isinstance(exposures, dict) else _latest_round_exposures(exposures)
    exposure = latest.get(task.get("round"))
    if task.get("status") in ("done", "dropped") and exposure is not None:
        return {
            "event": "round-close", "accepted_at": exposure["at"], "resolved": True,
            "round_id": exposure["round_id"], "provenance": "explicit",
        }
    return {
        "event": None, "accepted_at": None,
        "resolved": task.get("status") in ("done", "dropped"),
        "provenance": "current-task-state-approximation",
    }


def run_evidence(registry_path: Path, out_dir: Path, projects: set[str],
                 project_root: Path | None = None) -> dict:
    """Project task-id projection joining review findings to delegation evidence. No timestamps:
    identical registry/files yield byte-identical evidence.jsonl (S10)."""
    generated_from = str(registry_path)
    if project_root is not None:
        entries = [_project_entry(project_root, registry_path)]
        generated_from = str(project_root.resolve())
    else:
        entries = _registry_entries(registry_path)
    if projects:
        entries = [e for e in entries if e.get("name") in projects]
    task_rows: list[dict] = []
    scanned: list[str] = []
    skipped: list[dict] = []
    unlinked = delegation_skipped = verdicts_invalid = 0
    warning_observations: list[dict] = []
    adaptive_observations: list[dict] = []
    warning_rows_skipped = round_exposures_skipped = 0
    task_session_unknown = acceptance_approximations = 0
    verification_artifacts_invalid = 0
    normalized_warnings: list[dict] = []
    warning_context_coverage: Counter = Counter()

    for entry in entries:
        name = entry.get("name") or "(unnamed)"
        path = entry.get("path")
        if not path:
            skipped.append({"project": name, "reason": "no local path (remote-only registry entry)"})
            continue
        root = resolve_project_paths(Path(path).expanduser(), registry_path)[0]
        if not has_project_config(root):
            skipped.append({"project": name, "reason": "project root or .waystone.yml inaccessible"})
            continue
        try:
            cfg = load_config(root)
            reviews = _project_review_rows(name, root, cfg)
            project_tasks = _project_task_rows(root)
            tasks_by_id = {task["id"]: task for task in project_tasks}
            task_statuses = {task_id: task.get("status")
                             for task_id, task in tasks_by_id.items()}
        except (OSError, yaml.YAMLError, ValueError) as e:
            skipped.append({"project": name, "reason": f"project unreadable: {type(e).__name__}"})
            continue

        exposures, exposure_skipped = _round_exposure_rows(root)
        latest_exposures = _latest_round_exposures(exposures)
        warnings, warning_skipped, warnings_present = _load_warning_rows(root)
        delegations, n_skipped, n_verdicts_invalid, n_verify_invalid = (
            _project_delegation_rows(root))
        delegation_skipped += n_skipped
        verdicts_invalid += n_verdicts_invalid
        verification_artifacts_invalid += n_verify_invalid
        did_to_task = {delegation["did"]: delegation["task_id"] for delegation in delegations}
        project_warnings, context_unknown = _normalize_warning_rows(warnings, did_to_task)
        warning_context_coverage.update(context_unknown)
        warning_rows_skipped += warning_skipped
        round_exposures_skipped += exposure_skipped
        warning_observations.append(_warning_observation(
            name, project_warnings, warning_skipped, warnings_present, exposures, exposure_skipped))
        adaptive_observations.append(_adaptive_feedback_observation(
            name, root, reviews, project_warnings, exposures, context_unknown))

        by_task: dict[str, dict] = {}
        for task_id, task in tasks_by_id.items():
            session_id = task.get("session_id") if isinstance(task.get("session_id"), str) else None
            if session_id is None:
                task_session_unknown += 1
            by_task[task_id] = {
                "task_id": task_id, "project": name, "findings": [], "delegations": [],
                "join_key": "task-id", "provenance": "explicit",
                "task_status": task.get("status"),
                "task_context": {
                    "round": task.get("round"), "session_id": session_id,
                    "acceptance_criteria": len(task.get("accept") or [])
                        if isinstance(task.get("accept"), list) else 0,
                    "declared_scope_count": len(task.get("scope") or [])
                        if isinstance(task.get("scope"), list) else 0,
                    "anchor": task.get("anchor"),
                },
            }
        for review in reviews:
            for finding in review.get("findings") or []:
                task_id = (finding.get("id") if finding.get("source") == "task"
                           else finding.get("task_id"))
                if not task_id:
                    unlinked += 1
                    continue
                row = by_task.setdefault(task_id, {
                    "task_id": task_id, "project": name, "findings": [], "delegations": [],
                    "join_key": "task-id", "provenance": "explicit",
                    "task_status": task_statuses.get(task_id),
                    "task_context": {"round": None, "session_id": None,
                                     "acceptance_criteria": 0, "declared_scope_count": 0,
                                     "anchor": None},
                })
                projected_finding = {
                    "round": review.get("round_id"), "severity": finding.get("severity"),
                    "status": finding.get("status"), "type": finding.get("type", "unknown"),
                }
                for key in ("review_origin", "fixing_rounds", "status_history", "reopen_count",
                            "source_pointer", "evidence_pointers"):
                    if key in finding:
                        projected_finding[key] = finding[key]
                row["findings"].append(projected_finding)

        for delegation in delegations:
            task_id = delegation["task_id"]
            row = by_task.setdefault(task_id, {
                "task_id": task_id, "project": name, "findings": [], "delegations": [],
                "join_key": "task-id", "provenance": "explicit",
                "task_status": task_statuses.get(task_id),
                "task_context": {"round": None, "session_id": None,
                                 "acceptance_criteria": 0, "declared_scope_count": 0,
                                 "anchor": None},
            })
            row["delegations"].append({key: value for key, value in delegation.items()
                                       if key != "task_id"})
        warning_refs: dict[str, list[str]] = {}
        for number, warning in enumerate(project_warnings, 1):
            warning_id = f"{name}:warning-{number:06d}"
            normalized_warnings.append({"project": name, "warning_id": warning_id, **warning})
            for task_id in warning["task_ids"]:
                warning_refs.setdefault(task_id, []).append(warning_id)
        for row in by_task.values():
            row["findings"].sort(
                key=lambda f: (
                    str(f.get("round")), str(f.get("severity")), str(f.get("status")),
                    str(f.get("type")),
                ))
            row["delegations"].sort(key=lambda x: x["did"])
            task = tasks_by_id.get(row["task_id"], {})
            row["acceptance"] = _task_acceptance(task, row["delegations"], latest_exposures)
            if row["acceptance"]["provenance"] == "current-task-state-approximation":
                acceptance_approximations += 1
            row["route_guard"] = {
                "round_exposure": _round_exposure_projection(task, latest_exposures),
                "warning_refs": warning_refs.get(row["task_id"], []),
            }
            task_rows.append(row)
        scanned.append(name)

    task_rows.sort(key=lambda r: (r["project"], r["task_id"]))
    normalized_warnings.sort(key=lambda row: (row["project"], row["warning_id"]))
    task_session_unknown = sum(
        not isinstance((row.get("task_context") or {}).get("session_id"), str)
        for row in task_rows)
    acceptance_approximations = sum(
        (row.get("acceptance") or {}).get("provenance") == "current-task-state-approximation"
        for row in task_rows)
    finding_total = sum(len(row.get("findings") or []) for row in task_rows)
    delegation_total = sum(len(row.get("delegations") or []) for row in task_rows)
    coverage = {
        "generated_from": generated_from,
        "projects_total": len(entries),
        "projects_scanned": sorted(scanned),
        "projects_skipped": sorted(skipped, key=lambda s: s["project"]),
        "unlinked_findings": unlinked,
        "delegations_skipped": delegation_skipped,
        "verdicts_invalid": verdicts_invalid,
        "verification_artifacts_invalid": verification_artifacts_invalid,
        "warning_rows_skipped": warning_rows_skipped,
        "round_exposures_skipped": round_exposures_skipped,
        "task_session_unknown": task_session_unknown,
        "acceptance_approximations": acceptance_approximations,
        "warning_context_coverage": dict(sorted(warning_context_coverage.items())),
        "warning_observations": sorted(warning_observations, key=lambda row: row["project"]),
        "row_totals": {"tasks": len(task_rows), "findings": finding_total,
                       "delegations": delegation_total},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [_dumps(row) + "\n" for row in task_rows]
    lines.append(_dumps({"coverage": coverage}) + "\n")
    (out_dir / "evidence.jsonl").write_text("".join(lines), encoding="utf-8")
    (out_dir / "evidence_warnings.jsonl").write_text(
        "".join(_dumps(row) + "\n" for row in normalized_warnings), encoding="utf-8")
    (out_dir / "adaptive_feedback.json").write_text(
        _dumps(sorted(adaptive_observations, key=lambda row: row["project"])) + "\n",
        encoding="utf-8")
    return coverage


# ================================================================== audit (§9)
# Deterministic facts over projection artifacts ONLY (never raw logs — layer separation).
# Each lens carries a versioned rule id + provenance; <=5 examples with evidence pointers. No model
# interpretation. round<->session joins use only recorded session ids; missing bindings stay unknown.
_AUDIT_INPUTS = {
    "sessions": "sessions.jsonl",
    "delegations": "delegations.jsonl",
    "reviews": "reviews.jsonl",
    "parse_coverage": "parse_coverage.json",
}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _ratio(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def _by_project(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("project") or "", []).append(r)
    return groups


def _cat(s: dict, name: str) -> int:
    return (s.get("tools") or {}).get("by_category", {}).get(name, 0)


def _direct_work(s: dict) -> int:
    return _cat(s, "file_write") + _cat(s, "shell")


def _lens(name: str, rule: str, provenance: str, per_project: dict, examples: list, **extra) -> dict:
    bounded: list[dict] = []
    for raw in examples:
        if not isinstance(raw, dict):
            continue
        example = dict(raw)
        if not isinstance(example.get("pointer"), str):
            if isinstance(example.get("file"), str):
                example["pointer"] = f"{example['file']}:{example.get('line', 1)}"
            elif isinstance(example.get("task_id"), str):
                example["pointer"] = f"evidence.jsonl#task={example['task_id']}"
            elif isinstance(example.get("did"), str):
                example["pointer"] = f"evidence.jsonl#delegation={example['did']}"
            elif isinstance(example.get("session_id"), str):
                example["pointer"] = f"sessions.jsonl#session={example['session_id']}"
            elif isinstance(example.get("round_id"), str):
                example["pointer"] = f"reviews.jsonl#round={example['round_id']}"
        if isinstance(example.get("pointer"), str):
            bounded.append(example)
        if len(bounded) == 5:
            break
    d = {"lens": name, "rule": rule, "provenance": provenance,
         "per_project": per_project, "examples": bounded}
    d.update(extra)
    return d


def _lens_main_direct_work(sessions: list[dict]) -> dict:
    mains = [s for s in sessions if s.get("kind") == "main"]
    per: dict[str, dict] = {}
    for proj, rows in _by_project(mains).items():
        fw = sum(_cat(s, "file_write") for s in rows)
        sh = sum(_cat(s, "shell") for s in rows)
        tool_total = sum(sum((s.get("tools") or {}).get("by_category", {}).values()) for s in rows)
        per[proj] = {
            "main_sessions": len(rows),
            "file_write": fw,
            "shell": sh,
            "direct_work": fw + sh,
            "direct_work_ratio": _ratio(fw + sh, tool_total),
            "sessions_delegation_zero_direct":
                sum(1 for s in rows if s.get("delegations", 0) == 0 and _direct_work(s) > 0),
        }
    top = sorted(mains, key=lambda s: (-_direct_work(s), s.get("file") or ""))[:5]
    examples = [{"file": s.get("file"), "session_id": s.get("session_id"),
                 "file_write": _cat(s, "file_write"), "shell": _cat(s, "shell"),
                 "delegations": s.get("delegations", 0)} for s in top if _direct_work(s) > 0]
    return _lens("main_direct_work", "main-direct-work-v1", "inferred", per, examples)


def _lens_verification_debt(sessions: list[dict]) -> dict:
    def runs(s: dict) -> int:
        return (s.get("verification") or {}).get("runs", 0)

    def builds(s: dict) -> int:
        return (s.get("build") or {}).get("runs", 0)
    per: dict[str, dict] = {}
    for proj, rows in _by_project(sessions).items():
        fw_rows = [s for s in rows if _cat(s, "file_write") > 0]
        # a passing build IS a correctness check — a file-writing session that only compiled is NOT
        # debt; count those separately as build_only_sessions so the signal isn't lost
        debt = [s for s in fw_rows if runs(s) == 0 and builds(s) == 0]
        build_only = [s for s in fw_rows if runs(s) == 0 and builds(s) > 0]
        per[proj] = {
            "sessions": len(rows),
            "file_write_sessions": len(fw_rows),
            "debt_sessions": len(debt),
            "debt_ratio": _ratio(len(debt), len(fw_rows)),
            "build_only_sessions": len(build_only),
            "unclassified_shell_total": sum(s.get("unclassified_shell", 0) for s in rows),
        }
    debt_all = [s for s in sessions if _cat(s, "file_write") > 0 and runs(s) == 0 and builds(s) == 0]
    top = sorted(debt_all, key=lambda s: (-_cat(s, "file_write"), s.get("file") or ""))[:5]
    examples = [{"file": s.get("file"), "session_id": s.get("session_id"),
                 "file_write": _cat(s, "file_write")} for s in top]
    return _lens("verification_debt", "verification-debt-v2", "inferred", per, examples)


def _lens_retry_loops(sessions: list[dict]) -> dict:
    def cnt(s: dict) -> int:
        return (s.get("retry_loops") or {}).get("count", 0)
    per: dict[str, dict] = {}
    for proj, rows in _by_project(sessions).items():
        workers = [s for s in rows if s.get("kind") in ("subagent", "workflow_subagent")]
        per[proj] = {
            "sessions": len(rows),
            "sessions_with_retry": sum(1 for s in rows if cnt(s) > 0),
            "retry_loops_total": sum(cnt(s) for s in rows),
            "worker_sessions": len(workers),
            "worker_sessions_with_retry": sum(1 for s in workers if cnt(s) > 0),
            "worker_retry_loops_total": sum(cnt(s) for s in workers),
        }
    top = sorted([s for s in sessions if cnt(s) > 0],
                 key=lambda s: (-cnt(s), s.get("file") or ""))[:5]
    examples = []
    for s in top:
        ex = {"file": s.get("file"), "session_id": s.get("session_id"), "count": cnt(s)}
        rexs = (s.get("retry_loops") or {}).get("examples") or []
        if rexs and isinstance(rexs[0], dict) and "line" in rexs[0]:
            ex["line"] = rexs[0]["line"]
        examples.append(ex)
    return _lens("retry_loops", "retry-loops-v2", "inferred", per, examples)


def _lens_context_heavy(sessions: list[dict]) -> dict:
    def ch(s: dict, k: str) -> int:
        return (s.get("context_heavy") or {}).get(k, 0)

    def input_tokens(s: dict) -> int:
        value = (s.get("usage") or {}).get("input", 0)
        return value if type(value) is int else 0

    per: dict[str, dict] = {}
    for proj, rows in _by_project(sessions).items():
        mains = [s for s in rows if s.get("kind") == "main"]
        workers = [s for s in rows if s.get("kind") in ("subagent", "workflow_subagent")]
        per[proj] = {
            "sessions": len(rows),
            "sessions_over_100kb": sum(1 for s in rows if ch(s, "tool_results_over_100kb") > 0),
            "results_over_100kb_total": sum(ch(s, "tool_results_over_100kb") for s in rows),
            "max_result_bytes": max((ch(s, "max_result_bytes") for s in rows), default=0),
            "main_sessions": len(mains),
            "main_input_tokens": sum(input_tokens(s) for s in mains),
            "main_max_result_bytes_total": sum(ch(s, "max_result_bytes") for s in mains),
            "worker_sessions": len(workers),
            "worker_sessions_over_100kb": sum(
                1 for s in workers if ch(s, "tool_results_over_100kb") > 0),
            "worker_results_over_100kb_total": sum(
                ch(s, "tool_results_over_100kb") for s in workers),
            "worker_max_result_bytes": max(
                (ch(s, "max_result_bytes") for s in workers), default=0),
        }
    top = sorted(sessions, key=lambda s: (-ch(s, "max_result_bytes"), s.get("file") or ""))[:5]
    examples = [{"file": s.get("file"), "session_id": s.get("session_id"),
                 "max_result_bytes": ch(s, "max_result_bytes"),
                 "tool_results_over_100kb": ch(s, "tool_results_over_100kb")}
                for s in top if ch(s, "max_result_bytes") > 0]
    return _lens("context_heavy", "context-heavy-v3", "explicit", per, examples)


def _deleg_val(v) -> str:
    if isinstance(v, dict):
        return "unknown"  # {"provenance": "unknown"} — no result record joined
    if v is None:
        return "unspecified"
    return str(v)


def _lens_delegation_pattern(delegations: list[dict]) -> dict:
    per: dict[str, dict] = {}
    for proj, rows in _by_project(delegations).items():
        by_tool = Counter(_deleg_val(r.get("tool")) for r in rows)
        # is_async is only known (True/False) when a result record joined; unresolved delegations
        # carry a provenance-unknown marker (invariant #11) and must NOT deflate the ratio to a
        # definite 0.0 — compute the ratio only over the known subset, null when all unknown
        async_known = [r for r in rows if isinstance(r.get("is_async"), bool)]
        async_count = sum(1 for r in async_known if r.get("is_async") is True)
        per[proj] = {
            "delegations": len(rows),
            "by_tool": dict(sorted(by_tool.items())),
            "by_subagent_type": dict(sorted(Counter(_deleg_val(r.get("subagent_type")) for r in rows).items())),
            "by_model_requested": dict(sorted(Counter(_deleg_val(r.get("model_requested")) for r in rows).items())),
            "by_resolved_model": dict(sorted(Counter(_deleg_val(r.get("resolved_model")) for r in rows).items())),
            "by_status": dict(sorted(Counter(_deleg_val(r.get("status")) for r in rows).items())),
            "async_count": async_count,
            "async_unknown": len(rows) - len(async_known),
            "async_ratio": _ratio(async_count, len(async_known)) if async_known else None,
            "workflow_delegations": by_tool.get("Workflow", 0),
        }
    top = sorted(delegations,
                 key=lambda r: (r.get("project") or "", r.get("file") or "", r.get("line") or 0))[:5]
    examples = [{"file": r.get("file"), "line": r.get("line"), "session_id": r.get("session_id"),
                 "subagent_type": r.get("subagent_type"), "model_requested": r.get("model_requested"),
                 "resolved_model": _deleg_val(r.get("resolved_model")),
                 "status": _deleg_val(r.get("status"))} for r in top]
    return _lens("delegation_pattern", "delegation-pattern-v2", "explicit", per, examples)


def _evidence_task_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("task_id"), str)]


def _evidence_coverage(rows: list[dict]) -> dict:
    for row in reversed(rows):
        if isinstance(row, dict) and isinstance(row.get("coverage"), dict):
            return row["coverage"]
    return {}


def _lens_delegation_opportunity(sessions: list[dict], evidence_rows: list[dict]) -> dict:
    session_index: dict[tuple[str, str], list[dict]] = {}
    for session in sessions:
        if isinstance(session.get("session_id"), str):
            session_index.setdefault(
                (session.get("project") or "", session["session_id"]), []).append(session)
    task_rows = _evidence_task_rows(evidence_rows)
    task_sessions: Counter = Counter()
    for row in task_rows:
        context = row.get("task_context") or {}
        if isinstance(context.get("session_id"), str):
            task_sessions[(row.get("project") or "", context["session_id"])] += 1

    coverage = Counter()
    per: dict[str, dict] = {}
    candidates: list[dict] = []
    evaluated_by_project: Counter = Counter()
    for row in task_rows:
        project = row.get("project") or ""
        context = row.get("task_context") or {}
        session_id = context.get("session_id")
        if not isinstance(session_id, str):
            coverage["task_session_unknown"] += 1
            continue
        key = (project, session_id)
        if task_sessions[key] != 1:
            coverage["ambiguous_task_session"] += 1
            continue
        matches = session_index.get(key, [])
        if len(matches) != 1:
            coverage["session_not_found" if not matches else "ambiguous_trace_session"] += 1
            continue
        session = matches[0]
        if session.get("kind") != "main":
            coverage["non_main_session"] += 1
            continue
        routing_notes = [
            delegation["routing_note"]["note"]
            for delegation in (row.get("delegations") or [])
            if isinstance(delegation, dict)
            and isinstance(delegation.get("routing_note"), dict)
            and delegation["routing_note"].get("provenance") == "main-session"
            and isinstance(delegation["routing_note"].get("note"), str)
            and delegation["routing_note"]["note"]
            and "\n" not in delegation["routing_note"]["note"]
            and "\r" not in delegation["routing_note"]["note"]
        ]
        if routing_notes:
            coverage["routing_note_rebuttal"] += 1
            continue
        if row.get("delegations"):
            coverage["already_delegated"] += 1
            continue
        file_write = _cat(session, "file_write")
        shell = _cat(session, "shell")
        direct_work = file_write + shell
        retries = (session.get("retry_loops") or {}).get("count", 0)
        max_result_bytes = (session.get("context_heavy") or {}).get("max_result_bytes", 0)
        input_tokens = (session.get("usage") or {}).get("input", 0)
        evaluated_by_project[project] += 1
        triggered_by = []
        if (context.get("acceptance_criteria", 0) > 0
                and context.get("declared_scope_count", 0) > 0 and direct_work > 0):
            triggered_by.append("delegable-direct-work")
        if direct_work >= DELEGATION_DIRECT_WORK_MIN:
            triggered_by.append("direct-work-size")
        if retries > 0:
            triggered_by.append("retry")
        if max_result_bytes > CONTEXT_HEAVY_BYTES or input_tokens >= DELEGATION_CONTEXT_TOKENS_MIN:
            triggered_by.append("context-cost")
        if file_write <= 0 or not triggered_by:
            coverage["burden_signal_absent"] += 1
            continue
        candidates.append({
            "project": project, "task_id": row["task_id"], "session_id": session_id,
            "file_write": file_write, "shell": shell, "direct_work": direct_work,
            "retry_loops": retries, "max_result_bytes": max_result_bytes,
            "input_tokens": input_tokens,
            "acceptance_criteria": context.get("acceptance_criteria", 0),
            "triggered_by": triggered_by, "provenance": "inferred",
        })
    candidates.sort(key=lambda row: (row["project"], row["task_id"], row["session_id"]))
    candidate_counts = Counter(row["project"] for row in candidates)
    for project in sorted({row.get("project") or "" for row in task_rows}):
        per[project] = {
            "tasks_evaluated": evaluated_by_project[project],
            "candidates": candidate_counts[project],
        }
    examples = [{key: row[key] for key in (
        "project", "task_id", "session_id", "direct_work", "retry_loops", "max_result_bytes")}
        for row in candidates[:5]]
    return _lens(
        "delegation_opportunity", "delegation-opportunity-v1", "inferred", per, examples,
        _projection_rows=candidates, coverage=dict(sorted(coverage.items())),
        thresholds={"direct_work_min": DELEGATION_DIRECT_WORK_MIN,
                    "context_input_tokens_min": DELEGATION_CONTEXT_TOKENS_MIN,
                    "context_result_bytes_gt": CONTEXT_HEAVY_BYTES})


def _lens_worker_scope_drift(evidence_rows: list[dict]) -> dict:
    per: dict[str, dict] = {}
    examples: list[dict] = []
    for project, rows in _by_project(_evidence_task_rows(evidence_rows)).items():
        evaluations: list[tuple[dict, dict]] = []
        coverage: Counter = Counter()
        for row in rows:
            for delegation in row.get("delegations") or []:
                drift = delegation.get("scope_drift") or {}
                if drift.get("evaluable") is not True:
                    coverage[drift.get("coverage_reason") or "scope-drift-unavailable"] += 1
                    continue
                evaluations.append((delegation, drift))
                if drift.get("outside_scope") and len(examples) < 5:
                    examples.append({
                        "project": project, "task_id": row["task_id"],
                        "did": delegation.get("did"), "outside_scope": drift["outside_scope"],
                    })
        per[project] = {
            "delegations_evaluable": len(evaluations),
            "delegations_drifted": sum(bool(drift.get("outside_scope"))
                                       for _delegation, drift in evaluations),
            "changed_files": sum(len(drift.get("changed_files") or [])
                                 for _delegation, drift in evaluations),
            "outside_files": sum(len(drift.get("outside_scope") or [])
                                 for _delegation, drift in evaluations),
            "coverage": dict(sorted(coverage.items())),
        }
    return _lens("worker_scope_drift", "worker-scope-drift-v1", "inferred", per, examples)


def _lens_warn_friction(evidence_rows: list[dict]) -> dict:
    observations = _evidence_coverage(evidence_rows).get("warning_observations") or []
    per = {
        row["project"]: {key: row[key] for key in (
            "records", "fire", "conflict", "by_rule", "by_boundary", "by_rule_boundary",
            "recent_rounds", "coverage")}
        for row in observations if isinstance(row, dict) and isinstance(row.get("project"), str)
    }
    examples = []
    for project, facts in sorted(per.items()):
        for row in facts["recent_rounds"]:
            if row["fire"] or row["conflict"]:
                examples.append({"project": project, **row})
    return _lens("warn_friction", "warn-friction-v1", "explicit", per, examples)


def _lens_adaptive_feedback(observations: list[dict]) -> dict:
    per = {row["project"]: row["facts"] for row in observations
           if isinstance(row, dict) and isinstance(row.get("project"), str)
           and isinstance(row.get("facts"), dict)}
    examples = [example for row in observations if isinstance(row, dict)
                for example in (row.get("examples") or [])]
    candidates = [candidate for row in observations if isinstance(row, dict)
                  for candidate in (row.get("stale_candidates") or [])]
    return _lens(
        "adaptive_feedback", "adaptive-feedback-v1", "observed", per, examples,
        _projection_rows=candidates, causal_claims=False,
        interpretation_boundary="firing-rate-and-finding-recurrence-trends-only")


def _lens_error_landscape(sessions: list[dict]) -> dict:
    def err(s: dict, k: str) -> int:
        return (s.get("errors") or {}).get(k, 0)

    def total(s: dict) -> int:
        return err(s, "api") + err(s, "tool") + err(s, "parse")
    per: dict[str, dict] = {}
    for proj, rows in _by_project(sessions).items():
        api = sum(err(s, "api") for s in rows)
        tool = sum(err(s, "tool") for s in rows)
        parse = sum(err(s, "parse") for s in rows)
        per[proj] = {
            "sessions": len(rows),
            "api": api, "tool": tool, "parse": parse,
            "sessions_with_errors": sum(1 for s in rows if total(s) > 0),
            "errors_per_session": _ratio(api + tool + parse, len(rows)),
        }
    top = sorted(sessions, key=lambda s: (-total(s), s.get("file") or ""))[:5]
    examples = [{"file": s.get("file"), "session_id": s.get("session_id"),
                 "api": err(s, "api"), "tool": err(s, "tool"), "parse": err(s, "parse")}
                for s in top if total(s) > 0]
    return _lens("error_landscape", "error-landscape-v1", "explicit", per, examples)


def _lens_env_unpreparedness(sessions: list[dict], evidence_rows: list[dict],
                             evidence_available: bool = True) -> dict:
    projects = {session.get("project") or "" for session in sessions}
    task_rows = _evidence_task_rows(evidence_rows)
    projects.update(row.get("project") or "" for row in task_rows)
    per: dict[str, dict] = {}
    examples: list[dict] = []
    session_groups = _by_project(sessions)
    evidence_groups = _by_project(task_rows)
    for project in sorted(projects):
        signatures: Counter = Counter()
        sessions_with_signatures = 0
        sessions_without_projection = 0
        for session in session_groups.get(project, []):
            facts = session.get("env_unpreparedness")
            if not isinstance(facts, dict):
                sessions_without_projection += 1
                continue
            current = facts.get("signatures") or {}
            if current:
                sessions_with_signatures += 1
            for signature, count in current.items():
                if isinstance(signature, str) and isinstance(count, int):
                    signatures[signature] += count
            for example in facts.get("examples") or []:
                if len(examples) < 5:
                    examples.append({"project": project, "session_id": session.get("session_id"),
                                     **example})
        delegations = [delegation for row in evidence_groups.get(project, [])
                       for delegation in (row.get("delegations") or [])]
        failed_env = [delegation for delegation in delegations
                      if delegation.get("state") == "failed-env"]
        for delegation in failed_env:
            if len(examples) < 5:
                examples.append({"project": project, "did": delegation.get("did"),
                                 "env": delegation.get("env")})
        per[project] = {
            "sessions": len(session_groups.get(project, [])),
            "sessions_with_dependency_signatures": sessions_with_signatures,
            "dependency_error_signatures": dict(sorted(signatures.items())),
            "env_prep_failures": len(failed_env) if evidence_available else None,
            "evidence_available": evidence_available,
            "coverage": {"sessions_without_signature_projection": sessions_without_projection},
        }
    return _lens(
        "env_unpreparedness", "env-unpreparedness-v1", "inferred", per, examples,
        signal_provenance={"env_prep": "explicit", "dependency_signatures": "inferred"})


def _lens_review_association(reviews: list[dict]) -> dict:
    per: dict[str, dict] = {}
    for proj, rows in _by_project(reviews).items():
        sev = {"blocker": 0, "major": 0, "minor": 0, "unknown": 0}
        by_source: Counter = Counter()
        findings_total = 0
        for r in rows:
            for k in sev:
                sev[k] += (r.get("counts") or {}).get(k, 0)
            for f in r.get("findings") or []:
                findings_total += 1
                by_source[f.get("source") or "unknown"] += 1
        per[proj] = {
            "rounds": len(rows),
            "rounds_with_feedback": sum(1 for r in rows if r.get("feedback_file")),
            "findings_total": findings_total,
            "severity_counts": sev,
            "by_source": dict(sorted(by_source.items())),
            # 0.7: reviews are project-level only — no timestamp heuristic to bind a round to a session
            "round_session_mapping": {"provenance": "unknown"},
        }
    top = sorted(reviews, key=lambda r: (r.get("project") or "", r.get("round_id") or ""))[:5]
    examples = [{"file": r.get("feedback_file") or r.get("request_file"),
                 "round_id": r.get("round_id"), "session_id": {"provenance": "unknown"}}
                for r in top]
    return _lens("review_association", "review-association-v2", "explicit", per, examples)


def _session_role(session: dict) -> str:
    """Classify only the canonical main execution; execution labels are never profile roles."""
    return "main" if session.get("kind") == "main" else "unknown"


def _lens_finding_concentration(reviews: list[dict], sessions: list[dict],
                                evidence_rows: list[dict] | None = None) -> dict:
    session_index: dict[tuple[str, str], list[dict]] = {}
    for session in sessions:
        if isinstance(session.get("session_id"), str):
            session_index.setdefault(
                (session.get("project") or "", session["session_id"]), []).append(session)
    per: dict[str, dict] = {}
    examples: list[dict] = []
    projection_rows: list[dict] = []
    review_groups = _by_project(reviews)
    evidence_groups = _by_project(_evidence_task_rows(evidence_rows or []))
    projects = sorted(set(review_groups) | set(evidence_groups))
    for project in projects:
        rows = review_groups.get(project, [])
        by_role: Counter = Counter()
        by_session_kind: Counter = Counter()
        by_area: Counter = Counter()
        by_task: Counter = Counter()
        type_rounds: dict[str, set[str]] = {}
        severe_type_rounds: dict[str, set[str]] = {}
        type_occurrences: Counter = Counter()
        verification_by_round: Counter = Counter()
        reporting_by_round: Counter = Counter()
        unknown_type = rejected_type = unknown_task = unknown_area = unknown_role = 0
        verified_typed = non_real = 0
        recurrence_status_coverage: Counter = Counter()
        remediation_rounds: set[str] = set()
        remediation_by_task: dict[str, set[str]] = {}
        reopen_by_task: dict[str, int] = {}
        round_session_unknown = 0
        review_rounds_with_time = review_rounds_without_time = 0
        review_events: list[tuple[object, str]] = []
        for row in rows:
            findings = row.get("findings") or []
            round_id = row.get("round_id")
            if isinstance(round_id, str):
                verification_by_round[round_id] += 0
                reporting_by_round[round_id] += 0
            round_at = parse_iso_timestamp(row.get("round_at"))
            if round_at is None:
                review_rounds_without_time += 1
            else:
                review_rounds_with_time += 1
            routes = [route for route in (row.get("routes") or [])
                      if isinstance(route, dict) and isinstance(route.get("role"), str)
                      and route.get("provenance") == "main-session"]
            routed_roles = sorted({route["role"] for route in routes})
            if len(routed_roles) == 1:
                role = routed_roles[0]
                executions = sorted({str(route.get("execution")) for route in routes})
                session_kind = (f"host-guided:{executions[0]}" if len(executions) == 1
                                else "host-guided:mixed")
            else:
                role = "unknown"
                session_id = row.get("session_id")
                matches = (session_index.get((project, session_id), [])
                           if isinstance(session_id, str) else [])
                session_kind = (matches[0].get("kind") if len(matches) == 1
                                and isinstance(matches[0].get("kind"), str) else "unknown")
            if role == "unknown":
                round_session_unknown += 1
            for finding in findings:
                finding_type = finding.get("type", "unknown")
                status = finding.get("status")
                if status == "REJECTED":
                    rejected_type += 1
                    non_real += 1
                elif status == "REAL" and finding_type in FINDING_TYPES:
                    verified_typed += 1
                    type_occurrences[finding_type] += 1
                    if isinstance(round_id, str):
                        type_rounds.setdefault(finding_type, set()).add(round_id)
                        if finding.get("severity") in ("blocker", "major"):
                            severe_type_rounds.setdefault(finding_type, set()).add(round_id)
                        if finding_type == "verification":
                            verification_by_round[round_id] += 1
                        if finding_type == "reporting":
                            reporting_by_round[round_id] += 1
                    if round_at is not None:
                        review_events.append((round_at, finding_type))
                elif status != "REAL":
                    non_real += 1
                    recurrence_status_coverage[str(status) if status is not None else "unknown"] += 1
                else:
                    unknown_type += 1
                by_role[role] += 1
                by_session_kind[session_kind] += 1
                if role == "unknown":
                    unknown_role += 1
                task_id = (finding.get("id") if finding.get("source") == "task"
                           else finding.get("task_id"))
                if isinstance(task_id, str) and task_id:
                    by_task[task_id] += 1
                else:
                    unknown_task += 1
                areas = sorted({path.split("/", 1)[0] for path in (finding.get("paths") or [])
                                if isinstance(path, str) and path})
                if not areas:
                    unknown_area += 1
                for area in areas:
                    by_area[area] += 1
                remediation_rounds.update(
                    value for value in (finding.get("fixing_rounds") or [])
                    if isinstance(value, str))
                if isinstance(task_id, str) and task_id:
                    fixing_rounds = finding.get("fixing_rounds")
                    if isinstance(fixing_rounds, list) and all(
                            isinstance(value, str) for value in fixing_rounds):
                        remediation_by_task.setdefault(task_id, set()).update(fixing_rounds)
                    reopen_count = finding.get("reopen_count")
                    if type(reopen_count) is int and reopen_count >= 0:
                        reopen_by_task[task_id] = max(
                            reopen_by_task.get(task_id, 0), reopen_count)
                if len(examples) < 5:
                    examples.append({
                        "project": project, "round_id": row.get("round_id"),
                        "finding_id": finding.get("id"), "type": finding_type,
                        "task_id": task_id, "role": role,
                        "project_areas": areas or ["unknown"],
                        "pointer": finding.get("source_pointer")
                        or f"reviews.jsonl#round={row.get('round_id')}/finding={finding.get('id')}",
                    })
        recurring = []
        for finding_type in sorted(type_rounds):
            round_ids = sorted(type_rounds[finding_type])
            if len(round_ids) > 1:
                recurring.append({
                    "type": finding_type, "round_count": len(round_ids),
                    "first_round": round_ids[0], "last_round": round_ids[-1],
                    "occurrences": type_occurrences[finding_type],
                    "recurrences": len(round_ids) - 1,
                })
        acceptance_bindings_excluded = 0
        baselines: list[tuple[object, str]] = []
        for evidence_row in evidence_groups.get(project, []):
            acceptance = evidence_row.get("acceptance") or {}
            accepted_at = parse_iso_timestamp(acceptance.get("accepted_at"))
            if (acceptance.get("provenance") != "explicit"
                    or acceptance.get("resolved") is not True or accepted_at is None):
                if acceptance.get("resolved") is True:
                    acceptance_bindings_excluded += 1
                continue
            types = sorted({finding.get("type") for finding in evidence_row.get("findings") or []
                            if finding.get("status") == "REAL"
                            and finding.get("type") in FINDING_TYPES})
            if not types:
                acceptance_bindings_excluded += 1
                continue
            baselines.extend((accepted_at, finding_type) for finding_type in types)
        post_acceptance_defects = sum(any(
            event_type == finding_type and event_at > accepted_at
            for event_at, event_type in review_events)
            for accepted_at, finding_type in baselines)
        projection_rows.extend({
            "project": project, "task_id": task_id, "findings": count,
            "pointer": f"evidence.jsonl#task={task_id}",
        } for task_id, count in sorted(by_task.items()))
        type_round_observations = sum(len(round_ids) for round_ids in type_rounds.values())
        severe_type_round_observations = sum(
            len(round_ids) for round_ids in severe_type_rounds.values())

        def round_trend(counts: Counter) -> dict:
            round_ids = sorted(counts)
            if not round_ids:
                return {"rounds": 0, "first_round": None, "first": None,
                        "last_round": None, "last": None}
            return {
                "rounds": len(round_ids),
                "first_round": round_ids[0], "first": counts[round_ids[0]],
                "last_round": round_ids[-1], "last": counts[round_ids[-1]],
            }

        per[project] = {
            "rounds": len(rows),
            "findings_total": sum(len(row.get("findings") or []) for row in rows),
            "tasks_with_findings": len(by_task),
            "by_role": dict(sorted(by_role.items())),
            "by_session_kind": dict(sorted(by_session_kind.items())),
            "by_project_area": dict(sorted(by_area.items())),
            "recurring_types": recurring,
            "verified_real_typed": verified_typed,
            "unknown_type_excluded": unknown_type,
            "rejected_type_excluded": rejected_type,
            "non_real_excluded": non_real,
            "recurrence_status_coverage": dict(sorted(recurrence_status_coverage.items())),
            "taxonomy_type_round_observations": type_round_observations,
            "finding_recurrences": sum(max(len(round_ids) - 1, 0)
                                       for round_ids in type_rounds.values()),
            "verified_severe_type_round_observations": severe_type_round_observations,
            "severe_finding_recurrences": sum(max(len(round_ids) - 1, 0)
                                              for round_ids in severe_type_rounds.values()),
            "verification_finding_trend": round_trend(verification_by_round),
            "reporting_finding_trend": round_trend(reporting_by_round),
            "distinct_remediation_rounds": len(remediation_rounds),
            "finding_tasks_with_remediation_history": len(remediation_by_task),
            "remediation_rounds_total": sum(
                len(round_ids) for round_ids in remediation_by_task.values()),
            "finding_tasks_with_reopen_history": len(reopen_by_task),
            "reopens": sum(reopen_by_task.values()),
            "review_rounds_with_observed_at": review_rounds_with_time,
            "review_rounds_without_observed_at": review_rounds_without_time,
            "post_acceptance_source_available": evidence_rows is not None,
            "accepted_task_type_baselines": len(baselines),
            "acceptance_bindings_excluded": acceptance_bindings_excluded,
            "post_acceptance_defects": post_acceptance_defects,
            "unknown_task": unknown_task,
            "unknown_project_area": unknown_area,
            "unknown_role": unknown_role,
            "round_session_unknown": round_session_unknown,
        }
    return _lens(
        "finding_concentration", "finding-concentration-v2", "inferred", per, examples,
        _projection_rows=projection_rows,
        recurrence_rule="same-type-across-rounds-v1",
        signal_provenance={"taxonomy": "explicit", "role": "explicit-or-unknown",
                           "project_area": "inferred"})


def _lens_coverage_caveats(coverage: dict) -> dict:
    summary = {
        "parser_version": coverage.get("parser_version"),
        "files_skipped": coverage.get("files_skipped", 0),
        "record_parse_errors": coverage.get("record_parse_errors", 0),
        "replayed_records_skipped": coverage.get("replayed_records_skipped", 0),
        "partial_tail_lines": coverage.get("partial_tail_lines", 0),
        "unknown_raw_types": coverage.get("unknown_raw_types", {}),
        "unknown_tool_result_status": coverage.get("unknown_tool_result_status", 0),
        "row_totals": coverage.get("row_totals", {}),
    }
    # self-session truncation caveat is only present when trace ran inside a live CC session
    if "self_session" in coverage:
        summary["self_session"] = coverage["self_session"]
    return _lens("coverage_caveats", "coverage-caveats-v1", "explicit", {}, [], summary=summary)


def _lens_evidence_link(rows: list[dict]) -> dict:
    task_rows = [r for r in rows if isinstance(r, dict) and r.get("task_id")]
    per: dict[str, dict] = {}
    examples: list[dict] = []
    for proj, prows in _by_project(task_rows).items():
        joined = [r for r in prows if r.get("findings") and r.get("delegations")]
        unverified_with_open_severe = 0
        for row in joined:
            acceptance = row.get("acceptance") or {}
            resolved = acceptance.get("resolved") if isinstance(acceptance, dict) else None
            open_severe = (
                ((resolved is False) if isinstance(resolved, bool) else (
                    row.get("task_status") is not None
                    and row["task_status"] not in ("done", "dropped")))
                and any(f.get("severity") in ("blocker", "major")
                        and f.get("status") != "REJECTED"
                        for f in row.get("findings") or []))
            if not open_severe:
                continue
            count = sum(1 for d in row.get("delegations") or []
                        if d.get("verification_present") is False)
            unverified_with_open_severe += count
            if count and len(examples) < 5:
                examples.append({"project": proj, "task_id": row["task_id"],
                                 "unverified_delegations": count})
        per[proj] = {
            "tasks_with_findings": sum(1 for r in prows if r.get("findings")),
            "tasks_with_delegations": sum(1 for r in prows if r.get("delegations")),
            "tasks_joined": len(joined),
            "unverified_delegations_with_open_severe_findings": unverified_with_open_severe,
            "acceptance_event_bound_tasks": sum(
                (r.get("acceptance") or {}).get("provenance") == "explicit" for r in prows),
            "acceptance_status_approximation_tasks": sum(
                (r.get("acceptance") or {}).get("provenance")
                == "current-task-state-approximation" for r in prows),
        }
    return _lens(
        "evidence_link", "evidence-link-v1", "explicit", per, examples,
        round_session_mapping={"provenance": "unknown"})


def _maturity_decision_snapshot(path: Path) -> tuple[int, bool]:
    if not path.is_file():
        # A project has no decision log before its first recommendation choice. Absence is a
        # complete zero-count Bootstrap input; an existing malformed file below is degraded.
        return 0, True
    count = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0, False
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return count, False
        if (not isinstance(row, dict) or not isinstance(row.get("rec_id"), str)
                or row.get("decision") not in ("accept", "reject")
                or parse_iso_timestamp(row.get("at")) is None):
            return count, False
        count += 1
    return count, True


def maturity_counts(data: dict[str, object], in_dir: Path) -> dict[str, int]:
    sessions = data.get("sessions") if isinstance(data.get("sessions"), list) else []
    delegations = data.get("delegations") if isinstance(data.get("delegations"), list) else []
    reviews = data.get("reviews") if isinstance(data.get("reviews"), list) else []
    round_ids = {row.get("round_id") for row in reviews
                 if isinstance(row, dict) and isinstance(row.get("round_id"), str)}
    return {
        "traced_sessions": len(sessions),
        "rounds": len(round_ids),
        "review_feedback": sum(
            bool(row.get("feedback_file")) for row in reviews if isinstance(row, dict)),
        "findings": sum(
            len(row.get("findings") or []) for row in reviews
            if isinstance(row, dict) and isinstance(row.get("findings") or [], list)),
        "delegations": len(delegations),
        "decisions": _maturity_decision_snapshot(in_dir / "decisions.jsonl")[0],
    }


def maturity_stage(counts: dict[str, int]) -> str:
    """Compute bootstrap/calibrate/tune only. Enforce is intentionally unreachable this arc."""
    if (counts.get("review_feedback", 0) >= TUNE_MIN_REVIEW_FEEDBACK
            and counts.get("findings", 0) >= TUNE_MIN_FINDINGS):
        return "tune"
    activity = sum(counts.get(key, 0) for key in ("review_feedback", "delegations", "decisions"))
    if (counts.get("traced_sessions", 0) >= CALIBRATE_MIN_TRACED_SESSIONS
            and counts.get("rounds", 0) >= CALIBRATE_MIN_ROUNDS
            and activity >= CALIBRATE_MIN_ACTIVITY_SIGNALS):
        return "calibrate"
    return "bootstrap"


def _load_maturity_state(root: Path) -> dict | None:
    path = project_state_path(root) / "maturity.json"
    if not path.is_file():
        return None
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt maturity state {path} ({e})") from e
    if (not isinstance(previous, dict) or previous.get("schema") != "waystone-maturity-1"
            or previous.get("current_stage") not in MATURITY_STAGES
            or not isinstance(previous.get("counts"), dict)
            or not isinstance(previous.get("transitions"), list)):
        raise WorkflowError(f"corrupt maturity state {path}")
    return previous


def _write_maturity_state(root: Path, counts: dict[str, int], stage: str) -> dict:
    path = project_state_path(root) / "maturity.json"
    previous = _load_maturity_state(root)
    state = previous or {
        "schema": "waystone-maturity-1", "current_stage": None, "counts": {}, "transitions": [],
    }
    prior_stage = state.get("current_stage")
    if prior_stage != stage:
        state["transitions"].append({
            "from": prior_stage, "to": stage, "at": datetime.now(timezone.utc).isoformat(),
            "counts": dict(counts),
        })
    state["current_stage"] = stage
    state["counts"] = dict(counts)
    ensure_project_state_dir(root)
    write_text_atomic(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return state


def _maturity_fact(root: Path, data: dict[str, object], in_dir: Path,
                   present: dict[str, bool]) -> dict:
    previous = _load_maturity_state(root)
    degraded_inputs = []
    for key in ("sessions", "delegations", "reviews"):
        fresh_absent = (key == "reviews" and not (in_dir / "reviews.jsonl").exists()
                        and previous is None)
        if fresh_absent:
            continue
        rows = data.get(key)
        if (not present.get(key) or not isinstance(rows, list)
                or any(not isinstance(row, dict) for row in rows)):
            degraded_inputs.append(key)
    _decision_count, decisions_complete = _maturity_decision_snapshot(
        in_dir / "decisions.jsonl")
    decision_log_missing_after_activity = (
        not (in_dir / "decisions.jsonl").exists()
        and previous is not None
        and int((previous.get("counts") or {}).get("decisions", 0)) > 0)
    if not decisions_complete or decision_log_missing_after_activity:
        degraded_inputs.append("decisions")
    if degraded_inputs:
        stage = previous.get("current_stage") if previous is not None else None
        return {
            "stage": stage,
            "counts": dict(previous.get("counts") or {}) if previous is not None else {},
            "recommendation_tier": "always-allowed",
            "recommendation_strength": (
                {"bootstrap": "soft", "calibrate": "soft", "tune": "tuned"}.get(stage, "soft")),
            "degraded": True, "degraded_inputs": sorted(set(degraded_inputs)),
            "state_transition_recorded": False,
        }
    counts = maturity_counts(data, in_dir)
    stage = maturity_stage(counts)
    transitioned = previous is None or previous.get("current_stage") != stage
    _write_maturity_state(root, counts, stage)
    strength = {"bootstrap": "soft", "calibrate": "soft", "tune": "tuned"}[stage]
    return {
        "stage": stage, "counts": counts, "recommendation_tier": "always-allowed",
        "recommendation_strength": strength, "degraded": False, "degraded_inputs": [],
        "state_transition_recorded": transitioned,
    }


def run_audit(in_dir: Path, lens_scope: str | None = None,
              *, project_root: Path | None = None) -> dict:
    if lens_scope not in (None, PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE):
        raise WorkflowError(f"unknown improve lens scope: {lens_scope!r}")
    present: dict[str, bool] = {}
    data: dict[str, object] = {}
    for key, fname in _AUDIT_INPUTS.items():
        p = in_dir / fname
        ok = False
        if p.is_file():
            try:
                data[key] = _load_json(p) if fname.endswith(".json") else _load_jsonl(p)
                ok = True
            except (OSError, json.JSONDecodeError):
                ok = False
        present[key] = ok

    evidence_path = in_dir / "evidence.jsonl"
    if evidence_path.is_file():
        try:
            data["evidence"] = _load_jsonl(evidence_path)
            present["evidence"] = True
        except (OSError, json.JSONDecodeError):
            present["evidence"] = False
    else:
        present["evidence"] = False

    adaptive_path = in_dir / "adaptive_feedback.json"
    if adaptive_path.is_file():
        try:
            adaptive = _load_json(adaptive_path)
            if not isinstance(adaptive, list):
                raise ValueError("adaptive feedback projection is not a list")
            data["adaptive_feedback"] = adaptive
            present["adaptive_feedback"] = True
        except (OSError, json.JSONDecodeError, ValueError):
            present["adaptive_feedback"] = False
    else:
        present["adaptive_feedback"] = False

    lens_specs = [
        ("main_direct_work", ("sessions",), lambda: _lens_main_direct_work(data["sessions"])),
        ("verification_debt", ("sessions",), lambda: _lens_verification_debt(data["sessions"])),
        ("retry_loops", ("sessions",), lambda: _lens_retry_loops(data["sessions"])),
        ("context_heavy", ("sessions",), lambda: _lens_context_heavy(data["sessions"])),
        ("delegation_pattern", ("delegations",),
         lambda: _lens_delegation_pattern(data["delegations"])),
        ("delegation_opportunity", ("sessions", "evidence"),
         lambda: _lens_delegation_opportunity(data["sessions"], data["evidence"])),
        ("worker_scope_drift", ("evidence",),
         lambda: _lens_worker_scope_drift(data["evidence"])),
        ("warn_friction", ("evidence",), lambda: _lens_warn_friction(data["evidence"])),
        ("error_landscape", ("sessions",), lambda: _lens_error_landscape(data["sessions"])),
        ("env_unpreparedness", ("sessions",),
         lambda: _lens_env_unpreparedness(
             data["sessions"], data.get("evidence", []), present.get("evidence", False))),
        ("review_association", ("reviews",), lambda: _lens_review_association(data["reviews"])),
        ("finding_concentration", ("reviews",),
         lambda: _lens_finding_concentration(
             data["reviews"], data.get("sessions", []),
             data.get("evidence") if present.get("evidence") else None)),
        ("coverage_caveats", ("parse_coverage",),
         lambda: _lens_coverage_caveats(data["parse_coverage"])),
    ]
    if present["adaptive_feedback"]:
        lens_specs.append(("adaptive_feedback", ("adaptive_feedback",),
                           lambda: _lens_adaptive_feedback(data["adaptive_feedback"])))
    if lens_scope is not None:
        lens_specs = [spec for spec in lens_specs if lens_scope in LENS_SCOPES[spec[0]]]
    lenses: list[dict] = []
    candidate_rows: list[dict] = []
    skipped: list[dict] = []
    for name, requirements, builder in lens_specs:
        missing = [requirement for requirement in requirements if not present.get(requirement)]
        if not missing:
            lens = builder()
            for row in lens.pop("_projection_rows", []):
                projected = {"lens": name, **row}
                projected.setdefault("pointer", f"evidence.jsonl#task={row.get('task_id')}")
                candidate_rows.append(projected)
            lenses.append(lens)
        else:
            names = [(_AUDIT_INPUTS.get(requirement) or "evidence.jsonl")
                     for requirement in missing]
            skipped.append({"lens": name, "reason": f"missing {', '.join(names)}"})
    if ((lens_scope is None or lens_scope in LENS_SCOPES["evidence_link"])
            and present["evidence"]):
        lenses.append(_lens_evidence_link(data["evidence"]))
    lenses.sort(key=lambda x: x["lens"])
    skipped.sort(key=lambda x: x["lens"])

    facts = {
        "generated_from": str(in_dir),
        "inputs": {k: present.get(k, False) for k in (*_AUDIT_INPUTS, *(["evidence"] if
                   present.get("evidence") else []), *(["adaptive_feedback"] if
                   present.get("adaptive_feedback") else []))},
        "skipped_lenses": skipped,
        "lenses": lenses,
    }
    if project_root is not None and lens_scope == PROJECT_LENS_SCOPE:
        facts["maturity"] = _maturity_fact(project_root, data, in_dir, present)
    if lens_scope is not None:
        facts["scope"] = lens_scope
    candidate_rows.sort(key=lambda row: (
        row.get("lens") or "", row.get("project") or "", row.get("task_id") or "",
        row.get("session_id") or ""))
    (in_dir / "audit_candidates.jsonl").write_text(
        "".join(_dumps(row) + "\n" for row in candidate_rows), encoding="utf-8")
    (in_dir / "facts.json").write_text(_dumps(facts) + "\n", encoding="utf-8")
    return facts


# ================================================================== metrics (§15)
# Metrics are deterministic over the projection bytes. The append timestamp records the measurement
# event; `now` is injectable so the complete snapshot is reproducible in tests. Comparisons are
# factual numeric differences only and never interpreted as policy effects (§7 / §17-5).
_METRIC_INPUTS = {
    "delegations": ("delegations.jsonl", "jsonl"),
    "parse_coverage": ("parse_coverage.json", "json-object"),
    "reviews_coverage": ("reviews_coverage.json", "json-object"),
    "evidence": ("evidence.jsonl", "jsonl"),
    "warnings": ("evidence_warnings.jsonl", "jsonl"),
    "adaptive_feedback": ("adaptive_feedback.json", "json"),
    "decisions": ("decisions.jsonl", "jsonl"),
}

_METRIC_FIRST_MEASURED = {
    "quality.finding_recurrence_rate": "0.8.0",
    "quality.severe_finding_recurrence_rate": "0.8.0",
    "quality.verification_finding_trend": "0.8.0",
    "quality.report_grounding_finding_trend": "0.8.0",
    "quality.reopen_count": "0.8.0",
    "quality.remediation_round_burden": "0.8.0",
    "quality.post_acceptance_defect_rate": "0.8.0",
    "delegation_effectiveness.completion_rate": "0.7.0",
    "delegation_effectiveness.useful_artifact_rate": "0.8.0",
    "delegation_effectiveness.adaptive_before_after_trend": "0.8.0",
    "delegation_effectiveness.opportunity_adjusted_useful_artifact_rate": "0.8.0",
    "delegation_effectiveness.main_direct_work": "0.7.0",
    "delegation_effectiveness.main_context_inflow": "0.7.0",
    "delegation_effectiveness.worker_duplicate_exploration": "0.8.0",
    "delegation_effectiveness.worker_retry_context_load": "0.8.0",
    "delegation_effectiveness.blind_retry_count": "0.8.0",
    "reproducibility_environment.environment_failure_rate": "0.8.0",
    "reproducibility_environment.ad_hoc_manifest_mutation_fire_rate": "0.8.0",
    "reproducibility_environment.acceptance_reproducibility": "0.8.0",
    "governance.guard_fire_rate": "0.8.0",
    "governance.guard_conflict_count": "0.8.0",
    "governance.decision_acceptance_rate": "0.7.0",
    "governance.decision_rejection_rate": "0.7.0",
    "governance.rejected_materialization_suppression_rate": "0.8.0",
    "governance.repeated_warning_exposure_count": "0.8.0",
    "governance.retained_delta_count": "0.8.0",
    "governance.waiver_rate": "0.9.0",
    "governance.hard_block_rate": "0.9.0",
}


def _metric_record(key: str, value, numerator, denominator, coverage: dict,
                   provenance: str, unavailable_reason: str | None = None) -> dict:
    record = {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "coverage": coverage,
        "provenance": provenance,
        "first_measured_version": _METRIC_FIRST_MEASURED[key],
    }
    if unavailable_reason is not None:
        record["unavailable_reason"] = unavailable_reason
    return record


def _unavailable_metric(key: str, reason: str, coverage: dict,
                        *, numerator=None, denominator=None) -> dict:
    return _metric_record(
        key, None, numerator, denominator, coverage, "unavailable", reason)


def _rate_metric(key: str, numerator: int, denominator: int, coverage: dict,
                 provenance: str, empty_reason: str) -> dict:
    if denominator <= 0:
        return _unavailable_metric(
            key, empty_reason, coverage, numerator=numerator, denominator=denominator)
    return _metric_record(
        key, _ratio(numerator, denominator), numerator, denominator, coverage, provenance)


def _load_metric_inputs(in_dir: Path) -> tuple[dict[str, object], dict[str, dict], str]:
    data: dict[str, object] = {}
    coverage: dict[str, dict] = {}
    fingerprint = hashlib.sha256()
    for key, (filename, kind) in sorted(_METRIC_INPUTS.items()):
        path = in_dir / filename
        fingerprint.update(filename.encode("utf-8") + b"\0")
        if not path.is_file():
            fingerprint.update(b"<missing>\0")
            coverage[filename] = {"present": False, "valid": False, "rows": 0}
            continue
        try:
            raw = path.read_bytes()
            fingerprint.update(raw + b"\0")
            loaded = (json.loads(raw) if kind in ("json", "json-object") else [
                json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()
            ])
            expected_type = dict if kind == "json-object" else list
            if not isinstance(loaded, expected_type):
                raise ValueError("metric input has the wrong shape")
            data[key] = loaded
            coverage[filename] = {
                "present": True, "valid": True,
                "rows": len(loaded) if isinstance(loaded, list) else 1,
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            coverage[filename] = {"present": True, "valid": False, "rows": 0}
    return data, coverage, fingerprint.hexdigest()


def _load_metric_decisions(path: Path) -> tuple[list[dict], dict]:
    if not path.is_file():
        return [], {"present": False, "valid": False, "rows": 0, "rows_skipped": 0}
    rows: list[dict] = []
    skipped = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], {"present": True, "valid": False, "rows": 0, "rows_skipped": 1}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if (not isinstance(row, dict) or not isinstance(row.get("rec_id"), str)
                or row.get("decision") not in ("accept", "reject")
                or parse_iso_timestamp(row.get("at")) is None):
            skipped += 1
            continue
        rows.append({**row, "_line": line_number})
    rows.sort(key=lambda row: (
        parse_iso_timestamp(row["at"]), row["rec_id"], row["_line"]))
    return rows, {
        "present": True, "valid": True, "rows": len(rows), "rows_skipped": skipped,
    }


def _facts_lens(facts: dict, name: str) -> dict | None:
    matches = [lens for lens in facts.get("lenses") or []
               if isinstance(lens, dict) and lens.get("lens") == name]
    if (len(matches) != 1 or not isinstance(matches[0].get("per_project"), dict)
            or not isinstance(matches[0].get("rule"), str)
            or matches[0].get("provenance") not in ("observed", "inferred", "explicit")):
        return None
    return matches[0]


def _lens_metric_unavailable(key: str, lens_names: str | list[str]) -> dict:
    names = [lens_names] if isinstance(lens_names, str) else lens_names
    return _unavailable_metric(
        key, "lens-not-computed",
        {"required_lenses": names, "lenses_computed": False})


def _lens_project_rows(lens: dict) -> list[dict] | None:
    rows = list(lens["per_project"].values())
    return rows if all(isinstance(row, dict) for row in rows) else None


def _row_ints(rows: list[dict], field: str) -> list[int] | None:
    values = [row.get(field) for row in rows]
    return values if all(type(value) is int for value in values) else None


def _quality_metrics(facts: dict) -> dict[str, dict]:
    group = "quality"
    names = (
        "finding_recurrence_rate", "reopen_count", "remediation_round_burden",
        "post_acceptance_defect_rate", "severe_finding_recurrence_rate",
        "verification_finding_trend", "report_grounding_finding_trend",
    )
    lens = _facts_lens(facts, "finding_concentration")
    rows = _lens_project_rows(lens) if lens is not None else None
    if rows is None:
        return {name: _lens_metric_unavailable(
            f"{group}.{name}", "finding_concentration") for name in names}

    def totals(*fields: str) -> list[int] | None:
        values = [_row_ints(rows, field) for field in fields]
        if any(value is None for value in values):
            return None
        return [sum(value) for value in values]

    lens_coverage = {"lens": lens["lens"], "lens_rule": lens["rule"]}
    provenance = "observed" if lens["provenance"] == "explicit" else lens["provenance"]

    recurrence_values = totals(
        "finding_recurrences", "taxonomy_type_round_observations", "findings_total",
        "verified_real_typed", "unknown_type_excluded", "non_real_excluded")
    if recurrence_values is None:
        recurrence = _lens_metric_unavailable(
            f"{group}.finding_recurrence_rate", "finding_concentration")
    else:
        recurrences, observations, findings, typed, unknown, non_real = recurrence_values
        recurrence = _rate_metric(
            f"{group}.finding_recurrence_rate", recurrences, observations,
            {**lens_coverage, "findings_total": findings, "verified_real_typed": typed,
             "verified_real_unknown_type_excluded": unknown,
             "non_real_excluded": non_real,
             "taxonomy_type_round_observations": observations},
            provenance, "no verified REAL taxonomy type-round observations")

    severe_values = totals(
        "severe_finding_recurrences", "verified_severe_type_round_observations")
    if severe_values is None:
        severe_recurrence = _lens_metric_unavailable(
            f"{group}.severe_finding_recurrence_rate", "finding_concentration")
    else:
        severe_recurrences, severe_observations = severe_values
        severe_recurrence = _rate_metric(
            f"{group}.severe_finding_recurrence_rate", severe_recurrences,
            severe_observations,
            {**lens_coverage,
             "verified_severe_type_round_observations": severe_observations},
            provenance, "no verified severe taxonomy type-round observations")

    def trend(field: str, metric_name: str, coverage_name: str,
              **coverage_extra) -> dict:
        first_by_project: dict[tuple[str, str], int] = {}
        last_by_project: dict[tuple[str, str], int] = {}
        rounds = 0
        for project, row in lens["per_project"].items():
            value = row.get(field)
            if not isinstance(value, dict) or type(value.get("rounds")) is not int:
                return _lens_metric_unavailable(
                    f"{group}.{metric_name}", "finding_concentration")
            rounds += value["rounds"]
            if value["rounds"] == 0:
                continue
            if (not isinstance(value.get("first_round"), str)
                    or not isinstance(value.get("last_round"), str)
                    or type(value.get("first")) is not int
                    or type(value.get("last")) is not int):
                return _lens_metric_unavailable(
                    f"{group}.{metric_name}", "finding_concentration")
            first_by_project[(project, value["first_round"])] = value["first"]
            last_by_project[(project, value["last_round"])] = value["last"]
        coverage = {**lens_coverage, coverage_name: rounds, **coverage_extra}
        if rounds < 2:
            reason = ("fewer than two review rounds with reporting taxonomy coverage"
                      if metric_name == "report_grounding_finding_trend"
                      else "fewer than two rounds with verified verification findings")
            return _unavailable_metric(f"{group}.{metric_name}", reason, coverage)
        first_key, last_key = min(first_by_project), max(last_by_project)
        first_count, last_count = first_by_project[first_key], last_by_project[last_key]
        return _metric_record(
            f"{group}.{metric_name}",
            {"first": first_count, "last": last_count, "delta": last_count - first_count},
            last_count, first_count,
            {**coverage, "first_project": first_key[0], "first_round": first_key[1],
             "last_project": last_key[0], "last_round": last_key[1]},
            provenance)

    verification_trend = trend(
        "verification_finding_trend", "verification_finding_trend",
        "rounds_with_verification_findings")
    report_grounding_trend = trend(
        "reporting_finding_trend", "report_grounding_finding_trend",
        "rounds_with_reporting_taxonomy_coverage", taxonomy_type="reporting")

    reopen_values = totals("reopens", "finding_tasks_with_reopen_history", "findings_total")
    if reopen_values is None:
        reopen = _lens_metric_unavailable(f"{group}.reopen_count", "finding_concentration")
    else:
        reopen_total, reopen_denominator, findings = reopen_values
        coverage = {**lens_coverage, "findings_total": findings,
                    "finding_tasks_with_reopen_history": reopen_denominator}
        reopen = (_metric_record(
            f"{group}.reopen_count", reopen_total, reopen_total, reopen_denominator,
            coverage, provenance) if reopen_denominator else _unavailable_metric(
                f"{group}.reopen_count", "no finding task reopen history", coverage,
                numerator=0, denominator=0))

    remediation_values = totals(
        "remediation_rounds_total", "finding_tasks_with_remediation_history", "findings_total")
    if remediation_values is None:
        remediation = _lens_metric_unavailable(
            f"{group}.remediation_round_burden", "finding_concentration")
    else:
        remediation_rounds, remediation_denominator, findings = remediation_values
        coverage = {**lens_coverage, "findings_total": findings,
                    "finding_tasks_with_remediation_history": remediation_denominator}
        remediation = (_metric_record(
            f"{group}.remediation_round_burden",
            _ratio(remediation_rounds, remediation_denominator), remediation_rounds,
            remediation_denominator, coverage, provenance)
            if remediation_denominator else _unavailable_metric(
                f"{group}.remediation_round_burden", "no finding task remediation history",
                coverage, numerator=0, denominator=0))

    post_values = totals(
        "post_acceptance_defects", "accepted_task_type_baselines",
        "acceptance_bindings_excluded", "review_rounds_with_observed_at",
        "review_rounds_without_observed_at")
    post_sources = [row.get("post_acceptance_source_available") for row in rows]
    if post_values is None or not all(type(value) is bool for value in post_sources):
        post = _lens_metric_unavailable(
            f"{group}.post_acceptance_defect_rate", "finding_concentration")
    else:
        defects, baselines, excluded, with_time, without_time = post_values
        post_coverage = {
            **lens_coverage, "review_rounds_with_observed_at": with_time,
            "review_rounds_without_observed_at": without_time,
            "accepted_task_type_baselines": baselines,
            "acceptance_bindings_excluded": excluded,
        }
        if not all(post_sources):
            post = _unavailable_metric(
                f"{group}.post_acceptance_defect_rate", "evidence projection unavailable",
                post_coverage)
        else:
            post = _rate_metric(
                f"{group}.post_acceptance_defect_rate", defects, baselines, post_coverage,
                provenance, "no explicit accepted task/type baselines")
    return {
        "finding_recurrence_rate": recurrence,
        "severe_finding_recurrence_rate": severe_recurrence,
        "verification_finding_trend": verification_trend,
        "report_grounding_finding_trend": report_grounding_trend,
        "reopen_count": reopen,
        "remediation_round_burden": remediation,
        "post_acceptance_defect_rate": post,
    }


def _delegation_metrics(facts: dict, delegations: list[dict] | None,
                        evidence: list[dict] | None,
                        adaptive: list[dict] | None) -> dict[str, dict]:
    group = "delegation_effectiveness"
    main_lens = _facts_lens(facts, "main_direct_work")
    main_rows = _lens_project_rows(main_lens) if main_lens is not None else None
    direct_work = (_row_ints(main_rows, "direct_work") if main_rows is not None else None)
    main_sessions = (_row_ints(main_rows, "main_sessions")
                     if main_rows is not None else None)
    if direct_work is None or main_sessions is None:
        main_direct_work = _lens_metric_unavailable(
            f"{group}.main_direct_work", "main_direct_work")
    else:
        main_provenance = ("observed" if main_lens["provenance"] == "explicit"
                           else main_lens["provenance"])
        main_direct_work = _metric_record(
            f"{group}.main_direct_work", sum(direct_work), sum(direct_work),
            sum(main_sessions),
            {"main_sessions": sum(main_sessions), "lens": main_lens["lens"],
             "lens_rule": main_lens["rule"]}, main_provenance)

    context_lens = _facts_lens(facts, "context_heavy")
    context_rows = _lens_project_rows(context_lens) if context_lens is not None else None
    input_tokens = (_row_ints(context_rows, "main_input_tokens")
                    if context_rows is not None else None)
    context_main_sessions = (_row_ints(context_rows, "main_sessions")
                             if context_rows is not None else None)
    result_bytes = (_row_ints(context_rows, "main_max_result_bytes_total")
                    if context_rows is not None else None)
    if input_tokens is None or context_main_sessions is None or result_bytes is None:
        main_context_inflow = _lens_metric_unavailable(
            f"{group}.main_context_inflow", "context_heavy")
    else:
        context_provenance = ("observed" if context_lens["provenance"] == "explicit"
                              else context_lens["provenance"])
        main_context_inflow = _metric_record(
            f"{group}.main_context_inflow", sum(input_tokens), sum(input_tokens),
            sum(context_main_sessions),
            {"main_sessions": sum(context_main_sessions),
             "max_result_bytes_total": sum(result_bytes), "lens": context_lens["lens"],
             "lens_rule": context_lens["rule"]}, context_provenance)

    worker_duplicate_exploration = _unavailable_metric(
        f"{group}.worker_duplicate_exploration",
        "inter-worker overlap source unavailable",
        {"worker_tool_target_sets_available": False,
         "worker_round_binding_available": False})

    retry_lens = _facts_lens(facts, "retry_loops")
    retry_rows = _lens_project_rows(retry_lens) if retry_lens is not None else None
    worker_retry_fields = ([_row_ints(retry_rows, field) for field in (
        "worker_sessions", "worker_sessions_with_retry", "worker_retry_loops_total")]
        if retry_rows is not None else [None, None, None])
    worker_context_fields = ([_row_ints(context_rows, field) for field in (
        "worker_sessions", "worker_sessions_over_100kb",
        "worker_results_over_100kb_total", "worker_max_result_bytes")]
        if context_rows is not None else [None, None, None, None])
    if (any(values is None for values in (*worker_retry_fields, *worker_context_fields))
            or sum(worker_retry_fields[0]) != sum(worker_context_fields[0])):
        worker_retry_context_load = _lens_metric_unavailable(
            f"{group}.worker_retry_context_load", ["retry_loops", "context_heavy"])
    else:
        worker_sessions = sum(worker_retry_fields[0])
        worker_value = {
            "retry_loops": {
                "sessions_with_retry": sum(worker_retry_fields[1]),
                "retry_loops_total": sum(worker_retry_fields[2]),
            },
            "context_heavy": {
                "sessions_over_100kb": sum(worker_context_fields[1]),
                "results_over_100kb_total": sum(worker_context_fields[2]),
                "max_result_bytes": max(worker_context_fields[3], default=0),
            },
        }
        worker_retry_context_load = _metric_record(
            f"{group}.worker_retry_context_load", worker_value, worker_value,
            worker_sessions,
            {"worker_sessions": worker_sessions,
             "worker_kinds": ["subagent", "workflow_subagent"],
             "retry_lens_rule": retry_lens["rule"],
             "context_lens_rule": context_lens["rule"]}, "inferred")

    retry_totals = (_row_ints(retry_rows, "retry_loops_total")
                    if retry_rows is not None else None)
    traced_sessions = (_row_ints(retry_rows, "sessions")
                       if retry_rows is not None else None)
    if retry_totals is None or traced_sessions is None:
        blind_retry = _lens_metric_unavailable(
            f"{group}.blind_retry_count", "retry_loops")
    else:
        blind_retries = sum(retry_totals)
        blind_retry = _metric_record(
            f"{group}.blind_retry_count", blind_retries, blind_retries,
            sum(traced_sessions),
            {"sessions": sum(traced_sessions), "lens": retry_lens["lens"],
             "lens_rule": retry_lens["rule"], "signal_rule": "same-cmd-refail-v1"},
            "inferred")
    if delegations is None:
        completion = _unavailable_metric(
            f"{group}.completion_rate", "delegations projection unavailable",
            {"delegations.jsonl": "missing-or-invalid"})
    else:
        known_status = [row.get("status") for row in delegations
                        if isinstance(row, dict) and isinstance(row.get("status"), str)]
        completed = sum(status == "completed" for status in known_status)
        completion = _rate_metric(
            f"{group}.completion_rate", completed, len(known_status),
            {"delegations_total": len(delegations),
             "delegations_with_observed_status": len(known_status),
             "status_unknown_excluded": len(delegations) - len(known_status)},
            "observed", "no delegations with observed completion status")

    task_rows = _evidence_task_rows(evidence or [])
    evidence_delegations = [delegation for row in task_rows
                            for delegation in (row.get("delegations") or [])
                            if isinstance(delegation, dict)]
    known_states = [delegation.get("state") for delegation in evidence_delegations
                    if isinstance(delegation.get("state"), str)]
    useful = sum(state == "applied" for state in known_states)
    useful_coverage = {
        "evidence_available": evidence is not None,
        "delegations_total": len(evidence_delegations),
        "delegations_with_observed_state": len(known_states),
        "state_unknown_excluded": len(evidence_delegations) - len(known_states),
    }
    useful_artifact = (_rate_metric(
        f"{group}.useful_artifact_rate", useful, len(known_states), useful_coverage,
        "observed", "no delegations with observed artifact disposition")
        if evidence is not None else _unavailable_metric(
            f"{group}.useful_artifact_rate", "evidence projection unavailable", useful_coverage))

    opportunity_lens = _facts_lens(facts, "delegation_opportunity")
    opportunity_rows = (_lens_project_rows(opportunity_lens)
                        if opportunity_lens is not None else None)
    candidates_by_project = (_row_ints(opportunity_rows, "candidates")
                             if opportunity_rows is not None else None)
    if candidates_by_project is None:
        opportunity = _lens_metric_unavailable(
            f"{group}.opportunity_adjusted_useful_artifact_rate",
            "delegation_opportunity")
    elif evidence is None:
        opportunity = _unavailable_metric(
            f"{group}.opportunity_adjusted_useful_artifact_rate",
            "evidence projection unavailable",
            {"lens": opportunity_lens["lens"], "lens_rule": opportunity_lens["rule"],
             "evidence_available": False})
    else:
        candidates = sum(candidates_by_project)
        delegated_tasks = sum(bool(row.get("delegations")) for row in task_rows)
        applied_tasks = sum(any(delegation.get("state") == "applied"
                                for delegation in row.get("delegations") or [])
                            for row in task_rows)
        denominator = candidates + delegated_tasks
        opportunity = _rate_metric(
            f"{group}.opportunity_adjusted_useful_artifact_rate", applied_tasks, denominator,
            {"lens": opportunity_lens["lens"],
             "opportunity_lens_rule": opportunity_lens["rule"],
             "inferred_candidates": candidates,
             "observed_delegated_tasks": delegated_tasks,
             "lens_coverage": opportunity_lens.get("coverage", {})},
            "inferred", "no observed or inferred delegation opportunities")

    adaptive_deltas: list[dict] = []
    for row in adaptive or []:
        facts = row.get("facts") if isinstance(row, dict) else None
        if isinstance(facts, dict):
            adaptive_deltas.extend(delta for delta in facts.get("deltas") or []
                                   if isinstance(delta, dict))
    before_opportunities = sum((delta.get("before") or {}).get("opportunities", 0)
                               for delta in adaptive_deltas)
    after_opportunities = sum((delta.get("after") or {}).get("opportunities", 0)
                              for delta in adaptive_deltas)
    before_fires = sum((delta.get("before") or {}).get("fires", 0)
                       for delta in adaptive_deltas)
    after_fires = sum((delta.get("after") or {}).get("fires", 0)
                      for delta in adaptive_deltas)
    before_recurrences = sum(sum(
        value for value in ((delta.get("before") or {}).get("finding_recurrences") or {}).values()
        if type(value) is int) for delta in adaptive_deltas)
    after_recurrences = sum(sum(
        value for value in ((delta.get("after") or {}).get("finding_recurrences") or {}).values()
        if type(value) is int) for delta in adaptive_deltas)
    adaptive_coverage = {
        "adaptive_feedback_available": adaptive is not None,
        "policy_deltas": len(adaptive_deltas),
        "causal_claims": False,
        "interpretation_boundary": "firing-rate-and-finding-recurrence-trends-only",
    }
    if adaptive is None or not adaptive_deltas:
        adaptive_trend = _unavailable_metric(
            f"{group}.adaptive_before_after_trend", "adaptive feedback observations unavailable",
            adaptive_coverage)
    else:
        before_rate = (_ratio(before_fires, before_opportunities)
                       if before_opportunities else None)
        after_rate = (_ratio(after_fires, after_opportunities)
                      if after_opportunities else None)
        rate_delta = (round(after_rate - before_rate, 4)
                      if before_rate is not None and after_rate is not None else None)
        adaptive_trend = _metric_record(
            f"{group}.adaptive_before_after_trend",
            {"fire_rate": {"before": before_rate, "after": after_rate, "delta": rate_delta},
             "finding_recurrences": {"before": before_recurrences,
                                     "after": after_recurrences,
                                     "delta": after_recurrences - before_recurrences}},
            {"before_fires": before_fires, "after_fires": after_fires,
             "before_finding_recurrences": before_recurrences,
             "after_finding_recurrences": after_recurrences},
            {"before_opportunities": before_opportunities,
             "after_opportunities": after_opportunities,
             "policy_deltas": len(adaptive_deltas)},
            adaptive_coverage, "observed")
    return {
        "main_direct_work": main_direct_work,
        "main_context_inflow": main_context_inflow,
        "worker_duplicate_exploration": worker_duplicate_exploration,
        "worker_retry_context_load": worker_retry_context_load,
        "blind_retry_count": blind_retry,
        "completion_rate": completion,
        "useful_artifact_rate": useful_artifact,
        "adaptive_before_after_trend": adaptive_trend,
        "opportunity_adjusted_useful_artifact_rate": opportunity,
    }


def _warning_source_coverage(evidence: list[dict] | None,
                             warnings: list[dict] | None) -> tuple[bool, dict]:
    observations = (_evidence_coverage(evidence or []).get("warning_observations") or [])
    present_projects = sum(
        (row.get("coverage") or {}).get("warnings_file_present") is True
        for row in observations if isinstance(row, dict))
    absent_projects = sum(
        (row.get("coverage") or {}).get("warnings_file_present") is False
        for row in observations if isinstance(row, dict))
    known = warnings is not None and (present_projects > 0 or bool(warnings))
    return known, {
        "normalized_warning_rows": len(warnings or []),
        "projects_with_warning_source": present_projects,
        "projects_without_warning_source": absent_projects,
        "warning_source_coverage_known": bool(observations),
    }


def _environment_metrics(evidence: list[dict] | None,
                         warnings: list[dict] | None) -> dict[str, dict]:
    group = "reproducibility_environment"
    task_rows = _evidence_task_rows(evidence or [])
    delegations = [delegation for row in task_rows for delegation in row.get("delegations") or []
                   if isinstance(delegation, dict)]
    known_states = [delegation.get("state") for delegation in delegations
                    if isinstance(delegation.get("state"), str)]
    failed = sum(state == "failed-env" for state in known_states)
    env_coverage = {
        "evidence_available": evidence is not None,
        "delegations_total": len(delegations),
        "delegations_with_observed_state": len(known_states),
    }
    environment_failure = (_rate_metric(
        f"{group}.environment_failure_rate", failed, len(known_states), env_coverage,
        "observed", "no delegations with observed environment outcome")
        if evidence is not None else _unavailable_metric(
            f"{group}.environment_failure_rate", "evidence projection unavailable", env_coverage))

    warning_known, mutation_coverage = _warning_source_coverage(evidence, warnings)
    mutation_rows = [row for row in warnings or [] if isinstance(row, dict)
                     and row.get("event") == "evaluation"
                     and row.get("rule") == "env-manifest-mutation-v1"]
    evaluable = [row for row in mutation_rows
                 if isinstance((row.get("context") or {}).get("fired"), bool)]
    mutation_fires = sum((row.get("context") or {}).get("fired") is True for row in evaluable)
    mutation_coverage.update({
        "rule": "env-manifest-mutation-v1",
        "evaluation_rows": len(mutation_rows),
        "evaluable_rows": len(evaluable),
        "unevaluable_rows": len(mutation_rows) - len(evaluable),
    })
    if not warning_known:
        mutation = _unavailable_metric(
            f"{group}.ad_hoc_manifest_mutation_fire_rate",
            "normalized warning source unavailable", mutation_coverage)
    else:
        mutation = _rate_metric(
            f"{group}.ad_hoc_manifest_mutation_fire_rate", mutation_fires, len(evaluable),
            mutation_coverage, "observed", "no evaluable env-manifest-mutation opportunities")

    comparison_pairs = []
    for delegation in delegations:
        runs = delegation.get("verification_runs")
        if not isinstance(runs, list) or len(runs) < 2:
            continue
        valid = [run for run in runs if isinstance(run, dict)
                 and type(run.get("number")) is int
                 and isinstance(run.get("judgment_set_hash"), str)]
        valid.sort(key=lambda run: run["number"])
        comparison_pairs.extend(
            (delegation.get("did"), before["number"], after["number"],
             before["judgment_set_hash"] == after["judgment_set_hash"])
            for before, after in zip(valid, valid[1:]))
    reproducible_pairs = sum(pair[3] for pair in comparison_pairs)
    repro_coverage = {
        "verify_rerun_pairs": len(comparison_pairs),
        "delegations_with_verify_reruns": len({pair[0] for pair in comparison_pairs}),
        "comparison_source_available": evidence is not None,
        "comparison_rule": "adjacent-verifier-judgment-set-v1",
    }
    reproducibility = (_rate_metric(
        f"{group}.acceptance_reproducibility", reproducible_pairs,
        len(comparison_pairs), repro_coverage, "observed",
        "no delegation has at least two valid verify artifacts")
        if evidence is not None else _unavailable_metric(
            f"{group}.acceptance_reproducibility", "evidence projection unavailable",
            repro_coverage))
    return {
        "environment_failure_rate": environment_failure,
        "ad_hoc_manifest_mutation_fire_rate": mutation,
        "acceptance_reproducibility": reproducibility,
    }


def _governance_metrics(evidence: list[dict] | None, warnings: list[dict] | None,
                        decisions: list[dict], decision_coverage: dict,
                        adaptive: list[dict] | None) -> dict[str, dict]:
    group = "governance"
    warning_known, warning_coverage = _warning_source_coverage(evidence, warnings)
    evaluation_rows = [row for row in warnings or [] if isinstance(row, dict)
                       and row.get("event") == "evaluation"]
    evaluable = [row for row in evaluation_rows
                 if isinstance((row.get("context") or {}).get("fired"), bool)]
    fires = sum((row.get("context") or {}).get("fired") is True for row in evaluable)
    fire_coverage = {**warning_coverage, "evaluation_rows": len(evaluation_rows),
                     "evaluable_rows": len(evaluable),
                     "unevaluable_rows": len(evaluation_rows) - len(evaluable)}
    guard_fire = (_rate_metric(
        f"{group}.guard_fire_rate", fires, len(evaluable), fire_coverage,
        "observed", "no evaluable guard opportunities") if warning_known else
        _unavailable_metric(
            f"{group}.guard_fire_rate", "normalized warning source unavailable", fire_coverage))
    conflicts = sum(isinstance(row, dict) and row.get("event") == "conflict"
                    for row in warnings or [])
    conflict_coverage = {**warning_coverage, "warning_records": len(warnings or [])}
    guard_conflict = (_metric_record(
        f"{group}.guard_conflict_count", conflicts, conflicts, len(warnings or []),
        conflict_coverage, "observed") if warning_known else _unavailable_metric(
            f"{group}.guard_conflict_count", "normalized warning source unavailable",
            conflict_coverage))

    repeated_groups: Counter = Counter()
    for row in warnings or []:
        if not isinstance(row, dict) or row.get("event") != "fire":
            continue
        identity = row.get("policy_identity")
        identity_key = (_dumps(identity) if isinstance(identity, dict) else "unknown")
        repeated_groups[(str(row.get("project") or ""), str(row.get("rule") or ""),
                         identity_key)] += 1
    repeated = sum(max(count - 1, 0) for count in repeated_groups.values())
    repeated_warning = (_metric_record(
        f"{group}.repeated_warning_exposure_count", repeated, repeated,
        sum(repeated_groups.values()),
        {**warning_coverage, "distinct_warning_identities": len(repeated_groups),
         "rule": "same-rule-policy-identity-fire-v1"}, "observed")
        if warning_known else _unavailable_metric(
            f"{group}.repeated_warning_exposure_count",
            "normalized warning source unavailable", warning_coverage))

    latest: dict[str, dict] = {}
    for decision in decisions:
        latest[decision["rec_id"]] = decision
    accepted = sum(row["decision"] == "accept" for row in latest.values())
    rejected = sum(row["decision"] == "reject" for row in latest.values())
    decision_denominator = len(latest)
    decision_metric_coverage = {
        **decision_coverage, "effective_recommendations": decision_denominator,
        "history_rows_superseded": len(decisions) - decision_denominator,
    }
    if not decision_coverage["present"] or not decision_coverage["valid"]:
        decision_acceptance = _unavailable_metric(
            f"{group}.decision_acceptance_rate", "decisions log unavailable",
            decision_metric_coverage)
        decision_rejection = _unavailable_metric(
            f"{group}.decision_rejection_rate", "decisions log unavailable",
            decision_metric_coverage)
    else:
        decision_acceptance = _rate_metric(
            f"{group}.decision_acceptance_rate", accepted, decision_denominator,
            decision_metric_coverage, "observed", "no effective recommendation decisions")
        decision_rejection = _rate_metric(
            f"{group}.decision_rejection_rate", rejected, decision_denominator,
            decision_metric_coverage, "observed", "no effective recommendation decisions")

    rejected_materializations = 0
    adaptive_coverage_known = adaptive is not None and bool(adaptive)
    for row in adaptive or []:
        facts = row.get("facts") if isinstance(row, dict) else None
        coverage = facts.get("coverage") if isinstance(facts, dict) else None
        if not isinstance(coverage, dict):
            adaptive_coverage_known = False
            continue
        conflicts_by_reason = coverage.get("accept_delta_conflicts") or {}
        value = conflicts_by_reason.get("delta-from-rejected-rec", 0)
        if type(value) is int and value >= 0:
            rejected_materializations += value
        else:
            adaptive_coverage_known = False
    suppression_coverage = {
        "adaptive_feedback_available": adaptive is not None,
        "adaptive_conflict_coverage_known": adaptive_coverage_known,
        "rejected_effective_decisions": rejected,
        "rejected_materialization_conflicts": rejected_materializations,
        "causal_basis": "direct-rejection-materialization-contract",
    }
    if not adaptive_coverage_known:
        suppression = _unavailable_metric(
            f"{group}.rejected_materialization_suppression_rate",
            "adaptive rejection/materialization coverage unavailable", suppression_coverage)
    else:
        suppressed = max(rejected - rejected_materializations, 0)
        suppression = _rate_metric(
            f"{group}.rejected_materialization_suppression_rate", suppressed, rejected,
            suppression_coverage, "observed", "no rejected effective decisions")

    adaptive_deltas = [delta for row in adaptive or []
                       for delta in (((row.get("facts") or {}).get("deltas") or [])
                                     if isinstance(row, dict) else [])
                       if isinstance(delta, dict)]
    retained = sum(delta.get("status") in ("observing", "warning")
                   for delta in adaptive_deltas)
    retained_delta = (_metric_record(
        f"{group}.retained_delta_count", retained, retained, len(adaptive_deltas),
        {"adaptive_feedback_available": True, "policy_deltas": len(adaptive_deltas),
         "retained_statuses": ["observing", "warning"]}, "observed")
        if adaptive is not None else _unavailable_metric(
            f"{group}.retained_delta_count", "adaptive feedback observations unavailable",
            {"adaptive_feedback_available": False}))

    enforce_coverage = {"source_available": False, "enforce_arc_shipped": False}
    waiver = _unavailable_metric(
        f"{group}.waiver_rate", "enforce arc not shipped", enforce_coverage)
    hard_block = _unavailable_metric(
        f"{group}.hard_block_rate", "enforce arc not shipped", enforce_coverage)
    return {
        "guard_fire_rate": guard_fire,
        "guard_conflict_count": guard_conflict,
        "repeated_warning_exposure_count": repeated_warning,
        "decision_acceptance_rate": decision_acceptance,
        "decision_rejection_rate": decision_rejection,
        "rejected_materialization_suppression_rate": suppression,
        "retained_delta_count": retained_delta,
        "waiver_rate": waiver,
        "hard_block_rate": hard_block,
    }


def _load_metric_snapshots(path: Path) -> list[tuple[int, dict]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise WorkflowError(f"metrics history unreadable: {path} ({type(e).__name__})") from e
    snapshots: list[tuple[int, dict]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise WorkflowError(f"metrics history corrupt at {path}:{line_number}") from e
        if (not isinstance(row, dict) or row.get("schema") != "waystone-improve-metrics-1"
                or row.get("scope") not in (PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE)
                or not isinstance(row.get("metrics"), dict)
                or parse_iso_timestamp(row.get("at")) is None):
            raise WorkflowError(f"metrics history corrupt at {path}:{line_number}")
        snapshots.append((line_number, row))
    return snapshots


def _metric_comparison(metrics: dict, previous: tuple[int, dict] | None) -> dict:
    if previous is None:
        return {"previous_snapshot": None, "changes": {}, "causal_claims": False}
    previous_line, previous_snapshot = previous
    changes: dict[str, dict] = {}
    for group_name, group in sorted(metrics.items()):
        previous_group = previous_snapshot["metrics"].get(group_name) or {}
        for metric_name, metric in sorted(group.items()):
            current_value = metric.get("value")
            previous_value = (previous_group.get(metric_name) or {}).get("value")
            delta = None
            if (type(current_value) in (int, float)
                    and type(previous_value) in (int, float)):
                delta = round(current_value - previous_value, 4)
            changes[f"{group_name}.{metric_name}"] = {
                "previous": previous_value, "current": current_value, "delta": delta,
            }
    return {
        "previous_snapshot": {
            "at": previous_snapshot["at"], "line": previous_line,
            "input_fingerprint": previous_snapshot["input_coverage"]["fingerprint"],
        },
        "changes": changes,
        "causal_claims": False,
    }


def _metrics_facts(snapshot: dict, line_number: int) -> dict:
    available = unavailable = 0
    for group in snapshot["metrics"].values():
        for metric in group.values():
            if metric["provenance"] == "unavailable":
                unavailable += 1
            else:
                available += 1
    comparable = [
        (name, change) for name, change in sorted(snapshot["comparison"]["changes"].items())
        if type(change.get("delta")) in (int, float)
    ]
    facts = [{
        "kind": "aggregate", "available_metrics": available,
        "unavailable_metrics": unavailable,
        "positive_changes": sum(change["delta"] > 0 for _name, change in comparable),
        "negative_changes": sum(change["delta"] < 0 for _name, change in comparable),
        "unchanged": sum(change["delta"] == 0 for _name, change in comparable),
    }]
    facts.extend({"kind": "longitudinal-change", "metric": name, **change}
                 for name, change in comparable[:2])
    facts.append({"kind": "current-snapshot", "pointer": f"metrics.jsonl:{line_number}"})
    previous = snapshot["comparison"]["previous_snapshot"]
    if previous is not None:
        facts.append({"kind": "previous-snapshot",
                      "pointer": f"metrics.jsonl:{previous['line']}"})
    return {
        "schema": "waystone-improve-metrics-facts-1",
        "causal_claims": False,
        "facts": facts,
    }


def run_metrics(in_dir: Path, lens_scope: str,
                *, now: datetime | None = None) -> dict:
    if lens_scope not in (PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE):
        raise WorkflowError(f"unknown improve metrics scope: {lens_scope!r}")
    facts_path = in_dir / "facts.json"
    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"facts unavailable or corrupt: {facts_path} ({type(e).__name__})") from e
    if not isinstance(facts, dict):
        raise WorkflowError(f"facts unavailable or corrupt: {facts_path} (wrong shape)")

    data, input_coverage, fingerprint = _load_metric_inputs(in_dir)
    metric_facts = dict(facts)
    metric_facts.pop("metrics", None)
    metric_facts.pop("generated_from", None)
    fingerprint = hashlib.sha256(
        fingerprint.encode("ascii") + b"\0facts.json\0"
        + _dumps(metric_facts).encode("utf-8") + b"\0").hexdigest()
    decisions, decision_coverage = _load_metric_decisions(in_dir / "decisions.jsonl")
    input_coverage["decisions.jsonl"] = decision_coverage
    input_coverage["facts.json"] = {
        "present": True, "valid": True,
        "audit_inputs": facts.get("inputs") if isinstance(facts.get("inputs"), dict) else {},
        "skipped_lenses": len(facts.get("skipped_lenses") or []),
    }
    metrics = {
        "quality": _quality_metrics(facts),
        "delegation_effectiveness": _delegation_metrics(
            facts, data.get("delegations"), data.get("evidence"),
            data.get("adaptive_feedback")),
        "reproducibility_environment": _environment_metrics(
            data.get("evidence"), data.get("warnings")),
        "governance": _governance_metrics(
            data.get("evidence"), data.get("warnings"), decisions, decision_coverage,
            data.get("adaptive_feedback")),
    }
    measured_at = now or datetime.now(timezone.utc)
    if measured_at.tzinfo is None or measured_at.utcoffset() is None:
        raise WorkflowError("metrics measurement time must be timezone-aware")
    metrics_path = in_dir / "metrics.jsonl"
    history = _load_metric_snapshots(metrics_path)
    previous = next((entry for entry in reversed(history)
                     if entry[1]["scope"] == lens_scope), None)
    snapshot = {
        "schema": "waystone-improve-metrics-1",
        "at": measured_at.astimezone(timezone.utc).isoformat(),
        "scope": lens_scope,
        "input_coverage": {"fingerprint": fingerprint, "sources": input_coverage},
        "metrics": metrics,
    }
    snapshot["comparison"] = _metric_comparison(metrics, previous)
    line_number = len(history) + 1
    with metrics_path.open("a", encoding="utf-8") as stream:
        stream.write(_dumps(snapshot) + "\n")
    facts["metrics"] = _metrics_facts(snapshot, line_number)
    write_text_atomic(facts_path, _dumps(facts) + "\n")
    return snapshot


# ================================================================== decide (§11)
# Append-only record of the user's accept/reject on each improve recommendation. Unlike the trace/
# reviews/audit projections (byte-identical across re-runs), this is a user-action log, so an ISO-8601
# `at` timestamp is intentional — it feeds the rejection-rate metric and the next improve cycle. A
# re-decision appends a new row (history preserved; the latest row for a rec_id is the effective one).
# rec_id is minted by the skill as `<lens>/<kebab-gist>` so the same recommendation keeps a stable id
# across cycles; the pattern below enforces exactly that shape (conservative — one slash, snake_case
# lens, kebab-case gist, all lowercase).
_REC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*/[a-z0-9]+(?:-[a-z0-9]+)*$")


def run_decide(rec_id: str, decision: str, title: str | None, note: str | None, out_dir: Path) -> dict:
    record = {"rec_id": rec_id, "decision": decision, "at": datetime.now(timezone.utc).isoformat()}
    if title is not None:
        record["title"] = title
    if note is not None:
        record["note"] = note
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "decisions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(_dumps(record) + "\n")
    return record


# ------------------------------------------------------------------ CLI
def _parse_trace_args(argv: list[str]) -> tuple[list[str], set[str], str | None, str | None]:
    sources: list[str] = []
    projects: set[str] = set()
    out: str | None = None
    host: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--source", "--project", "--out", "--host"):
            if i + 1 >= len(argv):
                raise WorkflowError(f"{a} requires a value")
            val = argv[i + 1]
            if a == "--source":
                sources.append(val)
            elif a == "--project":
                projects.add(val)
            elif a == "--host":
                host = val
            else:
                out = val
            i += 2
        else:
            raise WorkflowError(f"unexpected argument {a!r}")
    if host is not None and host not in ("claude", "codex"):
        raise WorkflowError(f"--host must be claude|codex, got {host!r}")
    return sources, projects, out, host


def _parse_single_opt(argv: list[str], flag: str) -> str | None:
    """Parse a lone optional value flag (`--out`/`--in`); reject any other argument."""
    val: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == flag:
            if i + 1 >= len(argv):
                raise WorkflowError(f"{flag} requires a value")
            val = argv[i + 1]
            i += 2
        else:
            raise WorkflowError(f"unexpected argument {a!r}")
    return val


def _scope_improve_dir(project_root: Path | None, user_wide: bool) -> Path:
    if user_wide:
        return machine_dir() / "improve"
    if project_root is None:
        raise WorkflowError("project scope requires a project root")
    return project_state_path(project_root) / "improve"


def _residence_checked(value: str | None, flag: str, project_root: Path | None,
                       user_wide: bool) -> Path:
    """Resolve --out/--in inside the improve residence selected by the active scope."""
    residence = _scope_improve_dir(project_root, user_wide).resolve()
    if value is None:
        return residence
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise WorkflowError(
            f"{flag} must be an absolute path — a relative path would write behavioral evidence "
            f"outside the active improve residence: {value!r}")
    resolved = p.resolve()
    if resolved != residence and residence not in resolved.parents:
        mode = "user-wide" if user_wide else "project"
        raise WorkflowError(
            f"{flag} must stay inside the {mode} improve residence {residence}, got {resolved}")
    return resolved


def _prepare_project_output(project_root: Path | None, user_wide: bool) -> None:
    if not user_wide:
        if project_root is None:
            raise WorkflowError("project scope requires a project root")
        ensure_project_state_dir(project_root)


def _validate_improve_residences(project_root: Path) -> None:
    machine_root = machine_dir().resolve()
    project_state = project_state_path(project_root).resolve()
    machine = (machine_root / "improve").resolve()
    project = (project_state / "improve").resolve()

    def overlaps(first: Path, second: Path) -> bool:
        return first == second or first.is_relative_to(second) or second.is_relative_to(first)

    if overlaps(machine, project) or overlaps(machine_root, project_state):
        raise WorkflowError(
            f"machine improve residence {machine} and project improve residence {project} "
            f"are not isolated because WAYSTONE_HOME {machine_root} overlaps project state "
            f"{project_state}; "
            "set WAYSTONE_HOME to a directory outside the project"
        )


def _cli_trace(argv: list[str], project_root: Path | None, user_wide: bool) -> int:
    try:
        raw_sources, projects, out, explicit_host = _parse_trace_args(argv)
        if projects and not user_wide:
            raise WorkflowError("--project requires --user-wide; project mode is fixed to the current root")
        out_dir = _residence_checked(out, "--out", project_root, user_wide)
    except WorkflowError as e:
        print(f"waystone improve trace: {e}", file=sys.stderr)
        return 1

    host = explicit_host or ("codex" if os.environ.get("WAYSTONE_HOST") == "codex" else "claude")
    if not user_wide:
        projects = set()
    if raw_sources:
        sources = [Path(s).expanduser().resolve() for s in raw_sources]
        # an EXPLICITLY-named source that is not a directory is a precondition failure (exit 1); the
        # default source may legitimately be absent on a fresh machine (recorded in sources_missing)
        missing = [s for s in sources if not s.is_dir()]
        if missing:
            print("waystone improve trace: --source not a directory: "
                  + ", ".join(str(m) for m in missing), file=sys.stderr)
            return 1
    else:
        if host == "codex":
            base = os.environ.get("CODEX_HOME")
            base_path = Path(base) if base else Path.home() / ".codex"
            sources = [(base_path / "sessions").resolve()]
        else:
            base = os.environ.get("CLAUDE_CONFIG_DIR")
            base_path = Path(base) if base else Path.home() / ".claude"
            sources = [(base_path / "projects").resolve()]
    # dedupe while preserving order (a source passed twice must not double-count)
    seen: set[str] = set()
    sources = [s for s in sources if not (str(s) in seen or seen.add(str(s)))]

    try:
        _prepare_project_output(project_root, user_wide)
        cov = run_trace(
            sources, projects, out_dir, host=host,
            project_root=None if user_wide else project_root,
        )
    except WorkflowError as e:
        print(f"waystone improve trace: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve trace: cannot write outputs — {e}", file=sys.stderr)
        return 2

    print(f"waystone improve trace: {cov['row_totals']['sessions']} session(s), "
          f"{cov['row_totals']['delegations']} delegation(s) -> {out_dir}")
    return 0


def _cli_reviews(argv: list[str], project_root: Path | None, user_wide: bool) -> int:
    try:
        out = _parse_single_opt(argv, "--out")
        out_dir = _residence_checked(out, "--out", project_root, user_wide)
    except WorkflowError as e:
        print(f"waystone improve reviews: {e}", file=sys.stderr)
        return 1
    try:
        _prepare_project_output(project_root, user_wide)
        cov = run_reviews(_registry_path(), out_dir, None if user_wide else project_root)
    except WorkflowError as e:
        print(f"waystone improve reviews: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve reviews: cannot write outputs — {e}", file=sys.stderr)
        return 2
    print(f"waystone improve reviews: {cov['row_totals']['reviews']} review round(s), "
          f"{cov['row_totals']['findings']} finding(s), "
          f"{len(cov['projects_scanned'])} project(s) scanned, "
          f"{len(cov['projects_skipped'])} skipped -> {out_dir}")
    return 0


def _cli_evidence(argv: list[str], project_root: Path | None, user_wide: bool) -> int:
    try:
        _sources, projects, out, host = _parse_trace_args(argv)
        if _sources:
            raise WorkflowError("--source is not valid for evidence")
        if host is not None:
            raise WorkflowError("--host is only valid for trace")
        if projects and not user_wide:
            raise WorkflowError("--project requires --user-wide; project mode is fixed to the current root")
        out_dir = _residence_checked(out, "--out", project_root, user_wide)
    except WorkflowError as e:
        print(f"waystone improve evidence: {e}", file=sys.stderr)
        return 1
    try:
        _prepare_project_output(project_root, user_wide)
        coverage = run_evidence(
            _registry_path(), out_dir, projects, None if user_wide else project_root)
    except WorkflowError as e:
        print(f"waystone improve evidence: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve evidence: cannot write outputs — {e}", file=sys.stderr)
        return 2
    print(f"waystone improve evidence: {coverage['row_totals']['tasks']} task(s), "
          f"{len(coverage['projects_scanned'])} project(s) scanned, "
          f"{len(coverage['projects_skipped'])} skipped -> {out_dir / 'evidence.jsonl'}")
    return 0


def _cli_audit(argv: list[str], project_root: Path | None, user_wide: bool) -> int:
    try:
        inp = _parse_single_opt(argv, "--in")
        in_dir = _residence_checked(inp, "--in", project_root, user_wide)
    except WorkflowError as e:
        print(f"waystone improve audit: {e}", file=sys.stderr)
        return 1
    if not in_dir.is_dir():
        print(f"waystone improve audit: input dir does not exist: {in_dir} "
              f"(run `waystone improve trace`/`reviews` first)", file=sys.stderr)
        return 1
    try:
        _prepare_project_output(project_root, user_wide)
        scope = USER_HABIT_LENS_SCOPE if user_wide else PROJECT_LENS_SCOPE
        facts = run_audit(
            in_dir, scope, project_root=None if user_wide else project_root)
    except WorkflowError as e:
        print(f"waystone improve audit: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve audit: cannot write outputs — {e}", file=sys.stderr)
        return 2
    print(f"waystone improve audit: {len(facts['lenses'])} lens(es), "
          f"{len(facts['skipped_lenses'])} skipped -> {in_dir / 'facts.json'}")
    return 0


def _cli_metrics(argv: list[str], project_root: Path | None, user_wide: bool) -> int:
    try:
        inp = _parse_single_opt(argv, "--in")
        in_dir = _residence_checked(inp, "--in", project_root, user_wide)
        residence = _scope_improve_dir(project_root, user_wide).resolve()
        if in_dir != residence:
            raise WorkflowError(
                f"metrics history must live at {residence / 'metrics.jsonl'}, got {in_dir}")
    except WorkflowError as e:
        print(f"waystone improve metrics: {e}", file=sys.stderr)
        return 1
    if not in_dir.is_dir() or not (in_dir / "facts.json").is_file():
        print(f"waystone improve metrics: audit facts do not exist in {in_dir} "
              "(run `waystone improve audit` first)", file=sys.stderr)
        return 1
    try:
        _prepare_project_output(project_root, user_wide)
        scope = USER_HABIT_LENS_SCOPE if user_wide else PROJECT_LENS_SCOPE
        snapshot = run_metrics(in_dir, scope)
    except WorkflowError as e:
        print(f"waystone improve metrics: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve metrics: cannot write outputs — {e}", file=sys.stderr)
        return 2
    available = sum(
        metric["provenance"] != "unavailable"
        for group in snapshot["metrics"].values() for metric in group.values())
    print(f"waystone improve metrics: {available} available metric(s), "
          f"{len(_METRIC_FIRST_MEASURED) - available} unavailable "
          f"-> {in_dir / 'metrics.jsonl'}")
    return 0


def _parse_decide_args(argv: list[str]) -> tuple[str, str, str | None, str | None, str | None]:
    positionals: list[str] = []
    title = note = out = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--title", "--note", "--out"):
            if i + 1 >= len(argv):
                raise WorkflowError(f"{a} requires a value")
            val = argv[i + 1]
            if a == "--title":
                title = val
            elif a == "--note":
                note = val
            else:
                out = val
            i += 2
        elif a.startswith("--"):
            raise WorkflowError(f"unexpected argument {a!r}")
        else:
            positionals.append(a)
            i += 1
    if len(positionals) != 2:
        raise WorkflowError("expected <rec-id> accept|reject")
    rec_id, decision = positionals
    if decision not in ("accept", "reject"):
        raise WorkflowError(f"decision must be accept|reject, got {decision!r}")
    if not _REC_ID_RE.match(rec_id):
        raise WorkflowError(f"invalid rec-id {rec_id!r} (expected <lens>/<kebab-gist>)")
    return rec_id, decision, title, note, out


def _cli_decide(argv: list[str], project_root: Path | None, user_wide: bool) -> int:
    try:
        rec_id, decision, title, note, out = _parse_decide_args(argv)
        out_dir = _residence_checked(out, "--out", project_root, user_wide)
    except WorkflowError as e:
        print(f"waystone improve decide: {e}", file=sys.stderr)
        return 1
    try:
        _prepare_project_output(project_root, user_wide)
        rec = run_decide(rec_id, decision, title, note, out_dir)
    except WorkflowError as e:
        print(f"waystone improve decide: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve decide: cannot write decision — {e}", file=sys.stderr)
        return 2
    print(f"waystone improve decide: recorded {rec['decision']} for {rec['rec_id']} "
          f"-> {out_dir / 'decisions.jsonl'}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("waystone improve: expected subcommand "
              "(trace|reviews|evidence|audit|metrics|decide)\n" + __doc__,
              file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub not in ("trace", "reviews", "evidence", "audit", "metrics", "decide"):
        print(f"waystone improve: unknown subcommand {sub!r} "
              f"(expected trace|reviews|evidence|audit|metrics|decide)\n" + __doc__,
              file=sys.stderr)
        return 1
    if rest.count("--user-wide") > 1:
        print(f"waystone improve {sub}: --user-wide may be passed only once", file=sys.stderr)
        return 1
    user_wide = "--user-wide" in rest
    rest = [arg for arg in rest if arg != "--user-wide"]
    active_project_root = find_project_root(Path.cwd())
    if not user_wide and active_project_root is None:
        print(
            f"waystone improve {sub}: no waystone project found from {Path.cwd()} "
            "(run inside a project or pass --user-wide)",
            file=sys.stderr,
        )
        return 1
    if active_project_root is not None:
        try:
            _validate_improve_residences(active_project_root)
        except WorkflowError as e:
            print(f"waystone improve {sub}: {e}", file=sys.stderr)
            return 1
    project_root = None if user_wide else active_project_root
    if sub == "trace":
        return _cli_trace(rest, project_root, user_wide)
    if sub == "reviews":
        return _cli_reviews(rest, project_root, user_wide)
    if sub == "evidence":
        return _cli_evidence(rest, project_root, user_wide)
    if sub == "audit":
        return _cli_audit(rest, project_root, user_wide)
    if sub == "metrics":
        return _cli_metrics(rest, project_root, user_wide)
    if sub == "decide":
        return _cli_decide(rest, project_root, user_wide)
    raise AssertionError("unreachable improve subcommand")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
