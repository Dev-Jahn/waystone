#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Adaptive overlay store + boundary warn engine — `jw overlay` / `jw check` (0.8.0 M2).

A project-local overlay is a small set of *deltas*: machine-evaluable rules (from a fixed
vocabulary) that the harness can check at workflow boundaries (a delegation reaching needs-review,
an apply, a round close, a review ingest) and warn about — never enforce (enforce is 0.9). A delta
lives through {proposed → observing → warning → suspended/retired}: `observing` records fires
silently, `warning` also prints to stderr. Warns never change a host command's exit code (invariant
#6). Shadow replay estimates a rule's fire rate over past evidence before a delta is promoted to the
warning stage. Everything is plugin-local and never committed (invariant #10).

See dev_docs/0.8.0-m2-implementation-notes.md for the binding spec.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jw_common import (  # noqa: E402
    WorkflowError, _project_slug, find_project_root, load_config,
)

# delta-id grammar mirrors the improve rec_id (`<lens>/<kebab-gist>`, S2) so a rec materialises to a
# delta under the same id and the same recommendation keeps a stable identity across cycles.
DELTA_ID_RE = re.compile(r"^[a-z][a-z0-9_]*/[a-z0-9]+(?:-[a-z0-9]+)*$")
DELTA_STATUSES = ("proposed", "observing", "warning", "suspended", "retired")
ACTIVE_STATUSES = ("observing", "warning")
CANDIDATE_SCOPES = ("project_candidate", "user_candidate", "unresolved")


class _RefusedWrite(WorkflowError):
    """A plugin-local directory could not be created — maps to exit 2 (refused write)."""


# ---- rule vocabulary v1 (§4 — only what is machine-evaluable at a boundary) ----
RULES: dict[str, dict] = {
    "delegation-verification-evidence-v1": {
        "boundaries": {"delegate-run", "delegate-apply", "check"},
        "corpus": "delegations",
        "default_params": {},
    },
    "round-close-open-findings-v1": {
        # §6 boundary table (R4, "the single definition of evaluation targets") lists review-ingest as
        # a rule-2 target too; §4's "round-close, check" under-lists it — include it so the jw_review
        # ingest warn hook (§1) actually evaluates. Faithful minimal resolution of that inconsistency.
        "boundaries": {"round-close", "review-ingest", "check"},
        "corpus": "reviews",
        "default_params": {"severities": ["blocker", "major"]},
    },
}


def rule1_fires(contract: dict) -> bool:
    """delegation-verification-evidence-v1: fire when the delegate reported NO verification — either
    the report is absent/invalid (`present != True`) or its `verification` list is empty/absent. A
    delegate-claimed absence is a *reporting* gap, not a proof of unverified work — the warn nudges an
    independent verify before apply (§4)."""
    report = contract.get("delegate_report") or {}
    if report.get("present") is not True:
        return True
    return not report.get("verification")


def evaluate_rule2(root: Path, cfg: dict, severities, *, closing_done=frozenset(),
                   round_filter: str | None = None) -> dict:
    """round-close-open-findings-v1: finding-derived tasks (origin `review-<rid>`) whose severity is
    in `severities` and whose CURRENT registry status is outside {done, dropped} — i.e. a severe
    finding's follow-up task is still open. The two status axes are kept distinct (R3): the triage
    *verdict* (REAL/REJECTED/NEEDS-RULING) only filters out REJECTED findings; the task's *registry*
    status decides open/closed. Triage rows with no linked task are provenance-unknown — reported as
    `unlinked`, never fired (invariant #11). `closing_done` overrides the status of tasks being closed
    in the same round to `done` (evaluate against the final state). Reuses the 0.7 reviews parser."""
    import jw_improve
    severities = set(severities or [])
    closed_states = {"done", "dropped"}
    by_round = jw_improve._finding_tasks_by_round(root)

    rejected_ids: set[str] = set()
    unlinked = 0
    errors = 0
    rdir = root / cfg["reviews_dir"]
    if rdir.is_dir():
        for fb in sorted(rdir.glob("*-feedback.md")):
            rid = fb.stem[: -len("-feedback")]
            if round_filter is not None and rid != round_filter:
                continue
            try:
                text = fb.read_text(encoding="utf-8", errors="replace")
            except OSError:
                errors += 1
                continue
            for f in jw_improve._parse_triage(text):
                tid = f.get("task_id")
                if not tid:
                    unlinked += 1
                elif f.get("status") == "REJECTED":
                    rejected_ids.add(tid)

    fires: list[dict] = []
    rounds = [round_filter] if round_filter is not None else sorted(by_round)
    for rid in rounds:
        for t in by_round.get(rid, []):
            tid = t.get("id")
            sev = t.get("severity")
            status = "done" if tid in closing_done else t.get("status")
            if sev not in severities or tid in rejected_ids or status in closed_states:
                continue
            fires.append({"task_id": tid, "severity": sev, "status": status, "review_round": rid})
    return {"fires": fires, "unlinked": unlinked, "evaluation_errors": errors}


# ---- residence (§2 — plugin-local, keyed by project slug; never committed) -----
def _plugin_base() -> Path:
    return Path.home() / ".claude" / "jahns-workflow"


def _overlay_dir(root: Path) -> Path:
    return _plugin_base() / "overlay" / _project_slug(root)


def _deltas_dir(root: Path) -> Path:
    return _overlay_dir(root) / "deltas"


def _warnings_path(root: Path) -> Path:
    return _overlay_dir(root) / "warnings.jsonl"


def _delta_filename(delta_id: str) -> str:
    return delta_id.replace("/", "--") + ".json"


def _delta_path(root: Path, delta_id: str) -> Path:
    return _deltas_dir(root) / _delta_filename(delta_id)


def _mkdir_or_refuse(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise _RefusedWrite(f"cannot create plugin-local directory {path}: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- delta store (§3 — atomic per-delta JSON; strict single-record reads) ------
def _write_delta(root: Path, delta: dict) -> None:
    ddir = _deltas_dir(root)
    _mkdir_or_refuse(ddir)
    p = _delta_path(root, delta["id"])
    tmp = p.parent / (p.name + ".tmp")  # atomic: a crash mid-write must not corrupt the delta
    tmp.write_text(json.dumps(delta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def load_delta(root: Path, delta_id: str) -> dict:
    """Strict single-record read — an unknown id or corrupt file fails loud, naming the file (H3
    pattern), never an uncaught traceback."""
    p = _delta_path(root, delta_id)
    if not p.exists():
        raise WorkflowError(f"unknown delta {delta_id}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt delta file {p} ({e})")
    if not isinstance(data, dict):
        raise WorkflowError(f"corrupt delta file {p}")
    return data


def list_deltas(root: Path) -> list[dict]:
    """Lenient scan: a corrupt delta renders as {'corrupt': True, 'file': ...} rather than killing
    the whole listing (H3) — single-record verbs are the strict, file-naming paths."""
    ddir = _deltas_dir(root)
    out: list[dict] = []
    if not ddir.is_dir():
        return out
    for p in sorted(ddir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a mapping")
        except (OSError, json.JSONDecodeError, ValueError):
            out.append({"corrupt": True, "file": str(p)})
            continue
        out.append(data)
    return out


def active_deltas(root: Path) -> list[dict]:
    """Every non-corrupt delta in an active stage (observing/warning) — the boundary engine's set."""
    return [d for d in list_deltas(root) if not d.get("corrupt") and d.get("status") in ACTIVE_STATUSES]


def add_delta(root: Path, delta_id: str, *, rule: str, summary: str, pointers=None,
              expected_effect: str = "", risk: str = "", candidate_scope: str = "unresolved",
              observed_in=None, from_rec: str | None = None, title: str = "") -> dict:
    """Create a delta and immediately transition proposed → observing (S3 — the add IS the
    acceptance; improve calls it only after the user's AskUserQuestion, a manual add is itself the
    user's command). Provenance is filled from the explicit flags (S22): --from-rec records a
    decisions.jsonl rec_id reference only (it does not parse or auto-fill from that file)."""
    if not DELTA_ID_RE.match(delta_id):
        raise WorkflowError(f"invalid delta-id {delta_id!r} (expected <lens>/<kebab-gist>)")
    if rule not in RULES:
        raise WorkflowError(f"unknown rule {rule!r} (known: {', '.join(sorted(RULES))})")
    if candidate_scope not in CANDIDATE_SCOPES:
        raise WorkflowError(f"--candidate-scope must be one of {', '.join(CANDIDATE_SCOPES)}, "
                            f"got {candidate_scope!r}")
    if _delta_path(root, delta_id).exists():
        raise WorkflowError(f"delta {delta_id} already exists — suspend/retire it or use a new id")
    pslug = _project_slug(root)
    source, rec_id = ("improve-rec", from_rec) if from_rec is not None else ("manual", None)
    now = _now_iso()
    delta = {
        "schema": "jw-delta-1",
        "id": delta_id,
        "title": title or delta_id,
        "rule": rule,
        "params": dict(RULES[rule].get("default_params") or {}),
        "scope": {"pslug": pslug, "root": str(Path(root).resolve())},
        "candidate_scope": candidate_scope,
        "observed_in": list(observed_in) if observed_in else [pslug],
        "evidence": {"source": source, "rec_id": rec_id, "summary": summary,
                     "pointers": list(pointers or [])},
        "expected_effect": expected_effect,
        "risk": risk,
        "status": "observing",
        "replay": None,
        "created_at": now,
        "transitions": [{"to": "observing", "at": now, "note": "accepted via add"}],
    }
    _write_delta(root, delta)
    return delta


def _transition(root: Path, delta_id: str, to: str, *, require_from: str | None = None,
                replay_gate: bool = False, note: str | None = None) -> dict:
    delta = load_delta(root, delta_id)
    cur = delta.get("status")
    if cur == "retired":
        raise WorkflowError(f"delta {delta_id} is retired (terminal) — no further transitions")
    if require_from is not None and cur != require_from:
        raise WorkflowError(f"delta {delta_id} is {cur} — {to} requires it to be {require_from}")
    if replay_gate and not delta.get("replay"):
        raise WorkflowError(
            f"delta {delta_id} has no replay result — run `jw overlay replay {delta_id}` first")
    delta["status"] = to
    entry = {"to": to, "at": _now_iso()}
    if note:
        entry["note"] = note
    delta.setdefault("transitions", []).append(entry)
    _write_delta(root, delta)
    return delta


def promote(root: Path, delta_id: str) -> dict:
    """observing → warning; refused unless a replay result exists (S8/#6 — warn promotion is gated on
    seeing the estimated fire rate first)."""
    return _transition(root, delta_id, "warning", require_from="observing", replay_gate=True)


def demote(root: Path, delta_id: str) -> dict:
    """warning → observing (always allowed — de-escalation is never gated, #9)."""
    return _transition(root, delta_id, "observing", require_from="warning")


def suspend(root: Path, delta_id: str, note: str | None = None) -> dict:
    """any non-terminal stage → suspended (unconditional, #9)."""
    return _transition(root, delta_id, "suspended", note=note)


def retire(root: Path, delta_id: str, note: str | None = None) -> dict:
    """any non-terminal stage → retired (unconditional and final, #9)."""
    return _transition(root, delta_id, "retired", note=note)


# ---- boundary warn engine (§6 — S5/S6/S9; never blocks the host, never changes exit) ----
def _append_warning(root: Path, row: dict) -> None:
    p = _warnings_path(root)
    _mkdir_or_refuse(p.parent)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _emit(root: Path, boundary: str, delta_id: str, rule: str, delta_status: str, event: str,
          message: str, context: dict) -> dict:
    """Append a warnings.jsonl row and (only for a fire on a warning-stage delta) print to stderr.
    Observing fires and conflict/evaluation-error events are logged silently (S6)."""
    row = {"at": _now_iso(), "boundary": boundary, "delta_id": delta_id, "rule": rule,
           "delta_status": delta_status, "event": event, "message": message, "context": context}
    _append_warning(root, row)
    if event == "fire" and delta_status == "warning":
        print(f"jw warn [{delta_id}]: {message}", file=sys.stderr)
    return row


def _rule1_targets(root: Path, boundary: str, context: dict) -> tuple[list[str], list[str]]:
    """(fired_dids, error_dids) for delegation-verification-evidence-v1 at this boundary. Records
    without a contract (failed-env/-runner/-artifact) are excluded — they are not evaluable (R8)."""
    import jw_delegate
    targets: list[tuple[str, Path]] = []
    if boundary in ("delegate-run", "delegate-apply"):
        did = context.get("delegation_id")
        if did:
            rec = jw_delegate._record_dir(root, did)
            if (rec / "artifact" / "contract.yaml").exists():
                targets.append((did, rec))
    elif boundary == "check":
        for did, rec in jw_delegate._iter_delegations(root):
            st = jw_delegate._read_status_raw(rec)
            if st and st.get("state") == "needs-review" and (rec / "artifact" / "contract.yaml").exists():
                targets.append((did, rec))
    fired: list[str] = []
    errors: list[str] = []
    for did, rec in targets:
        try:
            contract = jw_delegate._load_contract(rec)
        except WorkflowError:
            errors.append(did)  # corrupt/unparseable = evaluation-error, never a fire (no invention)
            continue
        if rule1_fires(contract):
            fired.append(did)
    return fired, errors


def _rule2_at_boundary(root: Path, boundary: str, context: dict, severities) -> dict | None:
    """Evaluate round-close-open-findings-v1 for this boundary (None if the boundary carries no rule-2
    target). Config is loaded here so a config read failure surfaces as an evaluation error, not a fire."""
    cfg = load_config(root)
    if boundary == "round-close":
        return evaluate_rule2(root, cfg, severities,
                              closing_done=set(context.get("closing_task_ids") or []))
    if boundary == "review-ingest":
        return evaluate_rule2(root, cfg, severities, round_filter=context.get("round_id"))
    if boundary == "check":
        return evaluate_rule2(root, cfg, severities)
    return None


_RULE1_MSG = ("delegation {did} carries no delegate-side verification evidence — verify independently "
              "before apply (a delegate-claimed absence is a reporting gap, not proof of unverified work)")


def evaluate_boundary(root: Path, boundary: str, context: dict) -> list[dict]:
    """Evaluate active (observing/warning) deltas whose rule declares `boundary`, append fire/
    evaluation-error/conflict rows to warnings.jsonl, and (warning stage only) print fires to stderr.
    Wrapped so ANY exception is swallowed with one stderr notice — a warn-engine bug must never change
    the host command's exit or abort its flow (S5, host-exit invariant)."""
    try:
        return _evaluate_boundary(root, boundary, context)
    except Exception as e:  # noqa: BLE001 — never propagate into the host flow
        print(f"jw warn: overlay evaluation error at {boundary}: {e}", file=sys.stderr)
        return []


def _evaluate_boundary(root: Path, boundary: str, context: dict) -> list[dict]:
    relevant = [d for d in active_deltas(root)
                if boundary in RULES.get(d.get("rule"), {}).get("boundaries", set())]
    if not relevant:
        return []
    by_rule: dict[str, list[dict]] = {}
    for d in relevant:
        by_rule.setdefault(d["rule"], []).append(d)

    events: list[dict] = []
    for rule_id, group in sorted(by_rule.items()):
        # S9 least-restrictive: observing overrides warning; a representative delta carries the fire id
        observing = sorted((d for d in group if d["status"] == "observing"), key=lambda d: d["id"])
        rep = observing[0] if observing else sorted(group, key=lambda d: d["id"])[0]
        eff = "observing" if observing else "warning"
        if len(group) > 1:
            events.append(_emit(
                root, boundary, rep["id"], rule_id, eff, "conflict",
                f"{len(group)} active deltas reference {rule_id} — effective stage {eff} "
                f"(least-restrictive)", {"delta_ids": sorted(d["id"] for d in group)}))
        params = rep.get("params") or {}

        if rule_id == "delegation-verification-evidence-v1":
            fired, errors = _rule1_targets(root, boundary, context)
            for did in fired:
                events.append(_emit(root, boundary, rep["id"], rule_id, eff, "fire",
                                    _RULE1_MSG.format(did=did), {"delegation_id": did}))
            for did in errors:
                events.append(_emit(root, boundary, rep["id"], rule_id, eff, "evaluation-error",
                                    f"delegation {did} contract could not be evaluated",
                                    {"delegation_id": did}))
        elif rule_id == "round-close-open-findings-v1":
            severities = params.get("severities") or ["blocker", "major"]
            out = _rule2_at_boundary(root, boundary, context, severities)
            if out is None:
                continue
            if out["fires"]:
                desc = ", ".join(f"{f['task_id']} ({f['severity']}, review {f['review_round']})"
                                 for f in out["fires"])
                msg = f"round close leaves {len(out['fires'])} severe finding task(s) open: {desc}"
                if out["unlinked"]:
                    msg += f" · {out['unlinked']} unlinked finding(s) (provenance unknown)"
                events.append(_emit(root, boundary, rep["id"], rule_id, eff, "fire", msg,
                                    {"task_ids": [f["task_id"] for f in out["fires"]],
                                     "round_id": context.get("round_id"), "unlinked": out["unlinked"]}))
            if out["evaluation_errors"]:
                events.append(_emit(root, boundary, rep["id"], rule_id, eff, "evaluation-error",
                                    f"{out['evaluation_errors']} review file(s) could not be evaluated",
                                    {"round_id": context.get("round_id")}))
    return events


# ---- exposure (§9 — round exposure record; delegation exposure lives in jw_delegate) ----
def _exposure_dir(root: Path) -> Path:
    return _plugin_base() / "exposure" / _project_slug(root)


def _profile_summary() -> tuple[str | None, dict | None]:
    """(profile_fingerprint, {role: backend}) from the delegation profile, or (None, None) when it is
    absent — a round closes without any delegation, so the harness never guesses bindings."""
    import jw_delegate
    try:
        profile, fp = jw_delegate._load_profile()
    except WorkflowError:
        return None, None
    bindings: dict[str, str] = {}
    for role, b in (profile.get("bindings") or {}).items():
        if isinstance(b, dict) and isinstance(b.get("backend"), str):
            bindings[role] = b["backend"]
    return fp, (bindings or None)


def write_round_exposure(root: Path, round_id: str, head_sha: str | None, watermark: str | None):
    """Immutable per-round exposure record written at close (§9/#4). A re-close of the same round-id
    gets a `-2`/`-3` suffix (H4 precedent — existing records are never overwritten)."""
    fp, bindings = _profile_summary()
    exposure = {
        "schema": "jw-round-exposure-1", "round_id": round_id, "at": _now_iso(),
        "project": {"pslug": _project_slug(root), "root": str(Path(root).resolve())},
        "head_sha": head_sha, "config_watermark": watermark,
        "profile_fingerprint": fp, "bindings": bindings,
        "overlays_active": [{"id": d["id"], "status": d["status"]} for d in active_deltas(root)],
        "guards": None, "waivers": [],
    }
    edir = _exposure_dir(root)
    _mkdir_or_refuse(edir)
    p = edir / f"round-{round_id}.json"
    n = 2
    while p.exists():
        p = edir / f"round-{round_id}-{n}.json"
        n += 1
    p.write_text(json.dumps(exposure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p, exposure


# ---- CLI (hand-rolled parsing; {0,1,2} exit contract) --------------------------
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


def _cli_add(rest: list[str]) -> int:
    pos, opts = _parse_opts(
        rest, value=("rule", "summary", "expected-effect", "risk", "candidate-scope", "from-rec",
                     "title", "root"),
        repeat=("pointers", "observed-in"))
    if not pos:
        raise WorkflowError("add requires a <delta-id>")
    if not opts.get("rule"):
        raise WorkflowError("add requires --rule <rule-id>")
    if opts.get("summary") is None:
        raise WorkflowError("add requires --summary <text>")
    delta = add_delta(
        _resolve_root(opts.get("root")), pos[0], rule=opts["rule"], summary=opts["summary"],
        pointers=opts.get("pointers"), expected_effect=opts.get("expected-effect", ""),
        risk=opts.get("risk", ""), candidate_scope=opts.get("candidate-scope", "unresolved"),
        observed_in=opts.get("observed-in") or None, from_rec=opts.get("from-rec"),
        title=opts.get("title", ""))
    print(f"added delta {delta['id']} ({delta['status']})")
    return 0


def _cli_list(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    for d in list_deltas(_resolve_root(opts.get("root"))):
        if d.get("corrupt"):
            print(f"[corrupt]  {d['file']}")
        else:
            print(f"{d['id']}  [{d.get('status', '?')}]  {d.get('rule', '?')}")
    return 0


def _cli_show(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("show requires a <delta-id>")
    delta = load_delta(_resolve_root(opts.get("root")), pos[0])
    print(json.dumps(delta, ensure_ascii=False, indent=2))
    return 0


def _cli_promote(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("promote requires a <delta-id>")
    delta = promote(_resolve_root(opts.get("root")), pos[0])
    print(f"promoted {delta['id']} -> {delta['status']}")
    return 0


def _cli_demote(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("demote requires a <delta-id>")
    delta = demote(_resolve_root(opts.get("root")), pos[0])
    print(f"demoted {delta['id']} -> {delta['status']}")
    return 0


def _cli_suspend(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "note"))
    if not pos:
        raise WorkflowError("suspend requires a <delta-id>")
    delta = suspend(_resolve_root(opts.get("root")), pos[0], note=opts.get("note"))
    print(f"suspended {delta['id']}")
    return 0


def _cli_retire(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "note"))
    if not pos:
        raise WorkflowError("retire requires a <delta-id>")
    delta = retire(_resolve_root(opts.get("root")), pos[0], note=opts.get("note"))
    print(f"retired {delta['id']}")
    return 0


def _cli_check(rest: list[str]) -> int:
    """The explicit `check` boundary: evaluate every active delta against current state. Firing does
    NOT change the exit code — a successful evaluation is exit 0 even with warnings (S5)."""
    pos, opts = _parse_opts(rest, value=("root",))
    root = _resolve_root(opts.get("root"))
    events = evaluate_boundary(root, "check", {})
    fires = [e for e in events if e["event"] == "fire"]
    if not fires:
        print("jw check: no active-delta warnings")
    for e in fires:
        marker = "warn" if e["delta_status"] == "warning" else "observe"
        print(f"[{marker}] {e['rule']} [{e['delta_id']}]: {e['message']}")
    for e in (e for e in events if e["event"] == "evaluation-error"):
        print(f"[eval-error] {e['rule']}: {e['message']}")
    return 0


_HANDLERS = {"add": _cli_add, "list": _cli_list, "show": _cli_show, "promote": _cli_promote,
             "demote": _cli_demote, "suspend": _cli_suspend, "retire": _cli_retire,
             "check": _cli_check}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in _HANDLERS:
        print("jw overlay: expected subcommand "
              "(add|list|show|promote|demote|suspend|retire|replay)", file=sys.stderr)
        return 1
    try:
        return _HANDLERS[argv[0]](argv[1:])
    except _RefusedWrite as e:
        print(f"jw overlay: {e}", file=sys.stderr)
        return 2
    except WorkflowError as e:
        print(f"jw overlay: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
