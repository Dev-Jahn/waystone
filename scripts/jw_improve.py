#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""`jw improve` — mine Claude Code evidence into deterministic, local-only projection tables.

  jw improve trace   [--source DIR]... [--project SLUG]... [--out DIR]
  jw improve reviews [--out DIR]
  jw improve audit   [--in DIR]

`trace` walks each source (default `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`),
streams every transcript file line-by-line through jw_cclog, and emits three regenerable artifacts
into --out (default `~/.claude/jahns-workflow/improve/`):
  sessions.jsonl        one row per transcript (main/subagent/workflow-subagent)
  delegations.jsonl     one row per agent_spawn tool_use
  parse_coverage.json   files-by-kind, event-type counts, unknown/skip/error tallies

`reviews` reads the registered projects (`~/.claude/jahns-workflow/projects.json`), resolves each
`reviews_dir` via `.jahns-workflow.yml`, and projects the review evidence already on disk (it never
re-implements review ingest) into --out:
  reviews.jsonl         one row per review round (findings from the feedback triage table + the
                        finding-derived tasks joined by their `origin: review-<round-id>`)
  reviews_coverage.json projects scanned / skipped (inaccessible roots are reported, not fatal)

`audit` reads ONLY the four projection artifacts above (never raw logs) from --in (default = trace's
--out) and emits deterministic per-lens facts (no model interpretation — that is the skill's job):
  facts.json            8 lenses, each carrying rule id + provenance + <=5 evidence pointers;
                        missing inputs are reported in `skipped_lenses`.

Outputs are byte-identical across re-runs of the same input (no run timestamp; stable ordering;
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

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
from jw_common import (  # noqa: E402
    CONFIG_NAME,
    SEVERITIES,
    WorkflowError,
    load_config,
    load_tasks,
    load_yaml,
)

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


# ================================================================== reviews (§8)
# Project review evidence, projected — NOT re-implemented. The on-disk feedback format is exactly
# what `jw_review.ingest` writes: a metadata header, the byte-exact reviewer body, then an APPENDED
# markdown triage table under `## Findings (triage skeleton …)` whose rows are
#   | JW-GPT-NNN — title | <severity> | <verdict> | <evidence> | <task id> |
# We parse ONLY that appended table (the last such heading), never the verbatim body (§3.8: no
# defensive multi-format guessing). Finding-derived tasks carry the review-round link in their
# `origin` field (`review-<round-id>`), set by `jw task add --origin` in skills/review/SKILL.md — the
# `round` field records the FIXING round (stamped at close), so origin is the correct join key.
_TRIAGE_HEADING = "## Findings (triage skeleton"
_FINDING_ID_RE = re.compile(r"JW-GPT-\d+")
_VERDICT_RE = re.compile(r"\b(REAL|REJECTED|NEEDS-RULING)\b", re.IGNORECASE)


def _parse_triage(feedback_text: str) -> list[dict]:
    """Structured findings from the appended triage table only. Severity is read from the table
    cell (explicit) or left None (unknown) — never keyword-guessed from prose. Returns
    [{id, severity, status}] where status is the triage verdict (REAL/REJECTED/NEEDS-RULING) or None."""
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
        out.append({"id": m.group(0), "severity": severity, "status": status})
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
        fb = feedback_files.get(rid)
        if fb is not None:
            try:
                text = fb.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for f in _parse_triage(text):
                findings.append({
                    "id": f["id"], "severity": f["severity"], "status": f["status"],
                    "source": "triage",
                    "provenance": "explicit" if f["severity"] else "unknown",
                })
        findings.extend(tasks_by_round.get(rid, []))
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
    """Runtime-resolved global registry (honours HOME so tests can override it), matching
    jw_common.REGISTRY_PATH's location without freezing it at import time."""
    return Path.home() / ".claude" / "jahns-workflow" / "projects.json"


def run_reviews(registry_path: Path, out_dir: Path) -> dict:
    entries: list = []
    if registry_path.is_file():
        try:
            reg = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(reg, dict):
                entries = [e for e in reg.get("projects", []) if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError):
            entries = []

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
        if not (root / CONFIG_NAME).is_file():
            skipped.append({"project": name, "reason": "project root or .jahns-workflow.yml inaccessible"})
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
        "generated_from": str(registry_path),
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
    per: dict[str, dict] = {}
    for proj, rows in _by_project(sessions).items():
        fw_rows = [s for s in rows if _cat(s, "file_write") > 0]
        debt = [s for s in fw_rows if runs(s) == 0]
        per[proj] = {
            "sessions": len(rows),
            "file_write_sessions": len(fw_rows),
            "debt_sessions": len(debt),
            "debt_ratio": _ratio(len(debt), len(fw_rows)),
            "unclassified_shell_total": sum(s.get("unclassified_shell", 0) for s in rows),
        }
    debt_all = [s for s in sessions if _cat(s, "file_write") > 0 and runs(s) == 0]
    top = sorted(debt_all, key=lambda s: (-_cat(s, "file_write"), s.get("file") or ""))[:5]
    examples = [{"file": s.get("file"), "session_id": s.get("session_id"),
                 "file_write": _cat(s, "file_write")} for s in top]
    return _lens("verification_debt", "verification-debt-v1", "inferred", per, examples)


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
    return _lens("context_heavy", "context-heavy-v1", "explicit", per, examples)


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
        async_count = sum(1 for r in rows if r.get("is_async") is True)
        per[proj] = {
            "delegations": len(rows),
            "by_tool": dict(sorted(by_tool.items())),
            "by_subagent_type": dict(sorted(Counter(_deleg_val(r.get("subagent_type")) for r in rows).items())),
            "by_model_requested": dict(sorted(Counter(_deleg_val(r.get("model_requested")) for r in rows).items())),
            "by_resolved_model": dict(sorted(Counter(_deleg_val(r.get("resolved_model")) for r in rows).items())),
            "by_status": dict(sorted(Counter(_deleg_val(r.get("status")) for r in rows).items())),
            "async_count": async_count,
            "async_ratio": _ratio(async_count, len(rows)),
            "workflow_delegations": by_tool.get("Workflow", 0),
        }
    top = sorted(delegations,
                 key=lambda r: (r.get("project") or "", r.get("file") or "", r.get("line") or 0))[:5]
    examples = [{"file": r.get("file"), "line": r.get("line"), "session_id": r.get("session_id"),
                 "subagent_type": r.get("subagent_type"), "model_requested": r.get("model_requested"),
                 "resolved_model": _deleg_val(r.get("resolved_model")),
                 "status": _deleg_val(r.get("status"))} for r in top]
    return _lens("delegation_pattern", "delegation-pattern-v1", "explicit", per, examples)


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
    return _lens("review_association", "review-association-v1", "explicit", per, examples)


def _lens_coverage_caveats(coverage: dict) -> dict:
    summary = {
        "parser_version": coverage.get("parser_version"),
        "files_skipped": coverage.get("files_skipped", 0),
        "record_parse_errors": coverage.get("record_parse_errors", 0),
        "replayed_records_skipped": coverage.get("replayed_records_skipped", 0),
        "partial_tail_lines": coverage.get("partial_tail_lines", 0),
        "unknown_raw_types": coverage.get("unknown_raw_types", {}),
        "row_totals": coverage.get("row_totals", {}),
    }
    return _lens("coverage_caveats", "coverage-caveats-v1", "explicit", {}, [], summary=summary)


def run_audit(in_dir: Path) -> dict:
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
    lenses: list[dict] = []
    skipped: list[dict] = []
    for name, req, builder in lens_specs:
        if present.get(req):
            lenses.append(builder())
        else:
            skipped.append({"lens": name, "reason": f"missing {_AUDIT_INPUTS[req]}"})
    lenses.sort(key=lambda x: x["lens"])
    skipped.sort(key=lambda x: x["lens"])

    facts = {
        "generated_from": str(in_dir),
        "inputs": {k: present.get(k, False) for k in _AUDIT_INPUTS},
        "skipped_lenses": skipped,
        "lenses": lenses,
    }
    (in_dir / "facts.json").write_text(_dumps(facts) + "\n", encoding="utf-8")
    return facts


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


def _default_out() -> Path:
    return Path.home() / ".claude" / "jahns-workflow" / "improve"


def _cli_trace(argv: list[str]) -> int:
    try:
        raw_sources, projects, out = _parse_trace_args(argv)
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

    out_dir = Path(out).expanduser() if out else _default_out()

    try:
        cov = run_trace(sources, projects, out_dir)
    except OSError as e:
        print(f"jw improve trace: cannot write outputs — {e}", file=sys.stderr)
        return 2

    print(f"jw improve trace: {cov['row_totals']['sessions']} session(s), "
          f"{cov['row_totals']['delegations']} delegation(s) -> {out_dir}")
    return 0


def _cli_reviews(argv: list[str]) -> int:
    try:
        out = _parse_single_opt(argv, "--out")
    except WorkflowError as e:
        print(f"jw improve reviews: {e}", file=sys.stderr)
        return 1
    out_dir = Path(out).expanduser() if out else _default_out()
    try:
        cov = run_reviews(_registry_path(), out_dir)
    except OSError as e:
        print(f"jw improve reviews: cannot write outputs — {e}", file=sys.stderr)
        return 2
    print(f"jw improve reviews: {cov['row_totals']['reviews']} review round(s), "
          f"{cov['row_totals']['findings']} finding(s), "
          f"{len(cov['projects_scanned'])} project(s) scanned, "
          f"{len(cov['projects_skipped'])} skipped -> {out_dir}")
    return 0


def _cli_audit(argv: list[str]) -> int:
    try:
        inp = _parse_single_opt(argv, "--in")
    except WorkflowError as e:
        print(f"jw improve audit: {e}", file=sys.stderr)
        return 1
    in_dir = Path(inp).expanduser() if inp else _default_out()
    if not in_dir.is_dir():
        print(f"jw improve audit: input dir does not exist: {in_dir} "
              f"(run `jw improve trace`/`reviews` first)", file=sys.stderr)
        return 1
    try:
        facts = run_audit(in_dir)
    except OSError as e:
        print(f"jw improve audit: cannot write outputs — {e}", file=sys.stderr)
        return 2
    print(f"jw improve audit: {len(facts['lenses'])} lens(es), "
          f"{len(facts['skipped_lenses'])} skipped -> {in_dir / 'facts.json'}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("jw improve: expected subcommand (trace|reviews|audit)\n" + __doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub == "trace":
        return _cli_trace(rest)
    if sub == "reviews":
        return _cli_reviews(rest)
    if sub == "audit":
        return _cli_audit(rest)
    print(f"jw improve: unknown subcommand {sub!r} (expected trace|reviews|audit)\n" + __doc__,
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
