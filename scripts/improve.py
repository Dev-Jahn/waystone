#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""`waystone improve` — mine Claude Code evidence into deterministic, local-only projection tables.

  waystone improve trace   [--source DIR]... [--project SLUG]... [--out DIR] [--user-wide]
  waystone improve reviews [--out DIR] [--user-wide]
  waystone improve audit   [--in DIR] [--user-wide]
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
  facts.json            8 lenses, each carrying rule id + provenance + <=5 evidence pointers;
                        missing inputs are reported in `skipped_lenses`.

`decide` appends the user's accept/reject on one recommendation to an append-only log (out dir default
= trace's --out); it is the only `improve` output that carries a timestamp (a user-action log, not a
derived projection):
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
    ensure_project_state_dir,
    find_project_root,
    has_project_config,
    load_config,
    load_tasks,
    load_yaml,
    machine_dir,
    project_state_path,
    registry_path,
)

HEAD_LEN = 120
CONTEXT_HEAVY_BYTES = 100 * 1024
PROJECT_LENS_SCOPE = "project"
USER_HABIT_LENS_SCOPE = "user-habit"

# Lens selection is mode policy, separate from the unchanged lens calculations below. Cross-scope
# lenses run over only the projection directory selected by that mode.
LENS_SCOPES = {
    "main_direct_work": frozenset({USER_HABIT_LENS_SCOPE}),
    "verification_debt": frozenset({PROJECT_LENS_SCOPE}),
    "retry_loops": frozenset({PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE}),
    "context_heavy": frozenset({PROJECT_LENS_SCOPE, USER_HABIT_LENS_SCOPE}),
    "delegation_pattern": frozenset({USER_HABIT_LENS_SCOPE}),
    "error_landscape": frozenset({PROJECT_LENS_SCOPE}),
    "review_association": frozenset({PROJECT_LENS_SCOPE}),
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


def run_trace(sources: list[Path], projects: set[str], out_dir: Path,
              host: str = "claude") -> dict:
    if host == "codex":
        return _run_codex_trace(sources, projects, out_dir)
    if host != "claude":
        raise WorkflowError(f"trace host must be claude|codex, got {host!r}")
    files = discover(sources, projects)
    files_by_kind: Counter = Counter(kind for _, _, kind in files)

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


def _run_codex_trace(sources: list[Path], projects: set[str], out_dir: Path) -> dict:
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
        files_by_kind[kind] += 1
        row, delegations = _build_session(path, kind, session_kind, scope, parsed)
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


def _parse_triage(feedback_text: str) -> list[dict]:
    """Structured findings from the appended triage table only. Severity is read from the table
    cell (explicit) or left None (unknown) — never keyword-guessed from prose. Returns
    [{id, severity, status, task_id}] where status is the triage verdict (REAL/REJECTED/NEEDS-RULING)
    or None, and task_id is the linked task-id cell (used to dedup against the finding-derived task)."""
    idx = feedback_text.rfind(_TRIAGE_HEADING)
    if idx < 0:
        return []
    out: list[dict] = []
    for line in feedback_text[idx:].splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        m = _FINDING_ID_RE.search(cells[0]) if cells else None
        if not m:
            continue  # header row, separator row, or a non-finding table line
        sev = cells[1].strip().strip("`").lower() if len(cells) > 1 else ""
        severity = sev if sev in SEVERITIES else None
        status = None
        if len(cells) > 2:
            vm = _VERDICT_RE.search(cells[2])
            status = vm.group(1).upper() if vm else None
        task_id = cells[4].strip().strip("`") if len(cells) > 4 else ""
        out.append({"id": m.group(0), "severity": severity, "status": status,
                    "task_id": task_id or None})
    return out


def _finding_tasks_by_round(root: Path) -> dict[str, list[dict]]:
    """Finding-derived tasks grouped by REVIEW round (from `origin: review-<round-id>`), read from
    tasks.yaml + tasks.archive.yaml. Severity comes structurally from the task's `severity` field."""
    docs: list[dict] = []
    try:
        docs.append(load_tasks(root))
    except (OSError, yaml.YAMLError):
        pass
    arch = root / "tasks.archive.yaml"
    if arch.is_file():
        try:
            data = load_yaml(arch)
            if isinstance(data, dict):
                docs.append(data)
        except (OSError, yaml.YAMLError):
            pass
    by_round: dict[str, list[dict]] = {}
    for doc in docs:
        for t in doc.get("tasks", []) or []:
            if not isinstance(t, dict):
                continue
            origin = t.get("origin")
            if not (isinstance(origin, str) and origin.startswith("review-")):
                continue
            rid = origin[len("review-"):]
            sev = t.get("severity")
            severity = sev if sev in SEVERITIES else None
            by_round.setdefault(rid, []).append({
                "id": t.get("id"),
                "severity": severity,
                "status": t.get("status"),
                "source": "task",
                "provenance": "explicit" if severity else "unknown",
            })
    return by_round


def _project_review_rows(name: str, root: Path, cfg: dict) -> list[dict]:
    rdir = root / cfg["reviews_dir"]
    request_files: dict[str, Path] = {}
    feedback_files: dict[str, Path] = {}
    if rdir.is_dir():
        for p in sorted(rdir.glob("*-request.md")):
            request_files[p.stem[: -len("-request")]] = p
        for p in sorted(rdir.glob("*-feedback.md")):
            feedback_files[p.stem[: -len("-feedback")]] = p
    tasks_by_round = _finding_tasks_by_round(root)
    round_ids = sorted(set(request_files) | set(feedback_files) | set(tasks_by_round))

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
                    "id": f["id"], "severity": f["severity"], "status": f["status"],
                    "source": "triage",
                    "provenance": "explicit" if f["severity"] else "unknown",
                }
                # dedup: a triage row whose task-id cell names a task that also joins via origin is
                # ONE finding — keep the triage row (triage severity), annotate its task_id, and drop
                # the separate task finding so it is not double-counted
                tid = f["task_id"]
                entry["task_id"] = tid or None
                if tid and tid in tasks_by_id:
                    referenced_task_ids.add(tid)
                findings.append(entry)
        # finding-derived tasks not referenced by any triage row remain as source "task"
        findings.extend(t for t in round_tasks if t.get("id") not in referenced_task_ids)
        findings.sort(key=lambda x: (x["source"], x["id"] or ""))
        counts = {"blocker": 0, "major": 0, "minor": 0, "unknown": 0}
        for f in findings:
            counts[f["severity"] if f["severity"] in SEVERITIES else "unknown"] += 1
        req = request_files.get(rid)
        rows.append({
            "project": name,
            "root": str(root),
            "round_id": rid,
            "request_file": str(req) if req else None,
            "feedback_file": str(fb) if fb else None,
            "findings": findings,
            "counts": counts,
        })
    return rows


def _registry_path() -> Path:
    """Runtime-resolved global registry (honours HOME so tests can override it)."""
    return registry_path()


def _project_entry(root: Path) -> dict:
    root = root.resolve()
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
        entries = [_project_entry(project_root)]
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
    for entry in entries:
        name = entry.get("name") or "(unnamed)"
        path = entry.get("path")
        if not path:  # remote-only registry entry — no local tree to read review artifacts from
            skipped.append({"project": name, "reason": "no local path (remote-only registry entry)"})
            continue
        root = Path(path).expanduser()
        if not has_project_config(root):
            skipped.append({"project": name, "reason": "project root or .waystone.yml inaccessible"})
            continue
        try:
            cfg = load_config(root)
        except (OSError, yaml.YAMLError, ValueError) as e:
            skipped.append({"project": name, "reason": f"config unreadable: {type(e).__name__}"})
            continue
        rows.extend(_project_review_rows(name, root, cfg))
        scanned.append(name)

    rows.sort(key=lambda r: (r["project"], r["round_id"]))
    coverage = {
        "generated_from": generated_from,
        "projects_total": len(entries),
        "projects_scanned": sorted(scanned),
        "projects_skipped": sorted(skipped, key=lambda s: s["project"]),
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


def _project_delegation_rows(root: Path) -> tuple[list[dict], int]:
    """Task-linked delegation evidence for one project. A missing contract is a definite absence of
    delegate-side verification evidence; corrupt exposure/status records are skipped and counted."""
    import delegate

    rows: list[dict] = []
    skipped = 0
    for did, rec in delegate._iter_delegations(root):
        try:
            exposure = delegate._load_exposure(rec)
            status = delegate._read_status_raw(rec)
        except WorkflowError:
            skipped += 1
            continue
        if not isinstance(status, dict) or not exposure.get("task_id"):
            skipped += 1
            continue
        verification_present = False
        if (rec / "artifact" / "contract.yaml").is_file():
            try:
                contract = delegate._load_contract(rec)
            except WorkflowError:
                skipped += 1
                continue
            report = contract.get("delegate_report") or {}
            verification_present = report.get("present") is True and bool(report.get("verification"))
        rows.append({
            "task_id": exposure["task_id"], "did": did, "state": status.get("state"),
            "verification_present": verification_present,
        })
    rows.sort(key=lambda r: r["did"])
    return rows, skipped


def run_evidence(registry_path: Path, out_dir: Path, projects: set[str],
                 project_root: Path | None = None) -> dict:
    """Project task-id projection joining review findings to delegation evidence. No timestamps:
    identical registry/files yield byte-identical evidence.jsonl (S10)."""
    generated_from = str(registry_path)
    if project_root is not None:
        entries = [_project_entry(project_root)]
        generated_from = str(project_root.resolve())
    else:
        entries = _registry_entries(registry_path)
    if projects:
        entries = [e for e in entries if e.get("name") in projects]
    task_rows: list[dict] = []
    scanned: list[str] = []
    skipped: list[dict] = []
    unlinked = delegation_skipped = 0
    finding_total = delegation_total = 0

    for entry in entries:
        name = entry.get("name") or "(unnamed)"
        path = entry.get("path")
        if not path:
            skipped.append({"project": name, "reason": "no local path (remote-only registry entry)"})
            continue
        root = Path(path).expanduser()
        if not has_project_config(root):
            skipped.append({"project": name, "reason": "project root or .waystone.yml inaccessible"})
            continue
        try:
            cfg = load_config(root)
            reviews = _project_review_rows(name, root, cfg)
            task_statuses = {
                t.get("id"): t.get("status")
                for t in (load_tasks(root).get("tasks") or [])
                if isinstance(t, dict) and t.get("id")
            }
        except (OSError, yaml.YAMLError, ValueError) as e:
            skipped.append({"project": name, "reason": f"project unreadable: {type(e).__name__}"})
            continue

        by_task: dict[str, dict] = {}
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
                })
                row["findings"].append({
                    "round": review.get("round_id"), "severity": finding.get("severity"),
                    "status": finding.get("status"),
                })
                finding_total += 1

        delegations, n_skipped = _project_delegation_rows(root)
        delegation_skipped += n_skipped
        for delegation in delegations:
            task_id = delegation.pop("task_id")
            row = by_task.setdefault(task_id, {
                "task_id": task_id, "project": name, "findings": [], "delegations": [],
                "join_key": "task-id", "provenance": "explicit",
                "task_status": task_statuses.get(task_id),
            })
            row["delegations"].append(delegation)
            delegation_total += 1
        for row in by_task.values():
            row["findings"].sort(
                key=lambda f: (str(f.get("round")), str(f.get("severity")), str(f.get("status"))))
            row["delegations"].sort(key=lambda x: x["did"])
            task_rows.append(row)
        scanned.append(name)

    task_rows.sort(key=lambda r: (r["project"], r["task_id"]))
    coverage = {
        "generated_from": generated_from,
        "projects_total": len(entries),
        "projects_scanned": sorted(scanned),
        "projects_skipped": sorted(skipped, key=lambda s: s["project"]),
        "unlinked_findings": unlinked,
        "delegations_skipped": delegation_skipped,
        "row_totals": {"tasks": len(task_rows), "findings": finding_total,
                       "delegations": delegation_total},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [_dumps(row) + "\n" for row in task_rows]
    lines.append(_dumps({"coverage": coverage}) + "\n")
    (out_dir / "evidence.jsonl").write_text("".join(lines), encoding="utf-8")
    return coverage


# ================================================================== audit (§9)
# Deterministic facts over the four projection artifacts ONLY (never raw logs — layer separation).
# Each lens carries a versioned rule id + provenance; <=5 examples with evidence pointers. No model
# interpretation. round<->session mapping is left provenance:"unknown" (no timestamp heuristics).
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
    d = {"lens": name, "rule": rule, "provenance": provenance,
         "per_project": per_project, "examples": examples[:5]}
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
        per[proj] = {
            "sessions": len(rows),
            "sessions_with_retry": sum(1 for s in rows if cnt(s) > 0),
            "retry_loops_total": sum(cnt(s) for s in rows),
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
    return _lens("retry_loops", "retry-loops-v1", "inferred", per, examples)


def _lens_context_heavy(sessions: list[dict]) -> dict:
    def ch(s: dict, k: str) -> int:
        return (s.get("context_heavy") or {}).get(k, 0)
    per: dict[str, dict] = {}
    for proj, rows in _by_project(sessions).items():
        per[proj] = {
            "sessions": len(rows),
            "sessions_over_100kb": sum(1 for s in rows if ch(s, "tool_results_over_100kb") > 0),
            "results_over_100kb_total": sum(ch(s, "tool_results_over_100kb") for s in rows),
            "max_result_bytes": max((ch(s, "max_result_bytes") for s in rows), default=0),
        }
    top = sorted(sessions, key=lambda s: (-ch(s, "max_result_bytes"), s.get("file") or ""))[:5]
    examples = [{"file": s.get("file"), "session_id": s.get("session_id"),
                 "max_result_bytes": ch(s, "max_result_bytes"),
                 "tool_results_over_100kb": ch(s, "tool_results_over_100kb")}
                for s in top if ch(s, "max_result_bytes") > 0]
    return _lens("context_heavy", "context-heavy-v2", "explicit", per, examples)


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
            open_severe = (
                row.get("task_status") is not None
                and row["task_status"] not in ("done", "dropped")
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
        }
    return _lens(
        "evidence_link", "evidence-link-v1", "explicit", per, examples,
        round_session_mapping={"provenance": "unknown"})


def run_audit(in_dir: Path, lens_scope: str | None = None) -> dict:
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

    lens_specs = [
        ("main_direct_work", "sessions", lambda: _lens_main_direct_work(data["sessions"])),
        ("verification_debt", "sessions", lambda: _lens_verification_debt(data["sessions"])),
        ("retry_loops", "sessions", lambda: _lens_retry_loops(data["sessions"])),
        ("context_heavy", "sessions", lambda: _lens_context_heavy(data["sessions"])),
        ("delegation_pattern", "delegations", lambda: _lens_delegation_pattern(data["delegations"])),
        ("error_landscape", "sessions", lambda: _lens_error_landscape(data["sessions"])),
        ("review_association", "reviews", lambda: _lens_review_association(data["reviews"])),
        ("coverage_caveats", "parse_coverage", lambda: _lens_coverage_caveats(data["parse_coverage"])),
    ]
    if lens_scope is not None:
        lens_specs = [spec for spec in lens_specs if lens_scope in LENS_SCOPES[spec[0]]]
    lenses: list[dict] = []
    skipped: list[dict] = []
    for name, req, builder in lens_specs:
        if present.get(req):
            lenses.append(builder())
        else:
            skipped.append({"lens": name, "reason": f"missing {_AUDIT_INPUTS[req]}"})
    evidence_path = in_dir / "evidence.jsonl"
    if (lens_scope is None or lens_scope in LENS_SCOPES["evidence_link"]) and evidence_path.is_file():
        try:
            evidence_rows = _load_jsonl(evidence_path)
        except (OSError, json.JSONDecodeError):
            evidence_rows = None
        if evidence_rows is not None:
            lenses.append(_lens_evidence_link(evidence_rows))
            present["evidence"] = True
    lenses.sort(key=lambda x: x["lens"])
    skipped.sort(key=lambda x: x["lens"])

    facts = {
        "generated_from": str(in_dir),
        "inputs": {k: present.get(k, False) for k in (*_AUDIT_INPUTS, *(["evidence"] if
                   present.get("evidence") else []))},
        "skipped_lenses": skipped,
        "lenses": lenses,
    }
    if lens_scope is not None:
        facts["scope"] = lens_scope
    (in_dir / "facts.json").write_text(_dumps(facts) + "\n", encoding="utf-8")
    return facts


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


def _trace_project(root: Path, host: str) -> str:
    if host == "codex":
        return root.resolve().name
    return re.sub(r"[^A-Za-z0-9]", "-", str(root.resolve()))


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
        projects = {_trace_project(project_root, host)}
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
        cov = run_trace(sources, projects, out_dir, host=host)
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
        facts = run_audit(in_dir, scope)
    except WorkflowError as e:
        print(f"waystone improve audit: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone improve audit: cannot write outputs — {e}", file=sys.stderr)
        return 2
    print(f"waystone improve audit: {len(facts['lenses'])} lens(es), "
          f"{len(facts['skipped_lenses'])} skipped -> {in_dir / 'facts.json'}")
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
        print("waystone improve: expected subcommand (trace|reviews|evidence|audit|decide)\n" + __doc__,
              file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub not in ("trace", "reviews", "evidence", "audit", "decide"):
        print(f"waystone improve: unknown subcommand {sub!r} "
              f"(expected trace|reviews|evidence|audit|decide)\n" + __doc__,
              file=sys.stderr)
        return 1
    if rest.count("--user-wide") > 1:
        print(f"waystone improve {sub}: --user-wide may be passed only once", file=sys.stderr)
        return 1
    user_wide = "--user-wide" in rest
    rest = [arg for arg in rest if arg != "--user-wide"]
    project_root = None if user_wide else find_project_root(Path.cwd())
    if not user_wide and project_root is None:
        print(
            f"waystone improve {sub}: no waystone project found from {Path.cwd()} "
            "(run inside a project or pass --user-wide)",
            file=sys.stderr,
        )
        return 1
    if sub == "trace":
        return _cli_trace(rest, project_root, user_wide)
    if sub == "reviews":
        return _cli_reviews(rest, project_root, user_wide)
    if sub == "evidence":
        return _cli_evidence(rest, project_root, user_wide)
    if sub == "audit":
        return _cli_audit(rest, project_root, user_wide)
    if sub == "decide":
        return _cli_decide(rest, project_root, user_wide)
    raise AssertionError("unreachable improve subcommand")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
