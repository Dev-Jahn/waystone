#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""`jw improve` — mine Claude Code session logs into deterministic trace tables.

  jw improve trace [--source DIR]... [--project SLUG]... [--out DIR]

Discovery walks each source (default `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`),
streams every transcript file line-by-line through jw_cclog, and emits three regenerable, local-only
artifacts into --out (default `~/.claude/jahns-workflow/improve/`):
  sessions.jsonl        one row per transcript (main/subagent/workflow-subagent)
  delegations.jsonl     one row per agent_spawn tool_use
  parse_coverage.json   files-by-kind, event-type counts, unknown/skip/error tallies

Outputs are byte-identical across re-runs of the same input (no run timestamp; stable ordering;
sort_keys on every dump). Semantic labels carry provenance: rule-derived values are `inferred` with a
versioned `rule` id, unresolvable values are `{"provenance": "unknown"}`, and unmatched shell
commands are counted (never force-classified). Content (prompts/commands/file bodies) is never
stored; only bounded 120-char `head` snippets for evidence pointers.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jw_cclog import (  # noqa: E402
    PARSER_VERSION,
    SKIP_DIRS,
    TRANSCRIPT_KINDS,
    coalesce_messages,
    detect_kind,
    parse_transcript_file,
    scope_of,
    stable_id,
)
from jw_common import WorkflowError  # noqa: E402

HEAD_LEN = 120
CONTEXT_HEAVY_BYTES = 100 * 1024

# ------------------------------------------------------------------ verify-cmd-v1
# Conservative Bash-command classification. Order matters: TEST, then BUILD (so `tsc --build` is a
# build, not a typecheck), then LINT/TYPECHECK. Non-matches are counted as unclassified_shell.
_TEST_RE = re.compile(
    r"\bpytest\b|\bunittest\b|\buv run\b[^\n]*\btest|\btests/|\bnpm (?:run )?test\b|\bnpx jest\b"
    r"|\bjest\b|\bvitest\b|\bcargo test\b|\bgo test\b|\bmake (?:test|check)\b"
)
_BUILD_RE = re.compile(
    r"\bmake build\b|\bnpm run build\b|\byarn build\b|\bcargo build\b|\bgo build\b"
    r"|\btsc (?:-b\b|--build\b)"
)
_LINT_RE = re.compile(r"\bruff\b|\bflake8\b|\beslint\b")
_TYPECHECK_RE = re.compile(r"\bmypy\b|\btsc\b|\btypecheck\b|\bpyright\b|\bty check\b")


def classify_verification(command: str) -> str | None:
    """verify-cmd-v1: 'test' | 'build' | 'lint' | 'typecheck' | None (unclassified shell)."""
    if _TEST_RE.search(command):
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

    tool_err = sum(1 for tr in tool_results if tr.get("is_error"))
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
        cl = tr.get("content_len") or 0
        max_bytes = max(max_bytes, cl)
        if cl > CONTEXT_HEAVY_BYTES:
            over += 1

    row = {
        "project": scope["project"],
        "session_id": scope["session_id"],
        "kind": session_kind,
        "agent_id": scope["agent_id"],
        "workflow_id": scope["workflow_id"],
        "file": str(path),
        "agent_meta": _read_agent_meta(path, kind),
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
                         "rule": "verify-cmd-v1", "provenance": "inferred", "examples": verify_examples},
        "build": {"runs": build_runs, "failed": build_failed,
                  "rule": "verify-cmd-v1", "provenance": "inferred", "examples": build_examples},
        "unclassified_shell": unclassified,
        "retry_loops": {"count": retry_count, "rule": "same-cmd-refail-v1",
                        "provenance": "inferred", "examples": retry_examples},
        "context_heavy": {"tool_results_over_100kb": over, "max_result_bytes": max_bytes},
        "parser_version": PARSER_VERSION,
    }
    delegations = [_build_delegation(tc, result_by_tuid, scope, path, kind) for tc in spawn_calls]
    return row, delegations


# ------------------------------------------------------------------ trace driver
def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def run_trace(sources: list[Path], projects: set[str], out_dir: Path) -> dict:
    files = discover(sources, projects)
    files_by_kind: Counter = Counter(kind for _, _, kind in files)

    session_rows: list[dict] = []
    delegation_rows: list[dict] = []
    event_type_counts: Counter = Counter()
    unknown_raw_types: Counter = Counter()
    record_parse_errors = 0
    replayed_records_skipped = 0
    partial_tail_lines = 0

    for path, rel_parts, kind in files:
        if kind not in TRANSCRIPT_KINDS:
            continue  # non-transcript kinds are manifested in files_by_kind only (M1)
        scope = scope_of(rel_parts)
        session_kind = TRANSCRIPT_KINDS[kind]
        parsed = parse_transcript_file(
            path,
            file_id=stable_id("file", str(path)),
            server=None,
            project=scope["project"],
            session_id=scope["session_id"],
            agent_id=scope["agent_id"],
            workflow_id=scope["workflow_id"],
            is_sidechain_file=session_kind != "main",
        )
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
        "files_by_kind": dict(sorted(files_by_kind.items())),
        "files_skipped": sum(v for k, v in files_by_kind.items() if k.startswith("unknown_")),
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "unknown_raw_types": dict(sorted(unknown_raw_types.items())),
        "record_parse_errors": record_parse_errors,
        "replayed_records_skipped": replayed_records_skipped,
        "partial_tail_lines": partial_tail_lines,
        "row_totals": {"sessions": len(session_rows), "delegations": len(delegation_rows)},
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sessions.jsonl").write_text(
        "".join(_dumps(r) + "\n" for r in session_rows), encoding="utf-8")
    (out_dir / "delegations.jsonl").write_text(
        "".join(_dumps(r) + "\n" for r in delegation_rows), encoding="utf-8")
    (out_dir / "parse_coverage.json").write_text(_dumps(coverage) + "\n", encoding="utf-8")
    return coverage


# ------------------------------------------------------------------ CLI
def _parse_trace_args(argv: list[str]) -> tuple[list[str], set[str], str | None]:
    sources: list[str] = []
    projects: set[str] = set()
    out: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--source", "--project", "--out"):
            if i + 1 >= len(argv):
                raise WorkflowError(f"{a} requires a value")
            val = argv[i + 1]
            if a == "--source":
                sources.append(val)
            elif a == "--project":
                projects.add(val)
            else:
                out = val
            i += 2
        else:
            raise WorkflowError(f"unexpected argument {a!r}")
    return sources, projects, out


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "trace":
        print("jw improve: expected subcommand 'trace'\n" + __doc__, file=sys.stderr)
        return 1
    try:
        raw_sources, projects, out = _parse_trace_args(argv[1:])
    except WorkflowError as e:
        print(f"jw improve trace: {e}", file=sys.stderr)
        return 1

    if raw_sources:
        sources = [Path(s).expanduser().resolve() for s in raw_sources]
    else:
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        base_path = Path(base) if base else Path.home() / ".claude"
        sources = [(base_path / "projects").resolve()]
    # dedupe while preserving order (a source passed twice must not double-count)
    seen: set[str] = set()
    sources = [s for s in sources if not (str(s) in seen or seen.add(str(s)))]

    out_dir = Path(out).expanduser() if out else Path.home() / ".claude" / "jahns-workflow" / "improve"

    try:
        cov = run_trace(sources, projects, out_dir)
    except OSError as e:
        print(f"jw improve trace: cannot write outputs — {e}", file=sys.stderr)
        return 2

    print(f"jw improve trace: {cov['row_totals']['sessions']} session(s), "
          f"{cov['row_totals']['delegations']} delegation(s) -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
