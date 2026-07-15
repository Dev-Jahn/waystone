#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Four-layer adaptive policy store + boundary warn engine — `waystone overlay` / `waystone check`.

Machine-evaluable rules are composed across built-in base, user, project, and current-round layers.
The runtime supports observing and warning only; enforce remains vocabulary for the next arc and is
unreachable here. Local deltas remain private unless an explicit, consent-gated materialization
writes the commit-target project policy. Boundary warnings never change a host command's exit code.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    ROUND_RE, WorkflowError, _project_slug, ensure_project_state_dir, find_project_root,
    has_accepted_consent, hold_lock, load_config, machine_dir, migrate_project_state,
    project_lock_path, project_state_path, record_consent, write_text_atomic,
)

# delta-id grammar mirrors the improve rec_id (`<lens>/<kebab-gist>`, S2) so a rec materialises to a
# delta under the same id and the same recommendation keeps a stable identity across cycles.
DELTA_ID_RE = re.compile(r"^[a-z][a-z0-9_]*/[a-z0-9]+(?:-[a-z0-9]+)*$")
# R6: `add` is the acceptance event, so an extra `accepted` lifecycle state is intentionally absent.
DELTA_STATUSES = ("proposed", "observing", "warning", "suspended", "retired")
ACTIVE_STATUSES = ("observing", "warning")
CANDIDATE_SCOPES = ("project_candidate", "user_candidate", "unresolved")
POLICY_STAGES = ("observing", "warning", "enforce")
RUNTIME_POLICY_STAGES = ("observing", "warning")
PROJECT_POLICY_SCHEMA = "waystone-project-policy-1"
DELTA_SCHEMAS = ("waystone-delta-1", "jw-delta-1")


class _RefusedWrite(WorkflowError):
    """A plugin-local directory could not be created — maps to exit 2 (refused write)."""


# ---- rule vocabulary v1 (§4 — only what is machine-evaluable at a boundary) ----
RULES: dict[str, dict] = {
    "delegation-verification-evidence-v1": {
        "boundaries": {"delegate-run", "delegate-apply", "check"},
        "corpus": "delegations",
        "default_params": {},
        "finding_types": ["verification"],
    },
    "round-close-open-findings-v1": {
        # §6 boundary table (R4, "the single definition of evaluation targets") lists review-ingest as
        # a rule-2 target too; §4's "round-close, check" under-lists it — include it so the review
        # ingest warn hook (§1) actually evaluates. Faithful minimal resolution of that inconsistency.
        "boundaries": {"round-close", "review-ingest", "check"},
        "corpus": "reviews",
        "default_params": {"severities": ["blocker", "major"]},
        "finding_types": [
            "architecture", "correctness", "reporting", "reproducibility", "scope", "verification",
        ],
    },
    "delegation-scope-drift-v1": {
        "boundaries": {"delegate-run", "delegate-apply", "check"},
        "corpus": "delegations",
        "default_params": {},
        "finding_types": ["scope"],
    },
    "env-manifest-mutation-v1": {
        "boundaries": {"round-close", "check"},
        "corpus": "rounds",
        "default_params": {},
        "finding_types": ["reproducibility"],
    },
    "review-skipped-closes-v1": {
        "boundaries": {"round-close", "check"},
        "corpus": "rounds",
        "default_params": {"consecutive": 2},
        "finding_types": [
            "architecture", "correctness", "reporting", "reproducibility", "scope", "verification",
        ],
    },
    "done-without-evidence-v1": {
        "boundaries": {"round-close", "check"},
        "corpus": "rounds",
        "default_params": {},
        "finding_types": ["verification"],
    },
}

_MANIFEST_NAMES = frozenset({
    "Cargo.lock", "Cargo.toml", "Gemfile", "Gemfile.lock", "Pipfile", "Pipfile.lock",
    "bun.lock", "bun.lockb", "go.mod", "go.sum", "package-lock.json", "package.json",
    "pnpm-lock.yaml", "poetry.lock", "pyproject.toml", "uv.lock", "yarn.lock",
})
_REQUIREMENTS_RE = re.compile(r"^requirements[^/]*\.txt$")


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
    import improve
    severities = set(severities or [])
    closed_states = {"done", "dropped"}
    by_round = improve._finding_tasks_by_round(root)

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
            for f in improve._parse_triage(text):
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


def _round_payload(round_record: dict) -> dict:
    payload = round_record.get("round_evidence")
    return payload if isinstance(payload, dict) else round_record


def _is_dependency_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _MANIFEST_NAMES or _REQUIREMENTS_RE.fullmatch(name) is not None


def evaluate_env_manifest_mutation(round_record: dict) -> dict:
    """Return unapproved dependency-manifest paths from one immutable round observation.

    A manifest is accounted for only by an env_prep change in the same commit interval or by a
    structured task scope that contains the path. Natural-language task fields are never mined.
    """
    from common import _path_in_declared_scope

    payload = _round_payload(round_record)
    manifests = sorted({path for path in (payload.get("manifest_paths") or [])
                        if isinstance(path, str) and _is_dependency_manifest(path)})
    if payload.get("evaluable") is not True:
        return {"evaluable": False, "fires": [], "manifest_paths": manifests,
                "coverage_reason": payload.get("coverage_reason") or "round-diff-unavailable"}
    scopes = payload.get("task_scopes") if isinstance(payload.get("task_scopes"), dict) else {}
    referenced = {
        path for path in manifests
        if any(isinstance(prefixes, list) and _path_in_declared_scope(path, prefixes)
               for prefixes in scopes.values())
    }
    fires = [] if payload.get("env_prep_changed") is True else sorted(set(manifests) - referenced)
    return {
        "evaluable": True, "fires": fires, "manifest_paths": manifests,
        "referenced_manifest_paths": sorted(referenced),
        "env_prep_changed": payload.get("env_prep_changed") is True,
        "coverage_reason": None,
    }


def evaluate_done_without_evidence(round_record: dict) -> dict:
    """Find tasks that transitioned to done without any of the three recorded evidence signals."""
    payload = _round_payload(round_record)
    done_ids = sorted({task_id for task_id in (payload.get("done_task_ids") or [])
                       if isinstance(task_id, str)})
    rows = payload.get("done_evidence") if isinstance(payload.get("done_evidence"), list) else []
    by_task = {row.get("task_id"): row for row in rows if isinstance(row, dict)}
    unknown = [task_id for task_id in done_ids
               if by_task.get(task_id, {}).get("evaluation_errors", 0)]
    fires = [task_id for task_id in done_ids
             if task_id not in unknown
             if not any(by_task.get(task_id, {}).get(kind) is True
                        for kind in ("verification", "verify", "verdict"))]
    return {"evaluable": True, "fires": fires, "done_task_ids": done_ids,
            "evidence_rows": len(by_task), "unknown_task_ids": unknown,
            "evaluation_errors": sum(by_task.get(task_id, {}).get("evaluation_errors", 0)
                                     for task_id in done_ids)}


def evaluate_review_skipped_closes(rounds: list[dict], ingests: list[dict], *,
                                   consecutive: int = 2) -> dict:
    """Deterministic approximation of close streaks without an intervening review ingest."""
    if type(consecutive) is not int or consecutive < 1:
        raise WorkflowError("review-skipped-closes-v1 consecutive must be a positive integer")
    closes = sorted(
        (row for row in rounds if isinstance(row, dict)
         and isinstance(row.get("round_id"), str) and isinstance(row.get("at"), str)),
        key=lambda row: (row["at"], row["round_id"], row.get("_file") or ""),
    )
    review_events = sorted(
        (row for row in ingests if isinstance(row, dict) and isinstance(row.get("at"), str)),
        key=lambda row: (row["at"], row.get("round_id") or "", row.get("source_pointer") or ""),
    )
    event_index = 0
    streak = 0
    fires: list[str] = []
    by_round: list[dict] = []
    for close in closes:
        saw_ingest = False
        while event_index < len(review_events) and review_events[event_index]["at"] <= close["at"]:
            saw_ingest = True
            event_index += 1
        streak = 1 if saw_ingest else streak + 1
        fired = streak >= consecutive
        if fired:
            fires.append(close["round_id"])
        by_round.append({"round_id": close["round_id"], "streak": streak, "fired": fired})
    return {"opportunities": len(closes), "fires": fires, "by_round": by_round,
            "consecutive": consecutive}


# ---- residence (§2 — project-local, never committed) --------------------------
def _overlay_dir(root: Path) -> Path:
    return project_state_path(root) / "overlay"


def _deltas_dir(root: Path) -> Path:
    return _overlay_dir(root) / "deltas"


def _user_overlay_dir() -> Path:
    """The host-neutral user policy layer required by the four-layer model."""
    return machine_dir() / "overlay"


def _user_deltas_dir() -> Path:
    return _user_overlay_dir() / "deltas"


def _user_delta_path(delta_id: str) -> Path:
    return _user_deltas_dir() / _delta_filename(delta_id)


def _round_override_path(root: Path) -> Path:
    return _overlay_dir(root) / "round-override.json"


def _project_policy_path(root: Path) -> Path:
    return Path(root) / "docs" / "waystone-policy.yaml"


def _warnings_path(root: Path) -> Path:
    return _overlay_dir(root) / "warnings.jsonl"


def _review_ingests_path(root: Path) -> Path:
    return _overlay_dir(root) / "review-ingests.jsonl"


def _delta_filename(delta_id: str) -> str:
    return delta_id.replace("/", "--") + ".json"


def _delta_path(root: Path, delta_id: str) -> Path:
    return _deltas_dir(root) / _delta_filename(delta_id)


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


def record_review_ingest(root: Path, round_id: str) -> dict:
    """Append the ingest boundary needed to replay no-review close streaks."""
    if not isinstance(round_id, str) or not round_id:
        raise WorkflowError("review ingest round_id must be non-empty")
    row = {"at": _now_iso(), "round_id": round_id, "provenance": "observed"}
    _ensure_project_state_or_refuse(root)
    path = _review_ingests_path(root)
    _mkdir_or_refuse(path.parent)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def load_review_ingests(root: Path) -> tuple[list[dict], int]:
    path = _review_ingests_path(root)
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
        if (not isinstance(row, dict) or not isinstance(row.get("at"), str)
                or not isinstance(row.get("round_id"), str)):
            skipped += 1
            continue
        rows.append({**row, "source_pointer": f"{path}:{line_number}"})
    rows.sort(key=lambda row: (row["at"], row["round_id"], row["source_pointer"]))
    return rows, skipped


def _review_ingests_for_rounds(root: Path, rounds: list[dict]) -> tuple[list[dict], int, int]:
    """Combine timestamped new events with a labeled approximation for pre-L2-C feedback files."""
    rows, errors = load_review_ingests(root)
    explicit_rounds = {row["round_id"] for row in rows}
    if not (root / ".waystone.yml").is_file():
        return rows, errors, 0
    try:
        cfg = load_config(root)
        review_dir = root / cfg["reviews_dir"]
        feedback_rounds = sorted(
            path.stem[: -len("-feedback")] for path in review_dir.glob("*-feedback.md"))
    except (OSError, WorkflowError, KeyError):
        return rows, errors + 1, 0
    chronological = sorted(rounds, key=lambda row: (
        row.get("at") or "", row.get("round_id") or "", row.get("_file") or ""))
    legacy = 0
    for feedback_round in feedback_rounds:
        if feedback_round in explicit_rounds:
            continue
        positions = [index for index, close in enumerate(chronological)
                     if close.get("round_id") == feedback_round]
        if not positions:
            continue
        next_index = positions[-1] + 1
        if next_index >= len(chronological):
            continue
        rows.append({
            "round_id": feedback_round, "at": chronological[next_index]["at"],
            "provenance": "feedback-file-between-close-approximation",
            "source_pointer": str(review_dir / f"{feedback_round}-feedback.md"),
        })
        legacy += 1
    rows.sort(key=lambda row: (row["at"], row["round_id"], row.get("source_pointer") or ""))
    return rows, errors, legacy


# ---- delta store (§3 — atomic per-delta JSON; strict single-record reads) ------
def _write_delta(root: Path, delta: dict) -> None:
    _ensure_project_state_or_refuse(root)
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


def active_deltas_for_exposure(root: Path) -> list[dict]:
    """Strict active-delta scan for immutable exposure capture; one corrupt record fails the run."""
    ddir = _deltas_dir(root)
    out: list[dict] = []
    if not ddir.is_dir():
        return out
    for p in sorted(ddir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise WorkflowError(f"corrupt delta file {p} ({e})")
        if (not isinstance(data, dict) or data.get("schema") not in DELTA_SCHEMAS
                or not isinstance(data.get("id"), str)
                or DELTA_ID_RE.fullmatch(data["id"]) is None
                or data.get("status") not in DELTA_STATUSES
                or not isinstance(data.get("rule"), str)):
            raise WorkflowError(f"corrupt delta file {p}")
        if data["status"] in ACTIVE_STATUSES:
            out.append(data)
    return out


def _write_new_user_delta(delta: dict) -> Path:
    directory = _user_deltas_dir()
    _mkdir_or_refuse(directory)
    path = _user_delta_path(delta["id"])
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(delta, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError as e:
        raise WorkflowError(f"user overlay delta {delta['id']} already exists at {path}") from e
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def promote_user(root: Path, delta_id: str) -> dict:
    """Explicitly copy a cross-project-proven project candidate into the user overlay."""
    delta = load_delta(root, delta_id)
    if delta.get("candidate_scope") != "user_candidate":
        raise WorkflowError(
            f"delta {delta_id} candidate_scope is {delta.get('candidate_scope')!r}; "
            "promote-user requires user_candidate")
    observed = delta.get("observed_in")
    projects = sorted({item for item in observed or [] if isinstance(item, str) and item})
    if len(projects) < 2:
        raise WorkflowError(
            f"delta {delta_id} has evidence from {len(projects)} distinct project(s); "
            "promote-user requires observed_in evidence from at least 2 distinct projects")
    if delta.get("status") not in ACTIVE_STATUSES:
        raise WorkflowError(
            f"delta {delta_id} is {delta.get('status')}; promote-user requires an active delta")
    promoted = json.loads(json.dumps(delta))
    promoted["scope"] = {"kind": "user"}
    promoted["promoted_from"] = {
        "project": _project_slug(root), "delta_id": delta_id, "observed_in": projects,
        "at": _now_iso(),
    }
    _write_new_user_delta(promoted)
    return promoted


def _strict_delta_directory(directory: Path, *, layer: str, source_kind: str) -> list[dict]:
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            delta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise WorkflowError(f"corrupt delta file {path} ({e})") from e
        if (not isinstance(delta, dict) or delta.get("schema") not in DELTA_SCHEMAS
                or not isinstance(delta.get("id"), str)
                or DELTA_ID_RE.fullmatch(delta["id"]) is None
                or delta.get("status") not in DELTA_STATUSES
                or not isinstance(delta.get("rule"), str)
                or not isinstance(delta.get("params") or {}, dict)):
            raise WorkflowError(f"corrupt delta file {path}")
        if delta["status"] not in ACTIVE_STATUSES:
            continue
        out.append({
            "id": delta["id"], "rule": delta["rule"], "stage": delta["status"],
            "status": delta["status"], "params": dict(delta.get("params") or {}),
            "layer": layer, "source_kind": source_kind, "enabled": True,
        })
    return out


def _base_policies() -> list[dict]:
    """Machine-composable layer 0 defaults; disabled until a narrower layer activates a rule."""
    return [{
        "id": f"base/{rule_id}", "rule": rule_id, "stage": "observing",
        "status": "observing", "params": dict(rule.get("default_params") or {}),
        "layer": "base", "source_kind": "built-in", "enabled": False,
    } for rule_id, rule in sorted(RULES.items())]


def _load_project_policy(root: Path) -> list[dict]:
    path = _project_policy_path(root)
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise WorkflowError(f"corrupt committed project policy {path} ({e})") from e
    if (not isinstance(document, dict) or document.get("schema") != PROJECT_POLICY_SCHEMA
            or not isinstance(document.get("policies"), list)):
        raise WorkflowError(f"corrupt committed project policy {path}")
    out = []
    seen_ids = set()
    for index, policy in enumerate(document["policies"]):
        if (not isinstance(policy, dict) or not isinstance(policy.get("id"), str)
                or DELTA_ID_RE.fullmatch(policy["id"]) is None
                or not isinstance(policy.get("rule"), str)
                or policy.get("stage") not in POLICY_STAGES
                or not isinstance(policy.get("params"), dict)
                or not isinstance(policy.get("summary"), str)
                or not isinstance(policy.get("provenance"), dict)
                or policy["id"] in seen_ids):
            raise WorkflowError(f"corrupt committed project policy {path}: policies[{index}]")
        seen_ids.add(policy["id"])
        if policy["stage"] == "enforce":
            raise WorkflowError(
                f"committed project policy {policy['id']} requests enforce, which is not reachable "
                "until the Adapt & Enforce arc")
        out.append({
            "id": policy["id"], "rule": policy["rule"], "stage": policy["stage"],
            "status": policy["stage"], "params": dict(policy.get("params") or {}),
            "layer": "project", "source_kind": "committed", "enabled": True,
        })
    return out


def set_round_override(root: Path, round_id: str, rule: str, stage: str, reason: str) -> dict:
    if not ROUND_RE.fullmatch(round_id):
        raise WorkflowError(f"--round must match YYYY-MM-DD-<slug>, got {round_id!r}")
    if rule not in RULES:
        raise WorkflowError(f"unknown rule {rule!r} (known: {', '.join(sorted(RULES))})")
    if stage not in RUNTIME_POLICY_STAGES:
        allowed = ", ".join(RUNTIME_POLICY_STAGES)
        raise WorkflowError(f"round override stage must be one of {allowed}; enforce is not reachable")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkflowError("round override requires --reason")
    path = _round_override_path(root)
    document = None
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise WorkflowError(f"corrupt round override {path} ({e})") from e
        if (not isinstance(document, dict) or document.get("schema") != "waystone-round-override-1"
                or not isinstance(document.get("overrides"), list)):
            raise WorkflowError(f"corrupt round override {path}")
        if document.get("expired_at") is None and document.get("round_id") != round_id:
            raise WorkflowError(
                f"round override for {document.get('round_id')} is still active; close that round first")
    if document is None or document.get("expired_at") is not None:
        document = {
            "schema": "waystone-round-override-1", "round_id": round_id,
            "created_at": _now_iso(), "expired_at": None, "overrides": [],
        }
    if any(item.get("rule") == rule for item in document["overrides"] if isinstance(item, dict)):
        raise WorkflowError(f"round {round_id} already has an override for {rule}")
    entry = {
        "id": f"round/{rule}", "rule": rule, "stage": stage,
        "params": dict(RULES[rule].get("default_params") or {}),
        "reason": reason.strip(), "at": _now_iso(),
    }
    document["overrides"].append(entry)
    _ensure_project_state_or_refuse(root)
    _mkdir_or_refuse(path.parent)
    write_text_atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return entry


def _load_round_overrides(root: Path, round_id: str | None) -> list[dict]:
    path = _round_override_path(root)
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt round override {path} ({e})") from e
    if (not isinstance(document, dict) or document.get("schema") != "waystone-round-override-1"
            or not isinstance(document.get("round_id"), str)
            or not isinstance(document.get("overrides"), list)):
        raise WorkflowError(f"corrupt round override {path}")
    if document.get("expired_at") is not None:
        return []
    if round_id is not None and document["round_id"] != round_id:
        raise WorkflowError(
            f"active round override is for {document['round_id']}, not requested round {round_id}")
    out = []
    seen = set()
    for index, entry in enumerate(document["overrides"]):
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not isinstance(entry.get("rule"), str) or entry["rule"] not in RULES
                or entry.get("stage") not in RUNTIME_POLICY_STAGES
                or not isinstance(entry.get("params") or {}, dict)
                or not isinstance(entry.get("reason"), str) or not entry["reason"]
                or entry["rule"] in seen):
            raise WorkflowError(f"corrupt round override {path}: overrides[{index}]")
        seen.add(entry["rule"])
        out.append({
            "id": entry["id"], "rule": entry["rule"], "stage": entry["stage"],
            "status": entry["stage"], "params": dict(entry.get("params") or {}),
            "layer": "round", "source_kind": "override", "enabled": True,
            "round_id": document["round_id"], "reason": entry["reason"],
        })
    return out


def expire_round_overrides(root: Path, round_id: str) -> bool:
    path = _round_override_path(root)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt round override {path} ({e})") from e
    if (not isinstance(document, dict) or document.get("schema") != "waystone-round-override-1"
            or not isinstance(document.get("round_id"), str)
            or not isinstance(document.get("overrides"), list)):
        raise WorkflowError(f"corrupt round override {path}")
    if document.get("expired_at") is not None:
        return False
    if document["round_id"] != round_id:
        raise WorkflowError(f"round override {path} does not belong to closing round {round_id}")
    document["expired_at"] = _now_iso()
    write_text_atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return True


_STAGE_RANK = {stage: index for index, stage in enumerate(POLICY_STAGES)}


def _resolve_same_scope(policies: list[dict], scope: str,
                        conflicts: list[dict], shadowed: list[dict]) -> list[dict]:
    resolved = []
    by_rule: dict[str, list[dict]] = {}
    for policy in policies:
        if policy.get("enabled") is True:
            by_rule.setdefault(policy["rule"], []).append(policy)
    for rule, group in sorted(by_rule.items()):
        ordered = sorted(group, key=lambda item: (_STAGE_RANK[item["stage"]], item["id"]))
        winner = ordered[0]
        source_ids = sorted(item["id"] for item in group)
        effective = {**winner, "source_ids": source_ids}
        resolved.append(effective)
        if len(group) > 1:
            conflicts.append({
                "rule": rule, "scope": scope, "ids": source_ids,
                "effective_id": winner["id"], "effective_stage": winner["stage"],
                "resolution": "least-restrictive",
            })
            shadowed.extend({
                "id": item["id"], "rule": rule, "layer": item["layer"],
                "source_kind": item["source_kind"], "shadowed_by": winner["id"],
                "reason": "least-restrictive",
            } for item in ordered[1:])
    return resolved


def compose_policy(root: Path, round_id: str | None = None) -> dict:
    """Compose base < user < project < round, with D1d same-scope resolution and visibility."""
    base = _base_policies()
    user = _strict_delta_directory(_user_deltas_dir(), layer="user", source_kind="overlay")
    local = _strict_delta_directory(_deltas_dir(root), layer="project", source_kind="overlay")
    committed = _load_project_policy(root)
    round_policies = _load_round_overrides(root, round_id)
    conflicts: list[dict] = []
    shadowed: list[dict] = []

    # Committed and local overlay policies share project scope, but D1d gives committed policy the
    # explicit tie-break. Local policies remain visible as shadowed instead of disappearing.
    local_by_rule: dict[str, list[dict]] = {}
    committed_by_rule: dict[str, list[dict]] = {}
    for policy in local:
        local_by_rule.setdefault(policy["rule"], []).append(policy)
    for policy in committed:
        committed_by_rule.setdefault(policy["rule"], []).append(policy)
    project_candidates: list[dict] = []
    for rule in sorted(set(local_by_rule) | set(committed_by_rule)):
        local_group = local_by_rule.get(rule, [])
        committed_group = committed_by_rule.get(rule, [])
        if committed_group:
            winners = _resolve_same_scope(
                committed_group, "project-committed", conflicts, shadowed)
            project_candidates.extend(winners)
            if local_group:
                winner = winners[0]
                local_ids = sorted(item["id"] for item in local_group)
                conflicts.append({
                    "rule": rule, "scope": "project",
                    "ids": sorted([*local_ids, *(item["id"] for item in committed_group)]),
                    "effective_id": winner["id"], "effective_stage": winner["stage"],
                    "resolution": "committed-wins", "shadowed": local_ids,
                })
                shadowed.extend({
                    "id": item["id"], "rule": rule, "layer": "project",
                    "source_kind": "overlay", "shadowed_by": winner["id"],
                    "reason": "committed-wins",
                } for item in sorted(local_group, key=lambda item: item["id"]))
        else:
            project_candidates.extend(_resolve_same_scope(
                local_group, "project", conflicts, shadowed))

    resolved_layers = {
        "user": _resolve_same_scope(user, "user", conflicts, shadowed),
        "project": project_candidates,
        # One override per rule is enforced at write/load, but resolving here keeps the invariant
        # explicit if the representation evolves.
        "round": _resolve_same_scope(round_policies, "round", conflicts, shadowed),
    }
    effective_by_rule: dict[str, dict] = {}
    for layer in ("user", "project", "round"):
        for policy in resolved_layers[layer]:
            previous = effective_by_rule.get(policy["rule"])
            if previous is not None:
                shadowed.append({
                    "id": previous["id"], "rule": previous["rule"],
                    "layer": previous["layer"], "source_kind": previous["source_kind"],
                    "shadowed_by": policy["id"], "reason": "narrower-scope",
                })
            effective_by_rule[policy["rule"]] = policy

    return {
        "schema": "waystone-policy-composition-1", "round_id": round_id,
        "layers": [
            {"name": "base", "scope": "base", "policies": base},
            {"name": "user", "scope": "user", "policies": user},
            {"name": "project", "scope": "project", "policies": [*local, *committed]},
            {"name": "round", "scope": "round", "policies": round_policies},
        ],
        "effective": sorted(effective_by_rule.values(), key=lambda item: (item["rule"], item["id"])),
        "conflicts": sorted(conflicts, key=lambda item: (
            item["rule"], item["scope"], item["resolution"], item["effective_id"])),
        "shadowed": sorted(shadowed, key=lambda item: (
            item["rule"], item["layer"], item["id"], item["reason"])),
    }


def materialize(root: Path, delta_id: str, *, consent_recorded: bool = False) -> Path:
    """Promote one replay-validated local delta into the commit-target project policy file."""
    context = {"rule_id": delta_id}
    if consent_recorded:
        record_consent(root, "materialize", "accept", context)
    if not has_accepted_consent(root, "materialize", context):
        raise WorkflowError(
            f"materialize consent is required for {delta_id}; record it with `waystone consent "
            f"record materialize accept --context rule_id={delta_id}` or pass --consent-recorded")
    delta = load_delta(root, delta_id)
    if delta.get("status") not in ACTIVE_STATUSES:
        raise WorkflowError(f"delta {delta_id} must be active before materialization")
    if not isinstance(delta.get("replay"), dict):
        raise WorkflowError(
            f"delta {delta_id} has no replay evidence; run `waystone overlay replay {delta_id}` first")
    path = _project_policy_path(root)
    document = {"schema": PROJECT_POLICY_SCHEMA, "policies": []}
    if path.is_file():
        # Validate the existing document through the same strict loader before preserving it.
        _load_project_policy(root)
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise WorkflowError(f"corrupt committed project policy {path} ({e})") from e
    if any(policy.get("id") == delta_id for policy in document["policies"]
           if isinstance(policy, dict)):
        raise WorkflowError(f"committed project policy {delta_id} already exists in {path}")
    evidence = delta.get("evidence") if isinstance(delta.get("evidence"), dict) else {}
    replay = delta["replay"]
    document["policies"].append({
        "id": delta_id, "rule": delta["rule"], "stage": delta["status"],
        "params": dict(delta.get("params") or {}),
        "summary": str(evidence.get("summary") or delta.get("title") or delta_id),
        "provenance": {
            "source_delta": delta_id,
            "evidence": {
                "source": evidence.get("source"), "rec_id": evidence.get("rec_id"),
                "summary": evidence.get("summary"), "pointers": list(evidence.get("pointers") or []),
            },
            "replay": {key: replay.get(key) for key in ("opportunities", "fires", "fire_rate")},
        },
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    write_text_atomic(path, text)
    return path


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
        "schema": "waystone-delta-1",
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
            f"delta {delta_id} has no replay result — run `waystone overlay replay {delta_id}` first")
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


# ---- shadow replay (§5 — deterministic projection; timestamp only in the delta event) ----
def _delegation_context(record: Path, did: str) -> dict:
    import yaml

    context: dict = {"delegation_id": did}
    try:
        packet = yaml.safe_load((record / "packet.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        packet = None
    task = packet.get("task") if isinstance(packet, dict) and isinstance(packet.get("task"), dict) else {}
    if isinstance(task.get("id"), str):
        context["task_id"] = task["id"]
    if isinstance(task.get("round"), str):
        context["round_id"] = task["round"]
    return context


def _by_round_projection(rows: list[tuple[str | None, bool]]) -> list[dict]:
    grouped: dict[str, list[bool]] = {}
    for round_id, fired in rows:
        grouped.setdefault(round_id or "unknown", []).append(fired)
    return [{"round_id": round_id, "opportunities": len(fired), "fires": sum(fired)}
            for round_id, fired in sorted(grouped.items())]


def _delegation_round(root: Path, record: Path, context: dict, rounds: list[dict]) -> str | None:
    if isinstance(context.get("round_id"), str):
        return context["round_id"]
    try:
        exposure = json.loads((record / "exposure.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    at = exposure.get("at") if isinstance(exposure, dict) else None
    if not isinstance(at, str):
        return None
    following = [row for row in rounds if row["at"] >= at]
    return following[0]["round_id"] if following else None


def _replay_delegations(root: Path, rule_id: str) -> dict:
    import delegate
    from common import delegation_scope_drift

    base = delegate._delegations_dir(root)
    candidates = []
    if base.is_dir():
        candidates = [p.parent.parent for p in sorted(base.glob("*/artifact/contract.yaml"))]
    fires: list[str] = []
    errors = 0
    opportunities = 0
    round_rows: list[tuple[str | None, bool]] = []
    rounds, _round_errors = _round_records(root)
    for rec in candidates:
        context = _delegation_context(rec, rec.name)
        fired = False
        if rule_id == "delegation-verification-evidence-v1":
            try:
                contract = delegate._load_contract(rec)
            except WorkflowError:
                errors += 1
                continue
            fired = rule1_fires(contract)
        elif rule_id == "delegation-scope-drift-v1":
            drift = delegation_scope_drift(rec)
            if drift.get("evaluable") is not True:
                if drift.get("coverage_reason") != "scope-unknown":
                    errors += 1
                continue
            fired = bool(drift.get("outside_scope"))
        else:
            raise WorkflowError(f"delegation replay does not implement {rule_id!r}")
        opportunities += 1
        round_rows.append((_delegation_round(root, rec, context, rounds), fired))
        if fired:
            fires.append(f"{rec.name}/artifact/contract.yaml")
    return {
        "corpus": "delegations",
        "corpus_size": len(candidates),
        "opportunities": opportunities,
        "fires": len(fires),
        "examples": fires[:5],
        "evaluation_errors": errors,
        "by_round": _by_round_projection(round_rows),
    }


def _replay_reviews(root: Path, params: dict) -> dict:
    import improve

    cfg = load_config(root)
    rows = improve._project_review_rows(_project_slug(root), root, cfg)
    opportunities = 0
    fired_rounds: list[str] = []
    errors = 0
    unlinked = 0
    severities = params.get("severities") or ["blocker", "major"]
    for row in rows:
        out = evaluate_rule2(root, cfg, severities, round_filter=row["round_id"])
        errors += out["evaluation_errors"]
        unlinked += out["unlinked"]
        if out["evaluation_errors"]:
            continue
        opportunities += 1
        if out["fires"]:
            fired_rounds.append(row["round_id"])
    return {
        "corpus": "reviews",
        "corpus_size": len(rows),
        "opportunities": opportunities,
        "fires": len(fired_rounds),
        "examples": fired_rounds[:5],
        "evaluation_errors": errors,
        "unlinked_findings": unlinked,
        "resolution_provenance": "current-task-state-approximation",
        "by_round": [{"round_id": row["round_id"], "opportunities": 1,
                      "fires": int(row["round_id"] in fired_rounds)} for row in rows],
    }


def _round_records(root: Path) -> tuple[list[dict], int]:
    directory = _exposure_dir(root)
    if not directory.is_dir():
        return [], 0
    rows: list[dict] = []
    errors = 0
    for path in sorted(directory.glob("round-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors += 1
            continue
        if (not isinstance(row, dict) or row.get("schema") != "waystone-round-exposure-1"
                or not isinstance(row.get("round_id"), str) or not isinstance(row.get("at"), str)):
            errors += 1
            continue
        rows.append({**row, "_file": str(path)})
    rows.sort(key=lambda row: (row["at"], row["round_id"], row["_file"]))
    return rows, errors


def _replay_rounds(root: Path, rule_id: str, params: dict) -> dict:
    rows, errors = _round_records(root)
    fired_rounds: list[str] = []
    by_round: list[dict] = []
    opportunities = 0
    unevaluable = 0
    if rule_id == "review-skipped-closes-v1":
        ingests, ingest_errors, legacy_approximations = _review_ingests_for_rounds(root, rows)
        errors += ingest_errors
        result = evaluate_review_skipped_closes(
            rows, ingests, consecutive=params.get("consecutive", 2))
        fired_rounds = result["fires"]
        opportunities = result["opportunities"]
        by_round = [{"round_id": row["round_id"], "opportunities": 1,
                     "fires": int(row["fired"]), "streak": row["streak"]}
                    for row in result["by_round"]]
    else:
        for row in rows:
            if rule_id == "env-manifest-mutation-v1":
                result = evaluate_env_manifest_mutation(row)
                if result["evaluable"] is not True:
                    unevaluable += 1
                    continue
            elif rule_id == "done-without-evidence-v1":
                result = evaluate_done_without_evidence(row)
                errors += result.get("evaluation_errors", 0)
            else:
                raise WorkflowError(f"round replay does not implement {rule_id!r}")
            opportunities += 1
            fired = bool(result["fires"])
            if fired:
                fired_rounds.append(row["round_id"])
            by_round.append({"round_id": row["round_id"], "opportunities": 1,
                             "fires": int(fired)})
    report = {
        "corpus": "rounds", "corpus_size": len(rows), "opportunities": opportunities,
        "fires": len(fired_rounds), "examples": fired_rounds[:5],
        "evaluation_errors": errors, "unevaluable_rounds": unevaluable, "by_round": by_round,
    }
    if rule_id == "review-skipped-closes-v1":
        report["legacy_ingest_approximations"] = legacy_approximations
    return report


def replay(root: Path, delta_id: str) -> dict:
    """Replay one delta's fixed rule over its declared historical corpus. The returned projection
    has no timestamp and is therefore byte-stable for identical inputs. `replayed_at` is added only
    to the persisted delta event, where time is intentional (S7)."""
    delta = load_delta(root, delta_id)
    rule_id = delta.get("rule")
    rule = RULES.get(rule_id)
    if rule is None:
        raise WorkflowError(f"unknown rule {rule_id!r}")
    if rule["corpus"] == "delegations":
        report = _replay_delegations(root, rule_id)
    elif rule["corpus"] == "reviews":
        report = _replay_reviews(root, delta.get("params") or {})
    elif rule["corpus"] == "rounds":
        report = _replay_rounds(root, rule_id, delta.get("params") or {})
    else:
        raise WorkflowError(f"rule {rule_id!r} declares unknown replay corpus {rule['corpus']!r}")
    opportunities = report["opportunities"]
    report["fire_rate"] = round(report["fires"] / opportunities, 4) if opportunities else None
    report["estimated_nuisance_rate"] = None
    report["nuisance_provenance"] = "unlabeled"
    if not opportunities:
        report["status"] = "empty-corpus"

    persisted = dict(report)
    persisted["replayed_at"] = _now_iso()
    delta["replay"] = persisted
    _write_delta(root, delta)
    return report


# ---- boundary warn engine (§6 — S5/S6/S9; never blocks the host, never changes exit) ----
def _append_warning(root: Path, row: dict) -> None:
    _ensure_project_state_or_refuse(root)
    p = _warnings_path(root)
    _mkdir_or_refuse(p.parent)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _emit(root: Path, boundary: str, delta_id: str, rule: str, delta_status: str, event: str,
          message: str, context: dict) -> dict:
    """Append a warnings row; warning-stage fires and every policy conflict are visible on stderr."""
    row = {"at": _now_iso(), "boundary": boundary, "delta_id": delta_id, "rule": rule,
           "delta_status": delta_status, "event": event, "message": message, "context": context}
    _append_warning(root, row)
    if event == "fire" and delta_status == "warning":
        print(f"waystone warn [{delta_id}]: {message}", file=sys.stderr)
    elif event == "conflict":
        print(f"waystone warn conflict [{delta_id}]: {message}", file=sys.stderr)
    return row


def _delegation_targets(root: Path, boundary: str, context: dict) -> list[tuple[str, Path]]:
    import delegate

    targets: list[tuple[str, Path]] = []
    if boundary in ("delegate-run", "delegate-apply"):
        did = context.get("delegation_id")
        if did:
            rec = delegate._record_dir(root, did)
            if (rec / "artifact" / "contract.yaml").exists():
                targets.append((did, rec))
    elif boundary == "check":
        for did, rec in delegate._iter_delegations(root):
            st = delegate._read_status_raw(rec)
            if st and st.get("state") == "needs-review" and (rec / "artifact" / "contract.yaml").exists():
                targets.append((did, rec))
    return targets


def _rule1_targets(root: Path, boundary: str, context: dict) -> tuple[list[str], list[str], list[str]]:
    """(fired_dids, error_dids, evaluated_dids) for rule 1. Records
    without a contract (failed-env/-runner/-artifact) are excluded — they are not evaluable (R8)."""
    import delegate
    fired: list[str] = []
    errors: list[str] = []
    evaluated: list[str] = []
    for did, rec in _delegation_targets(root, boundary, context):
        try:
            contract = delegate._load_contract(rec)
        except WorkflowError:
            errors.append(did)  # corrupt/unparseable = evaluation-error, never a fire (no invention)
            continue
        evaluated.append(did)
        if rule1_fires(contract):
            fired.append(did)
    return fired, errors, evaluated


def _scope_drift_targets(root: Path, boundary: str, context: dict) -> tuple[list[dict], list[dict]]:
    from common import delegation_scope_drift

    evaluated: list[dict] = []
    errors: list[dict] = []
    for did, record in _delegation_targets(root, boundary, context):
        attribution = _delegation_context(record, did)
        attribution.update({key: context[key] for key in ("task_id", "round_id")
                            if isinstance(context.get(key), str)})
        drift = delegation_scope_drift(record)
        if drift.get("evaluable") is not True:
            if drift.get("coverage_reason") == "scope-unknown":
                continue
            errors.append({**attribution,
                           "coverage_reason": drift.get("coverage_reason") or "scope-unavailable"})
            continue
        evaluated.append({**attribution, "outside_scope": drift.get("outside_scope") or []})
    return evaluated, errors


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


def _round_record_at_boundary(root: Path, context: dict) -> dict | None:
    if isinstance(context.get("round_record"), dict):
        return context["round_record"]
    rows, _errors = _round_records(root)
    round_id = context.get("round_id")
    matches = [row for row in rows if round_id is None or row["round_id"] == round_id]
    return matches[-1] if matches else None


def _round_rule_at_boundary(root: Path, rule_id: str, context: dict, params: dict) -> dict | None:
    current = _round_record_at_boundary(root, context)
    if current is None:
        return None
    if rule_id == "env-manifest-mutation-v1":
        return evaluate_env_manifest_mutation(current)
    if rule_id == "done-without-evidence-v1":
        return evaluate_done_without_evidence(current)
    if rule_id == "review-skipped-closes-v1":
        rows, round_errors = _round_records(root)
        ingests, ingest_errors, legacy_approximations = _review_ingests_for_rounds(root, rows)
        result = evaluate_review_skipped_closes(
            rows, ingests, consecutive=params.get("consecutive", 2))
        result["evaluation_errors"] = round_errors + ingest_errors
        result["legacy_ingest_approximations"] = legacy_approximations
        current_rows = [row for row in result["by_round"]
                        if row["round_id"] == current["round_id"]]
        result["current_fired"] = bool(current_rows and current_rows[-1]["fired"])
        result["current_streak"] = current_rows[-1]["streak"] if current_rows else None
        return result
    return None


_RULE1_MSG = ("delegation {did} carries no delegate-side verification evidence — verify independently "
              "before apply (a delegate-claimed absence is a reporting gap, not proof of unverified work)")


def _emit_evaluations(root: Path, boundary: str, group: list[dict], rule_id: str,
                      fired: bool, context: dict) -> list[dict]:
    rows = []
    for delta in sorted(group, key=lambda item: item["id"]):
        rows.append(_emit(
            root, boundary, delta["id"], rule_id, delta["status"], "evaluation",
            "rule evaluated at workflow boundary", {**context, "fired": fired}))
    return rows


def evaluate_boundary(root: Path, boundary: str, context: dict) -> list[dict]:
    """Evaluate active (observing/warning) deltas whose rule declares `boundary`, append fire/
    evaluation-error/conflict rows to warnings.jsonl, print warning-stage fires and all conflicts.
    Wrapped so ANY exception is swallowed with one stderr notice — a warn-engine bug must never change
    the host command's exit or abort its flow (S5, host-exit invariant)."""
    try:
        return _evaluate_boundary(root, boundary, context)
    except Exception as e:  # noqa: BLE001 — never propagate into the host flow
        print(f"waystone warn: overlay evaluation error at {boundary}: {e}", file=sys.stderr)
        return []


def _evaluate_boundary(root: Path, boundary: str, context: dict) -> list[dict]:
    requested_round = context.get("round_id") if boundary == "round-close" else None
    composition = compose_policy(
        root, requested_round if isinstance(requested_round, str) else None)
    policies_by_id = {
        policy["id"]: policy for layer in composition["layers"] for policy in layer["policies"]
    }
    active = composition["effective"]
    events: list[dict] = []
    for d in sorted((d for d in active if d.get("rule") not in RULES),
                    key=lambda d: d.get("id", "")):
        rule_id = d.get("rule")
        message = f"active delta references unknown rule {rule_id!r} and could not be evaluated"
        events.append(_emit(root, boundary, d.get("id", "(missing-id)"), rule_id,
                            d["status"], "evaluation-error", message, {}))
        print(f"waystone warn [{d.get('id', '(missing-id)')}]: {message}", file=sys.stderr)

    relevant = [d for d in active
                if boundary in RULES.get(d.get("rule"), {}).get("boundaries", set())]
    if not relevant:
        return events
    relevant_rules = {policy["rule"] for policy in relevant}
    for conflict in composition["conflicts"]:
        if conflict["rule"] not in relevant_rules:
            continue
        conflict_context = {"delta_ids": conflict["ids"], "resolution": conflict["resolution"]}
        for key in ("delegation_id", "task_id", "round_id"):
            if isinstance(context.get(key), str):
                conflict_context[key] = context[key]
        if isinstance(context.get("task_ids"), list):
            conflict_context["task_ids"] = context["task_ids"]
        events.append(_emit(
            root, boundary, conflict["effective_id"], conflict["rule"],
            conflict["effective_stage"], "conflict",
            f"{len(conflict['ids'])} policies conflict at {conflict['scope']} scope — "
            f"effective stage {conflict['effective_stage']} ({conflict['resolution']})",
            conflict_context))

    for rep in sorted(relevant, key=lambda item: (item["rule"], item["id"])):
        rule_id = rep["rule"]
        group = [policies_by_id[source_id] for source_id in rep.get("source_ids", [rep["id"]])
                 if source_id in policies_by_id]
        if not group:
            group = [rep]
        eff = rep["stage"]
        params = rep.get("params") or {}

        if rule_id == "delegation-verification-evidence-v1":
            fired, errors, evaluated = _rule1_targets(root, boundary, context)
            import delegate
            for did in evaluated:
                attribution = _delegation_context(delegate._record_dir(root, did), did)
                attribution.update({key: context[key] for key in ("task_id", "round_id")
                                    if isinstance(context.get(key), str)})
                events.extend(_emit_evaluations(
                    root, boundary, group, rule_id, did in fired, attribution))
            for did in fired:
                attribution = _delegation_context(delegate._record_dir(root, did), did)
                attribution.update({key: context[key] for key in ("task_id", "round_id")
                                    if isinstance(context.get(key), str)})
                events.append(_emit(root, boundary, rep["id"], rule_id, eff, "fire",
                                    _RULE1_MSG.format(did=did), attribution))
            for did in errors:
                events.append(_emit(root, boundary, rep["id"], rule_id, eff, "evaluation-error",
                                    f"delegation {did} contract could not be evaluated",
                                    {"delegation_id": did}))
        elif rule_id == "delegation-scope-drift-v1":
            evaluated, errors = _scope_drift_targets(root, boundary, context)
            for row in evaluated:
                outside = row.get("outside_scope") or []
                events.extend(_emit_evaluations(root, boundary, group, rule_id, bool(outside), {
                    key: value for key, value in row.items() if key != "outside_scope"}))
                if outside:
                    events.append(_emit(
                        root, boundary, rep["id"], rule_id, eff, "fire",
                        f"delegation {row['delegation_id']} changed {len(outside)} file(s) outside "
                        "its structured declared scope",
                        {**row, "outside_scope": outside}))
            for row in errors:
                events.append(_emit(
                    root, boundary, rep["id"], rule_id, eff, "evaluation-error",
                    f"delegation {row['delegation_id']} scope could not be evaluated",
                    row))
        elif rule_id == "round-close-open-findings-v1":
            severities = params.get("severities") or ["blocker", "major"]
            out = _rule2_at_boundary(root, boundary, context, severities)
            if out is None:
                continue
            events.extend(_emit_evaluations(
                root, boundary, group, rule_id, bool(out["fires"]),
                {"round_id": context.get("round_id"),
                 "task_ids": [f["task_id"] for f in out["fires"]]}))
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
        elif rule_id in ("env-manifest-mutation-v1", "review-skipped-closes-v1",
                          "done-without-evidence-v1"):
            out = _round_rule_at_boundary(root, rule_id, context, params)
            if out is None:
                continue
            round_id = (_round_record_at_boundary(root, context) or {}).get("round_id")
            fired = out.get("current_fired") if rule_id == "review-skipped-closes-v1" else bool(out["fires"])
            attribution = {"round_id": round_id}
            if rule_id == "env-manifest-mutation-v1":
                attribution["manifest_paths"] = out.get("fires") or []
            elif rule_id == "done-without-evidence-v1":
                attribution["task_ids"] = out.get("fires") or []
            else:
                attribution["consecutive"] = params.get("consecutive", 2)
            if out.get("evaluable", True) is not True:
                events.append(_emit(
                    root, boundary, rep["id"], rule_id, eff, "evaluation-error",
                    f"round {round_id} could not be evaluated: {out.get('coverage_reason')}",
                    attribution))
                continue
            events.extend(_emit_evaluations(root, boundary, group, rule_id, bool(fired), attribution))
            if fired:
                if rule_id == "env-manifest-mutation-v1":
                    message = (f"round {round_id} mutates dependency manifest(s) without an env_prep "
                               f"change or structured task scope reference: {', '.join(out['fires'])}")
                elif rule_id == "done-without-evidence-v1":
                    message = (f"round {round_id} closes {len(out['fires'])} task(s) without joined "
                               "verification, verify, or verdict evidence")
                else:
                    message = (f"round {round_id} reaches {out['current_streak']} consecutive "
                               "closes without an intervening review feedback ingest")
                events.append(_emit(
                    root, boundary, rep["id"], rule_id, eff, "fire", message, attribution))
            if out.get("evaluation_errors"):
                events.append(_emit(
                    root, boundary, rep["id"], rule_id, eff, "evaluation-error",
                    f"{out['evaluation_errors']} round/review evidence row(s) could not be evaluated",
                    attribution))
    return events


# ---- exposure (§9 — round exposure record; delegation exposure lives in delegate) ----
def _exposure_dir(root: Path) -> Path:
    return project_state_path(root) / "exposure"


def _profile_summary(root: Path) -> tuple[str | None, dict | None]:
    """(profile_fingerprint, {role: backend}) from the delegation profile, or (None, None) when it is
    absent — a round closes without any delegation, so the harness never guesses bindings."""
    import delegate
    if not delegate._profile_path(root).is_file():
        return None, None
    profile, fp = delegate._load_profile(root)
    bindings: dict[str, str] = {}
    for role, b in (profile.get("bindings") or {}).items():
        if isinstance(b, dict) and isinstance(b.get("backend"), str):
            bindings[role] = b["backend"]
    return fp, (bindings or None)


def _config_env_prep_at(root: Path, sha: str) -> tuple[object, bool]:
    import yaml
    from common import git_rc

    rc, text, _err = git_rc(root, "show", f"{sha}:.waystone.yml")
    if rc != 0:
        return None, False
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, False
    if not isinstance(cfg, dict):
        return None, False
    delegation = cfg.get("delegation") if isinstance(cfg.get("delegation"), dict) else {}
    value = delegation.get("env_prep")
    if value is not None and (not isinstance(value, list)
                              or any(not isinstance(item, str) for item in value)):
        return None, False
    return value, True


def _task_done_evidence(root: Path, task_id: str) -> dict:
    import delegate

    signals = {"verification": False, "verify": False, "verdict": False}
    errors = 0
    directory = delegate._delegations_dir(root)
    if directory.is_dir():
        try:
            records = sorted(path for path in directory.iterdir() if path.is_dir())
        except OSError:
            return {"task_id": task_id, **signals, "evaluation_errors": 1}
        for record in records:
            exposure_path = record / "exposure.json"
            try:
                exposure = json.loads(exposure_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(exposure, dict) or exposure.get("task_id") != task_id:
                continue
            contract_path = record / "artifact" / "contract.yaml"
            if contract_path.is_file():
                try:
                    contract = delegate._load_contract(record)
                except WorkflowError:
                    errors += 1
                else:
                    report = contract.get("delegate_report") or {}
                    signals["verification"] |= (
                        report.get("present") is True and bool(report.get("verification")))
            try:
                signals["verify"] |= bool(delegate._verify_artifacts(record))
            except WorkflowError:
                errors += 1
            try:
                signals["verdict"] |= delegate.latest_canonical_verdict(record) is not None
            except WorkflowError:
                errors += 1
    return {"task_id": task_id, **signals, "evaluation_errors": errors}


def _capture_round_evidence(root: Path, base_sha: str | None, head_sha: str | None,
                            task_scopes: dict[str, list[str]], done_task_ids: list[str]) -> dict:
    from common import git_rc

    base = base_sha if isinstance(base_sha, str) and base_sha else None
    head = head_sha if isinstance(head_sha, str) and head_sha else None
    payload = {
        "evaluable": False, "coverage_reason": "round-diff-unavailable",
        "changed_files": [], "manifest_paths": [], "env_prep_changed": None,
        "task_scopes": {task_id: list(scopes) for task_id, scopes in sorted(task_scopes.items())},
        "done_task_ids": sorted(set(done_task_ids)),
        "done_evidence": [_task_done_evidence(root, task_id)
                          for task_id in sorted(set(done_task_ids))],
    }
    if base is None or head is None:
        return payload
    rc, out, _err = git_rc(root, "diff", "--name-only", base, head, "--")
    if rc != 0:
        return payload
    changed = sorted({line.strip() for line in out.splitlines() if line.strip()})
    before_env, before_ok = _config_env_prep_at(root, base)
    after_env, after_ok = _config_env_prep_at(root, head)
    if not (before_ok and after_ok):
        return {**payload, "changed_files": changed,
                "manifest_paths": [path for path in changed if _is_dependency_manifest(path)],
                "coverage_reason": "env-prep-comparison-unavailable"}
    return {
        **payload, "evaluable": True, "coverage_reason": None, "changed_files": changed,
        "manifest_paths": [path for path in changed if _is_dependency_manifest(path)],
        "env_prep_changed": before_env != after_env,
    }


def write_round_exposure(root: Path, round_id: str, head_sha: str | None, watermark: str | None,
                         session_id: str | None = None, *, base_sha: str | None = None,
                         task_scopes: dict[str, list[str]] | None = None,
                         done_task_ids: list[str] | None = None):
    """Immutable per-round exposure record written at close (§9/#4). A re-close of the same round-id
    gets a `-2`/`-3` suffix (H4 precedent — existing records are never overwritten)."""
    _ensure_project_state_or_refuse(root)
    fp, bindings = _profile_summary(root)
    cfg = load_config(root)
    env_prep = (cfg.get("delegation") or {}).get("env_prep")
    round_evidence = _capture_round_evidence(
        root, base_sha, watermark, task_scopes or {}, done_task_ids or [])
    policy_composition = compose_policy(root, round_id=round_id)
    exposure = {
        "schema": "waystone-round-exposure-1", "round_id": round_id, "at": _now_iso(),
        "session_id": session_id,
        "project": {"pslug": _project_slug(root), "root": str(Path(root).resolve())},
        "head_sha": head_sha, "config_watermark": watermark, "base_sha": base_sha,
        "profile_fingerprint": fp, "bindings": bindings,
        "env_prep": env_prep, "round_evidence": round_evidence,
        "overlays_active": [{"id": d["id"], "status": d["stage"]}
                            for d in policy_composition["effective"]],
        "policy_composition": policy_composition,
        # Adapt & Enforce has not shipped: null means no effective guard engine and [] means no
        # recorded waivers. These are truthful contract values, not missing-data fallbacks.
        "guards": None, "waivers": [],
    }
    edir = _exposure_dir(root)
    _mkdir_or_refuse(edir)
    base = edir / f"round-{round_id}.json"
    p = base
    n = 2
    content = json.dumps(exposure, ensure_ascii=False, indent=2) + "\n"
    while True:
        try:
            with p.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return p, exposure
        except FileExistsError:
            p = base.with_name(f"{base.stem}-{n}{base.suffix}")
            n += 1
        except BaseException:
            # open('x') made this path ours; a failed write must not leave a partial immutable record.
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            raise


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
    with hold_lock(project_lock_path(root)):
        migrate_project_state(root)
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
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        delta = add_delta(
            root, pos[0], rule=opts["rule"], summary=opts["summary"],
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
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        delta = promote(root, pos[0])
    print(f"promoted {delta['id']} -> {delta['status']}")
    return 0


def _cli_demote(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("demote requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        delta = demote(root, pos[0])
    print(f"demoted {delta['id']} -> {delta['status']}")
    return 0


def _cli_suspend(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "note"))
    if not pos:
        raise WorkflowError("suspend requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        delta = suspend(root, pos[0], note=opts.get("note"))
    print(f"suspended {delta['id']}")
    return 0


def _cli_retire(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "note"))
    if not pos:
        raise WorkflowError("retire requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        delta = retire(root, pos[0], note=opts.get("note"))
    print(f"retired {delta['id']}")
    return 0


def _cli_replay(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("replay requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        report = replay(root, pos[0])
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    rate = "null" if report["fire_rate"] is None else f"{report['fire_rate']:.4f}"
    print(f"would have fired {report['fires']}/{report['opportunities']} times (fire rate {rate}). "
          "Nuisance rate requires labeling — inspect examples. "
          "estimated nuisance rate (unlabeled: null)")
    return 0


def _cli_promote_user(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if len(pos) != 1:
        raise WorkflowError("promote-user requires one <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        promoted = promote_user(root, pos[0])
    print(f"promoted {promoted['id']} -> user overlay")
    return 0


def _cli_override(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "round", "stage", "reason"))
    if len(pos) != 1:
        raise WorkflowError("override requires one <rule-id>")
    for name in ("round", "stage", "reason"):
        if not opts.get(name):
            raise WorkflowError(f"override requires --{name}")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        entry = set_round_override(
            root, opts["round"], pos[0], opts["stage"], opts["reason"])
    print(f"round override {entry['rule']} -> {entry['stage']} ({opts['round']})")
    return 0


def _cli_materialize(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",), boolean=("consent-recorded",))
    if len(pos) != 1:
        raise WorkflowError("materialize requires one <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_lock(project_lock_path(root)):
        path = materialize(root, pos[0], consent_recorded=bool(opts.get("consent-recorded")))
    print(f"materialized {pos[0]} -> {path} (left uncommitted)")
    return 0


def _cli_compose(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "round"))
    if pos:
        raise WorkflowError("compose takes no positional arguments")
    root = _resolve_root(opts.get("root"))
    print(json.dumps(compose_policy(root, round_id=opts.get("round")),
                     ensure_ascii=False, indent=2))
    return 0


def _cli_check(rest: list[str]) -> int:
    """The explicit `check` boundary: evaluate every active delta against current state. Firing does
    NOT change the exit code — a successful evaluation is exit 0 even with warnings (S5)."""
    pos, opts = _parse_opts(rest, value=("root",))
    root = _resolve_root(opts.get("root"))
    events = evaluate_boundary(root, "check", {})
    fires = [e for e in events if e["event"] == "fire"]
    if not fires:
        print("waystone check: no active-delta warnings")
    for e in fires:
        marker = "warn" if e["delta_status"] == "warning" else "observe"
        print(f"[{marker}] {e['rule']} [{e['delta_id']}]: {e['message']}")
    for e in (e for e in events if e["event"] == "evaluation-error"):
        print(f"[eval-error] {e['rule']}: {e['message']}")
    return 0


_HANDLERS = {
    "add": _cli_add, "list": _cli_list, "show": _cli_show, "promote": _cli_promote,
    "promote-user": _cli_promote_user, "demote": _cli_demote, "suspend": _cli_suspend,
    "retire": _cli_retire, "replay": _cli_replay, "override": _cli_override,
    "materialize": _cli_materialize, "compose": _cli_compose, "check": _cli_check,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in _HANDLERS:
        print("waystone overlay: expected subcommand "
              "(add|list|show|promote|promote-user|demote|suspend|retire|replay|override|"
              "materialize|compose)", file=sys.stderr)
        return 1
    try:
        return _HANDLERS[argv[0]](argv[1:])
    except _RefusedWrite as e:
        print(f"waystone overlay: {e}", file=sys.stderr)
        return 2
    except WorkflowError as e:
        print(f"waystone overlay: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
