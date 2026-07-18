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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    ROUND_RE, WorkflowError, _project_slug, _read_registry, canonical_payload_hash,
    content_hash, ensure_project_state_dir, find_project_root, has_accepted_consent, hold_lock,
    git_rc, hold_project_lock, load_config, load_tasks, machine_dir, migrate_project_state,
    overlay_lock_path, parse_iso_timestamp, project_state_path, record_consent,
    registry_entry_paths, registry_lock_path, registry_path, write_text_atomic,
)  # noqa: E402

# delta-id grammar mirrors the improve rec_id (`<lens>/<kebab-gist>`, S2) so a rec materialises to a
# delta under the same id and the same recommendation keeps a stable identity across cycles.
DELTA_ID_RE = re.compile(r"^[a-z][a-z0-9_]*/[a-z0-9]+(?:-[a-z0-9]+)*$")
POLICY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# R6: `add` is the acceptance event, so an extra `accepted` lifecycle state is intentionally absent.
DELTA_STATUSES = ("proposed", "observing", "warning", "suspended", "retired")
ACTIVE_STATUSES = ("observing", "warning")
CANDIDATE_SCOPES = ("project_candidate", "user_candidate", "unresolved")
POLICY_STAGES = ("observing", "warning", "enforce")
RUNTIME_POLICY_STAGES = ("observing", "warning")
PROJECT_POLICY_SCHEMA = "waystone-project-policy-1"
DELTA_SCHEMAS = ("waystone-delta-1", "jw-delta-1")
REVIEW_FEEDBACK_SCHEMA = "waystone-review-feedback-1"
MATERIALIZATION_MAP_SCHEMA = "waystone-materialization-map-1"
ROUTING_POLICY_PATH = Path(__file__).resolve().parent.parent / "templates" / "routing-policy.yaml"


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
        "default_params": {
            "consecutive": 2, "diff_files_threshold": 20, "open_blocker_threshold": 1,
        },
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

# Dependency manifests and lockfiles across the ecosystems Waystone can identify by path alone.
# Gradle dependency locking uses either root `gradle.lockfile` or files below
# `gradle/dependency-locks/*.lockfile`; both are handled explicitly in `_is_dependency_manifest`.
_MANIFEST_NAMES = frozenset({
    "Cargo.lock", "Cargo.toml", "Cartfile", "Cartfile.resolved", "Directory.Packages.props",
    "Gemfile", "Gemfile.lock", "Package.resolved", "Package.swift", "Pipfile", "Pipfile.lock",
    "Podfile", "Podfile.lock", "build.gradle", "build.gradle.kts", "build.sbt", "bun.lock",
    "bun.lockb", "composer.json", "composer.lock", "deno.lock", "deps.edn", "flake.lock",
    "flake.nix", "go.mod", "go.sum", "go.work", "go.work.sum", "gradle.lockfile", "mix.exs",
    "mix.lock", "package-lock.json", "package.json", "packages.lock.json", "pak.lock",
    "pnpm-lock.yaml", "poetry.lock", "pom.xml", "project.clj", "pubspec.lock", "pubspec.yaml",
    "pyproject.toml", "renv.lock", "settings.gradle", "settings.gradle.kts", "uv.lock",
    "yarn.lock",
})
_REQUIREMENTS_RE = re.compile(r"^requirements[^/]*\.txt$")
_GRADLE_LOCK_RE = re.compile(r"(?:^|/)gradle/dependency-locks/[^/]+\.lockfile$")


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
    return (name in _MANIFEST_NAMES or _REQUIREMENTS_RE.fullmatch(name) is not None
            or _GRADLE_LOCK_RE.search(path) is not None)


def evaluate_env_manifest_mutation(round_record: dict) -> dict:
    """Return unapproved dependency-manifest paths from one immutable round observation.

    A manifest is accounted for only by an env_prep change in the same commit interval or by a
    structured task scope that contains the path. Natural-language task fields are never mined.
    """
    from common import _path_in_declared_scope

    has_snapshot = isinstance(round_record.get("round_evidence"), dict) or any(
        key in round_record for key in ("manifest_paths", "task_scopes", "env_prep_change_kind"))
    payload = _round_payload(round_record)
    manifests = sorted({path for path in (payload.get("manifest_paths") or [])
                        if isinstance(path, str) and _is_dependency_manifest(path)})
    if not has_snapshot:
        return {"evaluable": False, "fired": False, "fires": [],
                "manifest_paths": manifests, "coverage_reason": "round-snapshot-unavailable"}
    snapshot_reason = payload.get("coverage_reason")
    if payload.get("evaluable") is not True and snapshot_reason != "task-scope-unknown":
        return {"evaluable": False, "fired": False, "fires": [], "manifest_paths": manifests,
                "coverage_reason": snapshot_reason or "round-diff-unavailable"}
    scopes = payload.get("task_scopes") if isinstance(payload.get("task_scopes"), dict) else {}
    scope_coverage = (payload.get("task_scope_coverage")
                      if isinstance(payload.get("task_scope_coverage"), dict) else {
                          task_id: ("explicit" if prefixes else "scope-unknown")
                          for task_id, prefixes in scopes.items()
                      })
    referenced = {
        path for path in manifests
        if any(isinstance(prefixes, list) and _path_in_declared_scope(path, prefixes)
               for prefixes in scopes.values())
    }
    remaining = sorted(set(manifests) - referenced)
    change_kind = payload.get("env_prep_change_kind")
    meaningful_env_update = change_kind in ("added", "updated")
    coverage_reason = None
    if not remaining or meaningful_env_update:
        fires = []
    elif any(reason in ("task-scope-invalid", "scope-invalid")
             for reason in scope_coverage.values()):
        fires = []
        coverage_reason = "task-scope-invalid"
    elif (snapshot_reason == "task-scope-unknown" or not scope_coverage
          or any(reason in ("task-scope-unknown", "scope-unknown")
                 for reason in scope_coverage.values())):
        fires = []
        coverage_reason = "task-scope-unknown"
    else:
        fires = remaining
    evaluable = coverage_reason is None
    return {
        "evaluable": evaluable, "fired": bool(fires) if evaluable else False,
        "fires": fires, "manifest_paths": manifests,
        "referenced_manifest_paths": sorted(referenced),
        "env_prep_before": payload.get("env_prep_before"),
        "env_prep_after": payload.get("env_prep_after"),
        "env_prep_change_kind": change_kind,
        "coverage_reason": coverage_reason,
    }


def evaluate_done_without_evidence(round_record: dict) -> dict:
    """Find done transitions lacking a satisfied apply verdict or structured main verification."""
    if not isinstance(round_record.get("round_evidence"), dict) and not any(
            key in round_record for key in ("done_task_ids", "done_evidence")):
        return {"evaluable": False, "fired": False, "fires": [], "done_task_ids": [],
                "unknown_task_ids": [], "coverage_reason": "round-snapshot-unavailable"}
    payload = _round_payload(round_record)
    done_ids = sorted({task_id for task_id in (payload.get("done_task_ids") or [])
                       if isinstance(task_id, str)})
    raw_rows = payload.get("done_evidence")
    if not isinstance(raw_rows, list):
        return {"evaluable": False, "fired": False, "fires": [],
                "done_task_ids": done_ids, "unknown_task_ids": done_ids,
                "coverage_reason": "done-evidence-unavailable"}
    grouped: dict[str, list[dict]] = {}
    for row in raw_rows:
        if isinstance(row, dict) and isinstance(row.get("task_id"), str):
            grouped.setdefault(row["task_id"], []).append(row)
    fires: list[str] = []
    unknown: list[str] = []
    unknown_reasons: list[str] = []
    errors = 0
    for task_id in done_ids:
        rows = grouped.get(task_id, [])
        if len(rows) != 1:
            unknown.append(task_id)
            unknown_reasons.append("done-evidence-missing" if not rows else "done-evidence-conflict")
            continue
        row = rows[0]
        errors += row.get("evaluation_errors", 0) if type(row.get("evaluation_errors", 0)) is int else 1
        if row.get("evaluable") is not True:
            unknown.append(task_id)
            unknown_reasons.append(row.get("coverage_reason") or "done-evidence-unavailable")
        elif row.get("positive") is not True:
            fires.append(task_id)
    evaluable = bool(fires) or not unknown
    coverage_reason = None
    if not evaluable:
        coverage_reason = (unknown_reasons[0] if len(set(unknown_reasons)) == 1
                           else "done-evidence-partial")
    return {"evaluable": evaluable, "fired": bool(fires), "fires": fires,
            "coverage_reason": coverage_reason, "done_task_ids": done_ids,
            "evidence_rows": sum(len(rows) for rows in grouped.values()),
            "unknown_task_ids": unknown, "evaluation_errors": errors}


def _logical_round_rows(rounds: list[dict]) -> list[dict]:
    """Latest immutable exposure per logical round id; a re-close remains one guard opportunity."""
    def order(row: dict) -> tuple[str, int, str]:
        path = Path(row.get("_file") or "")
        base = f"round-{row.get('round_id')}"
        suffix = path.stem.removeprefix(base + "-") if path.stem.startswith(base + "-") else ""
        return row.get("at") or "", int(suffix) if suffix.isdigit() else 1, str(path)

    latest: dict[str, dict] = {}
    for row in rounds:
        round_id = row.get("round_id") if isinstance(row, dict) else None
        if not isinstance(round_id, str) or not isinstance(row.get("at"), str):
            continue
        if round_id not in latest or order(row) > order(latest[round_id]):
            latest[round_id] = row
    return sorted(latest.values(), key=lambda row: (row["at"], row["round_id"], row.get("_file") or ""))


def evaluate_review_skipped_closes(rounds: list[dict], ingests: list[dict], *,
                                   consecutive: int = 2, diff_files_threshold: int = 20,
                                   open_blocker_threshold: int = 1) -> dict:
    """Evaluate logical close streaks over canonical packet/PR review-feedback events."""
    if type(consecutive) is not int or consecutive < 1:
        raise WorkflowError("review-skipped-closes-v1 consecutive must be a positive integer")
    if type(diff_files_threshold) is not int or diff_files_threshold < 1:
        raise WorkflowError(
            "review-skipped-closes-v1 diff_files_threshold must be a positive integer")
    if type(open_blocker_threshold) is not int or open_blocker_threshold < 1:
        raise WorkflowError(
            "review-skipped-closes-v1 open_blocker_threshold must be a positive integer")
    closes = _logical_round_rows(rounds)
    review_events = sorted(
        (row for row in ingests if isinstance(row, dict) and isinstance(row.get("at"), str)
         and row.get("event", "review-feedback") == "review-feedback"),
        key=lambda row: (row["at"], row.get("round_id") or "", row.get("source_pointer") or ""),
    )
    event_index = 0
    streak = 0
    fires: list[str] = []
    by_round: list[dict] = []
    previous_close_at = ""
    unknown: Counter = Counter()

    def recognized_feedback(event: dict) -> bool:
        if event.get("source") == "pr-marker":
            return True
        narrative_bound = (
            event.get("narrative_digest_matches") is True
            or event.get("narrative_coverage_reason") == "legacy-pre-digest"
        )
        request_bound = (
            event.get("rendered_request_digest_matches") is True
            or event.get("rendered_request_coverage_reason")
            == "request-digest-missing-legacy-fallback"
        )
        return (event.get("reviewer_configured") is True
                and narrative_bound and request_bound)

    for close in closes:
        interval_events = []
        while event_index < len(review_events) and review_events[event_index]["at"] <= close["at"]:
            if review_events[event_index]["at"] > previous_close_at:
                interval_events.append(review_events[event_index])
            event_index += 1
        recognized_events = [
            event for event in interval_events if recognized_feedback(event)
        ]
        unknown_events = [event for event in interval_events if event not in recognized_events]
        for event in unknown_events:
            reason = event.get("reviewer_coverage_reason")
            if event.get("reviewer_configured") is True:
                request_bound = (
                    event.get("rendered_request_digest_matches") is True
                    or event.get("rendered_request_coverage_reason")
                    == "request-digest-missing-legacy-fallback"
                )
                if not request_bound:
                    reason = (event.get("rendered_request_coverage_reason")
                              or "request-digest-verdict-unavailable")
                else:
                    reason = (event.get("narrative_coverage_reason")
                              or "narrative-digest-verdict-unavailable")
            unknown[reason or "reviewer-identity-unavailable"] += 1
        payload = _round_payload(close)
        review_mode = close.get("review_mode", payload.get("review_mode"))
        reason = None
        if review_mode not in ("packet", "pr"):
            reason = "review-mode-unavailable"
        elif review_mode == "pr" and not any(event.get("source") == "pr-marker"
                                              for event in recognized_events):
            reason = "pr-state-unavailable"
        if reason is not None:
            unknown[reason] += 1
            streak = 0
            by_round.append({"round_id": close["round_id"], "streak": None, "fired": False,
                             "evaluable": False, "coverage_reason": reason,
                             "feedback_observed": None})
            previous_close_at = close["at"]
            continue
        saw_feedback = bool(recognized_events)
        feedback_observed = True if saw_feedback else None if unknown_events else False
        streak = 1 if saw_feedback else streak + 1
        changed_files = payload.get("changed_files")
        open_blockers = payload.get("open_blocker_task_ids")
        risk_reason = None
        if not saw_feedback and isinstance(changed_files, list) \
                and len(changed_files) >= diff_files_threshold:
            risk_reason = "diff-files-threshold"
        elif not saw_feedback and isinstance(open_blockers, list) \
                and len(open_blockers) >= open_blocker_threshold:
            risk_reason = "open-blocker-threshold"
        risk_known = saw_feedback or (isinstance(changed_files, list)
                                      and isinstance(open_blockers, list))
        fired = streak >= consecutive or risk_reason is not None
        if not risk_known:
            unknown["high-risk-input-unavailable"] += 1
        if fired:
            fires.append(close["round_id"])
        by_round.append({"round_id": close["round_id"], "streak": streak, "fired": fired,
                         "evaluable": True, "coverage_reason": None,
                         "risk_reason": risk_reason, "risk_evaluable": risk_known,
                         "feedback_observed": feedback_observed})
        previous_close_at = close["at"]
    opportunities = sum(row["evaluable"] for row in by_round)
    feedback_coverage = {
        "observed": sum(row.get("feedback_observed") is True for row in by_round),
        "absent": sum(row.get("feedback_observed") is False for row in by_round),
        "unknown": sum(row.get("feedback_observed") is None for row in by_round),
        "unknown_reasons": dict(sorted(
            (reason, count) for reason, count in unknown.items()
            if reason.startswith("reviewer-") or reason.startswith("configured-reviewer-"))),
    }
    return {"evaluable": bool(opportunities), "fired": bool(fires),
            "coverage_reason": None if opportunities else "review-state-unavailable",
            "opportunities": opportunities, "fires": fires, "by_round": by_round,
            "consecutive": consecutive,
            "diff_files_threshold": diff_files_threshold,
            "open_blocker_threshold": open_blocker_threshold,
            "feedback_coverage": feedback_coverage,
            "unknown_reviewer_feedback": sum(feedback_coverage["unknown_reasons"].values()),
            "unevaluable_pr_state": unknown["pr-state-unavailable"],
            "unevaluable_review_mode": unknown["review-mode-unavailable"],
            "unevaluable_high_risk": unknown["high-risk-input-unavailable"]}


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


def record_review_feedback(root: Path, round_id: str, *, source: str, event_id: str,
                           reviewer: str | None = None, reply_header: dict | None = None,
                           force: bool = False) -> dict:
    """Append one canonical review-feedback event shared by packet ingest and completed PR markers."""
    if not isinstance(round_id, str) or not round_id:
        raise WorkflowError("review feedback round_id must be non-empty")
    if source not in ("packet-ingest", "pr-marker"):
        raise WorkflowError("review feedback source must be packet-ingest|pr-marker")
    if not isinstance(event_id, str) or not event_id:
        raise WorkflowError("review feedback event_id must be non-empty")
    if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip()):
        raise WorkflowError("review feedback reviewer must be a non-empty string when provided")
    reviewer_effort = None
    review_target = None
    reply_metadata = None
    if source == "packet-ingest":
        reply_header = reply_header if isinstance(reply_header, dict) else {
            "metadata": {}, "model": reviewer, "effort": None, "review_target": None,
        }
        reviewer = reply_header.get("model")
        reviewer_effort = reply_header.get("effort")
        review_target = reply_header.get("review_target")
        reply_metadata = reply_header.get("metadata") or {}
    row = {
        "schema": REVIEW_FEEDBACK_SCHEMA, "event": "review-feedback", "at": _now_iso(),
        "round_id": round_id, "source": source, "event_id": event_id,
        "reviewer": reviewer, "provenance": "observed",
    }
    if source == "packet-ingest":
        row.update({
            "reviewer_effort": reviewer_effort, "review_target": review_target,
            "reply_metadata": reply_metadata,
        })
    else:
        row.update({"reviewer_configured": True, "reviewer_coverage_reason": None})
    _ensure_project_state_or_refuse(root)
    path = _review_ingests_path(root)
    _mkdir_or_refuse(path.parent)
    if force and source == "packet-ingest" and path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        output: list[str] = []
        replaced = False
        for line in lines:
            try:
                prior = json.loads(line)
            except json.JSONDecodeError:
                output.append(line)
                continue
            prior_event_id = prior.get("event_id") if isinstance(prior, dict) else None
            same_round = (isinstance(prior, dict)
                          and prior.get("source") == "packet-ingest"
                          and (prior.get("round_id") == round_id
                               or (isinstance(prior_event_id, str)
                                   and prior_event_id.startswith(
                                       f"packet:{round_id}:reviewer:"))))
            if same_round:
                if not replaced:
                    output.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
                    replaced = True
                continue
            output.append(line)
        if not replaced:
            output.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        write_text_atomic(path, "\n".join(output) + "\n")
        return row
    existing, _skipped = load_review_ingests(root)
    prior = next((item for item in existing if item.get("event_id") == event_id), None)
    if prior is not None:
        return {key: prior.get(key) for key in row}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def record_review_ingest(root: Path, round_id: str, reviewer: str | None = None,
                         *, reply_header: dict | None = None, force: bool = False) -> dict:
    """Compatibility wrapper: a packet ingest projects to the canonical feedback event."""
    return record_review_feedback(
        root, round_id, source="packet-ingest",
        event_id=f"packet:{round_id}", reviewer=reviewer, reply_header=reply_header,
        force=force)


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
    try:
        cfg = load_config(root)
    except (OSError, WorkflowError):
        cfg = None
    import review

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if (not isinstance(row, dict) or row.get("schema") != REVIEW_FEEDBACK_SCHEMA
                or row.get("event") != "review-feedback"
                or parse_iso_timestamp(row.get("at")) is None
                or not isinstance(row.get("round_id"), str)
                or review._round_request_binding_date(row.get("round_id")) is None
                or row.get("source") not in ("packet-ingest", "pr-marker")
                or not isinstance(row.get("event_id"), str)
                or (row.get("reviewer") is not None
                    and (not isinstance(row.get("reviewer"), str)
                         or not row["reviewer"].strip()))
                or (row.get("reviewer_coverage_reason") is not None
                    and not isinstance(row.get("reviewer_coverage_reason"), str))):
            skipped += 1
            continue
        if row["source"] == "pr-marker":
            row = {**row, "reviewer_configured": True,
                   "reviewer_coverage_reason": None}
        else:
            binding = None
            if cfg is not None:
                binding, _binding_reason = review.ingest_round_binding(
                    root, row["round_id"], cfg)
                feedback = root / cfg["reviews_dir"] / f"{row['round_id']}-feedback.md"
            else:
                feedback = root / "__unavailable-feedback__"
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=row["round_id"], binding=binding,
                request_generation_dir=(project_state_path(root) / "review-requests"
                                        if cfg is not None
                                        and (cfg.get("review") or {}).get("mode") == "pr"
                                        else feedback.parent))
            row = {
                **row,
                "reviewer": projected["model"],
                "reviewer_effort": projected["effort"],
                "review_target": projected["review_target"],
                "review_target_matches": projected["review_target_matches"],
                "reviewer_configured": projected["reviewer_configured"],
                "reviewer_coverage_reason": projected["reviewer_coverage_reason"],
                "narrative_digest_matches": projected["narrative_digest_matches"],
                "narrative_coverage_reason": projected["narrative_coverage_reason"],
                "rendered_request_digest_matches":
                    projected["rendered_request_digest_matches"],
                "rendered_request_coverage_reason":
                    projected["rendered_request_coverage_reason"],
                "reply_metadata": projected["metadata"],
            }
        rows.append({**row, "source_pointer": f"{path}:{line_number}"})
    deduped = {row["event_id"]: row for row in rows}
    rows = list(deduped.values())
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
            "schema": REVIEW_FEEDBACK_SCHEMA, "event": "review-feedback",
            "source": "packet-ingest", "event_id": f"legacy-packet:{feedback_round}",
            "provenance": "feedback-file-between-close-approximation",
            "reviewer": None, "reviewer_configured": None,
            "reviewer_coverage_reason": "reviewer-identity-unavailable",
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
    if path.exists():
        raise WorkflowError(f"user overlay delta {delta['id']} already exists at {path}")
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(delta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def _registered_canonical_roots(root: Path) -> list[Path]:
    source = registry_path()
    registry = _read_registry(source)
    wanted = Path(root).resolve()
    current_registered = False
    roots = []
    for entry in registry["projects"]:
        identities = registry_entry_paths(entry, source)
        if not identities:
            continue
        roots.append(identities[0])
        current_registered = current_registered or wanted in identities
    if not current_registered:
        raise WorkflowError(
            f"promote-user requires the source project to be registered by canonical path: {wanted}")
    return roots


def _policy_params_fingerprint(rule: str, params: dict) -> str:
    return canonical_payload_hash({"rule": rule, "params": params})


def _warnings_observe_candidate(root: Path, delta: dict) -> bool:
    path = _warnings_path(root)
    if not path.is_file():
        return False
    identity = {"layer": "project", "id": delta["id"]}
    fingerprint = _policy_params_fingerprint(delta["rule"], dict(delta.get("params") or {}))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(row, dict) and row.get("rule") == delta["rule"]
                and row.get("event") == "evaluation"
                and row.get("delta_status") in ACTIVE_STATUSES
                and row.get("policy_identity") == identity
                and row.get("origin_delta_id") == delta["id"]
                and row.get("params_fingerprint") == fingerprint
                and parse_iso_timestamp(row.get("at")) is not None):
            return True
    return False


def _derived_observed_projects(root: Path, delta: dict) -> list[str]:
    projects = _registered_canonical_roots(root)
    return sorted(str(project) for project in projects
                  if _warnings_observe_candidate(project, delta))


def _promote_user_locked(root: Path, delta_id: str) -> dict:
    delta = load_delta(root, delta_id)
    if delta.get("candidate_scope") != "user_candidate":
        raise WorkflowError(
            f"delta {delta_id} candidate_scope is {delta.get('candidate_scope')!r}; "
            "promote-user requires user_candidate")
    projects = _derived_observed_projects(root, delta)
    if len(projects) < 2:
        raise WorkflowError(
            f"delta {delta_id} has evidence from {len(projects)} distinct project(s); "
            "promote-user requires observed_in evidence from at least 2 distinct projects")
    if delta.get("status") not in ACTIVE_STATUSES:
        raise WorkflowError(
            f"delta {delta_id} is {delta.get('status')}; promote-user requires an active delta")
    promoted = json.loads(json.dumps(delta))
    promoted["scope"] = {"kind": "user"}
    promoted["origin_delta_id"] = delta_id
    promoted["observed_in"] = projects
    promoted["promoted_from"] = {
        "project": _project_slug(root), "delta_id": delta_id, "observed_in": projects,
        "at": _now_iso(),
    }
    _write_new_user_delta(promoted)
    return promoted


def promote_user(root: Path, delta_id: str) -> dict:
    """Atomically promote one independently observed candidate under the global lock order."""
    with hold_lock(registry_lock_path()):
        with hold_lock(overlay_lock_path()):
            with hold_project_lock(root):
                return _promote_user_locked(root, delta_id)


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
            "identity": {"layer": layer, "id": delta["id"]},
            "origin_delta_id": delta.get("origin_delta_id") or delta["id"],
        })
    return out


def _base_policies() -> list[dict]:
    """Machine-composable layer 0 defaults; resolved even before an adaptive layer activates."""
    return [{
        "id": f"base/{rule_id}", "rule": rule_id, "stage": "observing",
        "status": "observing", "params": dict(rule.get("default_params") or {}),
        "layer": "base", "source_kind": "built-in", "enabled": False,
        "identity": {"layer": "base", "id": f"base/{rule_id}"},
    } for rule_id, rule in sorted(RULES.items())]


def _validate_rule_params(rule: str, params: dict, label: str) -> None:
    if rule not in RULES:
        raise WorkflowError(f"{label} references unknown rule {rule!r}")
    if rule == "round-close-open-findings-v1":
        severities = params.get("severities")
        if (set(params) != {"severities"} or not isinstance(severities, list) or not severities
                or any(item not in ("blocker", "major", "minor") for item in severities)
                or len(set(severities)) != len(severities)):
            raise WorkflowError(f"{label} has invalid params for {rule}")
        return
    if rule == "review-skipped-closes-v1":
        consecutive = params.get("consecutive")
        diff_threshold = params.get("diff_files_threshold")
        blocker_threshold = params.get("open_blocker_threshold")
        if (set(params) != {"consecutive", "diff_files_threshold", "open_blocker_threshold"}
                or any(type(value) is not int or value < 1
                       for value in (consecutive, diff_threshold, blocker_threshold))):
            raise WorkflowError(f"{label} has invalid params for {rule}")
        return
    if params:
        raise WorkflowError(f"{label} has invalid params for {rule}: expected an empty object")


def _load_project_policy(root: Path) -> list[dict]:
    path = _project_policy_path(root)
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise WorkflowError(f"corrupt committed project policy {path} ({e})") from e
    if (not isinstance(document, dict) or document.get("schema") != PROJECT_POLICY_SCHEMA
            or not isinstance(document.get("policies"), list)
            or set(document) != {"schema", "policies"}):
        raise WorkflowError(f"corrupt committed project policy {path}")
    out = []
    seen_ids = set()
    for index, policy in enumerate(document["policies"]):
        label = f"committed project policy {path}: policies[{index}]"
        if (not isinstance(policy, dict) or not isinstance(policy.get("id"), str)
                or POLICY_ID_RE.fullmatch(policy["id"]) is None
                or not isinstance(policy.get("rule"), str)
                or policy.get("stage") not in POLICY_STAGES
                or not isinstance(policy.get("params"), dict)
                or not isinstance(policy.get("summary"), str)
                or not policy["summary"].strip()
                or "\n" in policy["summary"] or "\r" in policy["summary"]
                or set(policy) != {"id", "rule", "stage", "params", "summary"}
                or policy["id"] in seen_ids):
            raise WorkflowError(f"corrupt {label}")
        seen_ids.add(policy["id"])
        _validate_rule_params(policy["rule"], policy["params"], label)
        if policy["stage"] == "enforce":
            raise WorkflowError(
                f"committed project policy {policy['id']} requests enforce, which is not reachable "
                "until the Adapt & Enforce arc")
        runtime = {
            "id": policy["id"], "rule": policy["rule"], "stage": policy["stage"],
            "status": policy["stage"], "params": dict(policy.get("params") or {}),
            "layer": "project", "source_kind": "committed", "enabled": True,
            "identity": {"layer": "project", "id": policy["id"]},
        }
        origin = _materialization_origins(root).get(policy["id"])
        if origin is not None:
            runtime["origin_delta_id"] = origin
        out.append(runtime)
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
        "reason": reason.strip(), "round_id": round_id, "at": _now_iso(),
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
            or parse_iso_timestamp(document.get("created_at")) is None
            or not isinstance(document.get("overrides"), list)):
        raise WorkflowError(f"corrupt round override {path}")
    if document.get("expired_at") is not None:
        return []
    closed_rounds, _errors = _round_records(root)
    created_at = parse_iso_timestamp(document["created_at"])
    durable_close = None if round_id == document["round_id"] else next((
        row for row in closed_rounds
        if row["round_id"] == document["round_id"]
        and parse_iso_timestamp(row["at"]) >= created_at), None)
    if durable_close is not None:
        document["expired_at"] = _now_iso()
        document["expiry_reason"] = "durable-round-close"
        write_text_atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        print(f"waystone overlay: recovered expired override for closed round "
              f"{document['round_id']}", file=sys.stderr)
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
                or entry.get("round_id") != document["round_id"]
                or entry["rule"] in seen):
            raise WorkflowError(f"corrupt round override {path}: overrides[{index}]")
        seen.add(entry["rule"])
        out.append({
            "id": entry["id"], "rule": entry["rule"], "stage": entry["stage"],
            "status": entry["stage"], "params": dict(entry.get("params") or {}),
            "layer": "round", "source_kind": "override", "enabled": True,
            "round_id": document["round_id"], "reason": entry["reason"],
            "identity": {"layer": "round", "id": entry["id"]},
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
        source_identities = sorted(
            (item["identity"] for item in group), key=lambda item: (item["layer"], item["id"]))
        effective = {**winner, "source_identities": source_identities}
        resolved.append(effective)
        if len(group) > 1:
            conflicts.append({
                "rule": rule, "scope": scope, "identities": source_identities,
                "effective_identity": winner["identity"], "effective_stage": winner["stage"],
                "resolution": "least-restrictive",
            })
            shadowed.extend({
                "identity": item["identity"], "rule": rule, "layer": item["layer"],
                "source_kind": item["source_kind"], "shadowed_by": winner["identity"],
                "origin_delta_id": item.get("origin_delta_id"),
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
    identities: dict[tuple[str, str], dict] = {}
    for policy in (*base, *user, *local, *committed, *round_policies):
        identity = policy.get("identity")
        key = (identity.get("layer"), identity.get("id")) if isinstance(identity, dict) else None
        if key is None or not all(isinstance(value, str) and value for value in key):
            raise WorkflowError("policy composition contains an invalid policy identity")
        if key in identities:
            raise WorkflowError(
                f"duplicate policy identity {key[0]}:{key[1]} in policy composition")
        identities[key] = policy
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
                conflicts.append({
                    "rule": rule, "scope": "project",
                    "identities": sorted(
                        [*(item["identity"] for item in local_group),
                         *(item["identity"] for item in committed_group)],
                        key=lambda item: (item["layer"], item["id"])),
                    "effective_identity": winner["identity"],
                    "effective_stage": winner["stage"],
                    "resolution": "committed-wins",
                    "shadowed": [item["identity"] for item in sorted(
                        local_group, key=lambda item: item["id"])],
                })
                shadowed.extend({
                    "identity": item["identity"], "rule": rule, "layer": "project",
                    "source_kind": "overlay", "shadowed_by": winner["identity"],
                    "origin_delta_id": item.get("origin_delta_id"),
                    "reason": "committed-wins",
                } for item in sorted(local_group, key=lambda item: item["id"]))
        else:
            project_candidates.extend(_resolve_same_scope(
                local_group, "project", conflicts, shadowed))

    resolved_layers = {
        "base": [{**policy, "source_identities": [policy["identity"]]} for policy in base],
        "user": _resolve_same_scope(user, "user", conflicts, shadowed),
        "project": project_candidates,
        # One override per rule is enforced at write/load, but resolving here keeps the invariant
        # explicit if the representation evolves.
        "round": _resolve_same_scope(round_policies, "round", conflicts, shadowed),
    }
    effective_by_rule: dict[str, dict] = {
        policy["rule"]: policy for policy in resolved_layers["base"]}
    for layer in ("user", "project", "round"):
        for policy in resolved_layers[layer]:
            previous = effective_by_rule.get(policy["rule"])
            if previous is not None:
                shadowed.append({
                    "identity": previous["identity"], "rule": previous["rule"],
                    "layer": previous["layer"], "source_kind": previous["source_kind"],
                    "shadowed_by": policy["identity"],
                    "origin_delta_id": previous.get("origin_delta_id"),
                    "reason": "narrower-scope",
                })
            effective_by_rule[policy["rule"]] = policy

    if any(row["identity"] == row["shadowed_by"] for row in shadowed):
        raise WorkflowError("policy composition produced a self-shadow identity")
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
            item["rule"], item["scope"], item["resolution"],
            item["effective_identity"]["layer"], item["effective_identity"]["id"])),
        "shadowed": sorted(shadowed, key=lambda item: (
            item["rule"], item["layer"], item["identity"]["id"], item["reason"])),
    }


def _neutral_policy_id(rule: str) -> str:
    policy_id = re.sub(r"-v[0-9]+$", "", rule)
    if POLICY_ID_RE.fullmatch(policy_id) is None:
        raise WorkflowError(f"rule {rule!r} cannot produce a neutral committed policy id")
    return policy_id


def _materialization_map_path(root: Path) -> Path:
    return _overlay_dir(root) / "materializations.json"


def _load_materialization_map(root: Path) -> dict:
    path = _materialization_map_path(root)
    if not path.is_file():
        return {"schema": MATERIALIZATION_MAP_SCHEMA, "mappings": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt local materialization mapping {path} ({e})") from e
    if (not isinstance(document, dict) or set(document) != {"schema", "mappings"}
            or document.get("schema") != MATERIALIZATION_MAP_SCHEMA
            or not isinstance(document.get("mappings"), list)):
        raise WorkflowError(f"corrupt local materialization mapping {path}")
    seen = set()
    for row in document["mappings"]:
        if (not isinstance(row, dict)
                or set(row) != {"policy_id", "origin_delta_id", "rule", "candidate_hash", "at"}
                or POLICY_ID_RE.fullmatch(str(row.get("policy_id") or "")) is None
                or DELTA_ID_RE.fullmatch(str(row.get("origin_delta_id") or "")) is None
                or not isinstance(row.get("rule"), str)
                or not isinstance(row.get("candidate_hash"), str)
                or parse_iso_timestamp(row.get("at")) is None
                or row["policy_id"] in seen):
            raise WorkflowError(f"corrupt local materialization mapping {path}")
        seen.add(row["policy_id"])
    return document


def _materialization_origins(root: Path) -> dict[str, str]:
    return {row["policy_id"]: row["origin_delta_id"]
            for row in _load_materialization_map(root)["mappings"]}


def _write_materialization_mapping(root: Path, delta: dict, candidate: dict) -> None:
    document = _load_materialization_map(root)
    if any(row["policy_id"] == candidate["id"] for row in document["mappings"]):
        raise WorkflowError(
            f"local materialization mapping for policy {candidate['id']} already exists")
    document["mappings"].append({
        "policy_id": candidate["id"], "origin_delta_id": delta["id"],
        "rule": candidate["rule"], "candidate_hash": canonical_payload_hash(candidate),
        "at": _now_iso(),
    })
    path = _materialization_map_path(root)
    _mkdir_or_refuse(path.parent)
    write_text_atomic(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _materialized_candidate(root: Path, delta: dict) -> dict:
    del root
    summary = f"Project policy for {re.sub(r'-v[0-9]+$', '', delta['rule']).replace('-', ' ')}."
    identity_hash = canonical_payload_hash({
        "origin_delta_id": delta["id"], "rule": delta["rule"],
        "params": dict(delta.get("params") or {}),
    })
    return {
        "id": f"{_neutral_policy_id(delta['rule'])}-{identity_hash[:12]}",
        "rule": delta["rule"], "stage": delta["status"],
        "params": dict(delta.get("params") or {}), "summary": summary,
    }


def materialize_consent_context(root: Path, delta_id: str) -> dict:
    delta = load_delta(root, delta_id)
    if delta.get("status") not in ACTIVE_STATUSES:
        raise WorkflowError(f"delta {delta_id} must be active before materialization")
    if not isinstance(delta.get("replay"), dict):
        raise WorkflowError(
            f"delta {delta_id} has no replay evidence; run `waystone overlay replay {delta_id}` first")
    candidate = _materialized_candidate(root, delta)
    _validate_rule_params(candidate["rule"], candidate["params"], f"delta {delta_id}")
    return {
        "origin_delta_id": delta_id,
        "target_path": str(_project_policy_path(root).resolve()),
        "candidate_hash": canonical_payload_hash(candidate),
        "stage": delta["status"],
    }


def materialize(root: Path, delta_id: str, *, consent_recorded: bool = False) -> Path:
    """Promote one replay-validated local delta into the commit-target project policy file."""
    delta = load_delta(root, delta_id)
    if delta.get("status") not in ACTIVE_STATUSES:
        raise WorkflowError(f"delta {delta_id} must be active before materialization")
    if not isinstance(delta.get("replay"), dict):
        raise WorkflowError(
            f"delta {delta_id} has no replay evidence; run `waystone overlay replay {delta_id}` first")
    path = _project_policy_path(root)
    candidate = _materialized_candidate(root, delta)
    _validate_rule_params(candidate["rule"], candidate["params"], f"delta {delta_id}")
    document = {"schema": PROJECT_POLICY_SCHEMA, "policies": []}
    if path.is_file():
        # Validate the existing document through the same strict loader before preserving it.
        _load_project_policy(root)
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise WorkflowError(f"corrupt committed project policy {path} ({e})") from e
    if any(policy.get("id") == candidate["id"] for policy in document["policies"]
           if isinstance(policy, dict)):
        raise WorkflowError(
            f"committed project policy {candidate['id']} already exists in {path}")
    context = materialize_consent_context(root, delta_id)
    if consent_recorded:
        record_consent(root, "materialize", "accept", context)
    if not has_accepted_consent(root, "materialize", context):
        raise WorkflowError(
            f"materialize consent is required for the current {delta_id} candidate; "
            "record acceptance after inspecting its bound path, stage, and hash")
    document["policies"].append(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    policy_before = path.read_bytes() if path.is_file() else None
    mapping_path = _materialization_map_path(root)
    mapping_before = mapping_path.read_bytes() if mapping_path.is_file() else None
    try:
        write_text_atomic(path, text)
        _write_materialization_mapping(root, delta, candidate)
    except Exception:
        if policy_before is None:
            path.unlink(missing_ok=True)
        else:
            write_text_atomic(path, policy_before.decode("utf-8"))
        if mapping_before is None:
            mapping_path.unlink(missing_ok=True)
        else:
            write_text_atomic(mapping_path, mapping_before.decode("utf-8"))
        raise
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
    if from_rec is not None:
        import improve

        existing = list_deltas(root)
        if any(delta.get("corrupt") for delta in existing):
            raise WorkflowError("cannot prove --from-rec uniqueness while an overlay delta is corrupt")
        matches = [delta for delta in existing
                   if isinstance(delta.get("evidence"), dict)
                   and delta["evidence"].get("source") == "improve-rec"
                   and delta["evidence"].get("rec_id") == from_rec]
        if matches:
            raise WorkflowError(
                f"recommendation {from_rec} already materialized as delta {matches[0].get('id')}")
        decisions, _skipped = improve._load_decisions(root)
        matching_decisions = [row for row in decisions if row["rec_id"] == from_rec]
        if not matching_decisions or matching_decisions[-1]["decision"] != "accept":
            raise WorkflowError(f"--from-rec {from_rec} requires a latest accepted improve decision")
        accepted_at = parse_iso_timestamp(matching_decisions[-1]["at"])
        created_at = parse_iso_timestamp(now)
        if accepted_at is None or created_at is None or accepted_at > created_at:
            raise WorkflowError(
                f"--from-rec {from_rec} acceptance timestamp must not follow delta creation")
    delta = {
        "schema": "waystone-delta-1",
        "id": delta_id,
        "title": title or delta_id,
        "rule": rule,
        "params": dict(RULES[rule].get("default_params") or {}),
        "scope": {"pslug": pslug, "root": str(Path(root).resolve())},
        "candidate_scope": candidate_scope,
        # A new composite identity cannot have prior observations. Promotion derives observed_in
        # later from exact durable evaluation rows; the compatibility argument stays ignored.
        "observed_in": [],
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
    start_level = load_config(root)["policy"]["start_level"]
    delta = _transition(root, delta_id, "warning", require_from="observing", replay_gate=True)
    if start_level == "observe-only":
        print(
            f"waystone overlay: promoted {delta_id} to warning, but stderr emission is suppressed "
            "by policy.start_level observe-only",
            file=sys.stderr,
        )
    return delta


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

    context: dict = {"delegation_id": did, "snapshot": did}
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
    unevaluable: Counter = Counter()
    round_rows: list[tuple[str | None, bool]] = []
    evaluations: list[dict] = []
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
                reason = drift.get("coverage_reason") or "scope-unavailable"
                unevaluable[reason] += 1
                errors += int(reason != "scope-unknown")
                continue
            fired = drift["fired"]
        else:
            raise WorkflowError(f"delegation replay does not implement {rule_id!r}")
        opportunities += 1
        round_id = _delegation_round(root, rec, context, rounds)
        round_rows.append((round_id, fired))
        evaluations.append({
            "subject_id": rec.name, "snapshot": rec.name, "round_id": round_id,
            "opportunities": 1, "fires": int(fired),
        })
        if fired:
            fires.append(f"{rec.name}/artifact/contract.yaml")
    return {
        "corpus": "delegations",
        "corpus_size": len(candidates),
        "opportunities": opportunities,
        "fires": len(fires),
        "examples": fires[:5],
        "evaluation_errors": errors,
        "unevaluable_delegations": sum(unevaluable.values()),
        "unevaluable_by_reason": dict(sorted(unevaluable.items())),
        "evaluations": evaluations,
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
                or not isinstance(row.get("round_id"), str)
                or parse_iso_timestamp(row.get("at")) is None):
            errors += 1
            continue
        rows.append({**row, "_file": str(path)})
    return _logical_round_rows(rows), errors


def _replay_rounds(root: Path, rule_id: str, params: dict) -> dict:
    rows, errors = _round_records(root)
    fired_rounds: list[str] = []
    by_round: list[dict] = []
    opportunities = 0
    unevaluable = 0
    unevaluable_by_reason: Counter = Counter()
    if rule_id == "review-skipped-closes-v1":
        ingests, ingest_errors, legacy_approximations = _review_ingests_for_rounds(root, rows)
        errors += ingest_errors
        result = evaluate_review_skipped_closes(
            rows, ingests, consecutive=params.get("consecutive", 2),
            diff_files_threshold=params.get("diff_files_threshold", 20),
            open_blocker_threshold=params.get("open_blocker_threshold", 1))
        fired_rounds = result["fires"]
        opportunities = result["opportunities"]
        by_round = [{"round_id": row["round_id"], "subject_id": row["round_id"],
                     "snapshot": row["round_id"],
                     "opportunities": int(row["evaluable"]),
                     "fires": int(row["fired"]), "streak": row["streak"],
                     "risk_reason": row.get("risk_reason"),
                     "evaluable": row["evaluable"],
                     "coverage_reason": row["coverage_reason"]}
                    for row in result["by_round"]]
        unevaluable = len(by_round) - opportunities
        unevaluable_by_reason.update(
            row["coverage_reason"] for row in by_round if not row["evaluable"])
    else:
        for row in rows:
            if rule_id == "env-manifest-mutation-v1":
                result = evaluate_env_manifest_mutation(row)
            elif rule_id == "done-without-evidence-v1":
                result = evaluate_done_without_evidence(row)
                errors += result.get("evaluation_errors", 0)
            else:
                raise WorkflowError(f"round replay does not implement {rule_id!r}")
            if result["evaluable"] is not True:
                unevaluable += 1
                unevaluable_by_reason[result.get("coverage_reason") or "unknown"] += 1
                by_round.append({"round_id": row["round_id"],
                                 "subject_id": row["round_id"], "snapshot": row["round_id"],
                                 "opportunities": 0, "fires": 0,
                                 "evaluable": False,
                                 "coverage_reason": result.get("coverage_reason")})
                continue
            opportunities += 1
            fired = result["fired"]
            if fired:
                fired_rounds.append(row["round_id"])
            by_round.append({"round_id": row["round_id"],
                             "subject_id": row["round_id"], "snapshot": row["round_id"],
                             "opportunities": 1,
                             "fires": int(fired), "evaluable": True,
                             "coverage_reason": None})
    report = {
        "corpus": "rounds", "corpus_size": len(rows), "opportunities": opportunities,
        "fires": len(fired_rounds), "examples": fired_rounds[:5],
        "evaluation_errors": errors, "unevaluable_rounds": unevaluable, "by_round": by_round,
        "unevaluable_by_reason": dict(sorted(unevaluable_by_reason.items())),
    }
    if rule_id == "review-skipped-closes-v1":
        report["legacy_ingest_approximations"] = legacy_approximations
        report["unevaluable_pr_state"] = result["unevaluable_pr_state"]
        report["unevaluable_review_mode"] = result["unevaluable_review_mode"]
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


def _emit(root: Path, boundary: str, policy: dict, rule: str, delta_status: str, event: str,
          message: str, context: dict) -> dict:
    """Append a warnings row; warning-stage fires and every policy conflict are visible on stderr."""
    identity = policy["identity"]
    start_level = load_config(root)["policy"]["start_level"]
    suppressed = (event == "fire" and delta_status == "warning"
                  and start_level == "observe-only")
    row = {"at": _now_iso(), "boundary": boundary, "policy_identity": identity, "rule": rule,
           "delta_status": delta_status, "event": event, "message": message, "context": context,
           "start_level": start_level, "suppressed_by_start_level": suppressed,
           "params_fingerprint": _policy_params_fingerprint(
               rule, dict(policy.get("params") or {})),
           "policy_source_kind": policy.get("source_kind")}
    if isinstance(policy.get("origin_delta_id"), str):
        row["origin_delta_id"] = policy["origin_delta_id"]
    _append_warning(root, row)
    display = f"{identity['layer']}:{identity['id']}"
    if event == "fire" and delta_status == "warning" and not suppressed:
        print(f"waystone warn [{display}]: {message}", file=sys.stderr)
    elif event == "conflict" and not suppressed:
        print(f"waystone warn conflict [{display}]: {message}", file=sys.stderr)
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
            rows, ingests, consecutive=params.get("consecutive", 2),
            diff_files_threshold=params.get("diff_files_threshold", 20),
            open_blocker_threshold=params.get("open_blocker_threshold", 1))
        result["evaluation_errors"] = round_errors + ingest_errors
        result["legacy_ingest_approximations"] = legacy_approximations
        current_rows = [row for row in result["by_round"]
                        if row["round_id"] == current["round_id"]]
        result["current_fired"] = bool(current_rows and current_rows[-1]["fired"])
        result["current_streak"] = current_rows[-1]["streak"] if current_rows else None
        result["current_risk_reason"] = (
            current_rows[-1].get("risk_reason") if current_rows else None)
        result["evaluable"] = bool(current_rows and current_rows[-1]["evaluable"])
        result["fired"] = result["current_fired"]
        result["coverage_reason"] = (
            current_rows[-1]["coverage_reason"] if current_rows else "round-snapshot-unavailable")
        return result
    return None


_RULE1_MSG = ("delegation {did} carries no delegate-side verification evidence — verify independently "
              "before apply (a delegate-claimed absence is a reporting gap, not proof of unverified work)")


def _emit_evaluations(root: Path, boundary: str, group: list[dict], rule_id: str,
                      fired: bool, context: dict) -> list[dict]:
    rows = []
    for delta in sorted(group, key=lambda item: item["id"]):
        rows.append(_emit(
            root, boundary, delta, rule_id, delta["status"], "evaluation",
            "rule evaluated at workflow boundary",
            {**context, "evaluable": True, "fired": fired, "coverage_reason": None}))
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
        unknown_context = {
            "evaluable": False, "fired": False, "coverage_reason": "evaluation-error",
        }
        for key in ("delegation_id", "task_id", "round_id"):
            if isinstance(context.get(key), str):
                unknown_context[key] = context[key]
        try:
            row = _emit(
                root, boundary,
                {"identity": {"layer": "boundary", "id": f"boundary/{boundary}"}},
                "boundary-evaluation-v1", "observing",
                "evaluation-error", f"boundary evaluation failed: {type(e).__name__}",
                unknown_context)
        except Exception as record_error:  # noqa: BLE001 — the host still cannot be failed by warning IO
            print(f"waystone warn: could not record unknown evaluation at {boundary}: {record_error}",
                  file=sys.stderr)
            return []
        return [row]


def _evaluate_boundary(root: Path, boundary: str, context: dict) -> list[dict]:
    requested_round = context.get("round_id") if boundary == "round-close" else None
    composition = compose_policy(
        root, requested_round if isinstance(requested_round, str) else None)
    policies_by_identity = {
        (policy["identity"]["layer"], policy["identity"]["id"]): policy
        for layer in composition["layers"] for policy in layer["policies"]
    }
    active = [policy for policy in composition["effective"] if policy.get("enabled") is True]
    events: list[dict] = []
    for d in sorted((d for d in active if d.get("rule") not in RULES),
                    key=lambda d: d.get("id", "")):
        rule_id = d.get("rule")
        message = f"active delta references unknown rule {rule_id!r} and could not be evaluated"
        events.append(_emit(root, boundary, d, rule_id,
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
        conflict_context = {
            "policy_identities": conflict["identities"], "resolution": conflict["resolution"]}
        for key in ("delegation_id", "task_id", "round_id"):
            if isinstance(context.get(key), str):
                conflict_context[key] = context[key]
        if isinstance(context.get("task_ids"), list):
            conflict_context["task_ids"] = context["task_ids"]
        events.append(_emit(
            root, boundary, policies_by_identity[
                (conflict["effective_identity"]["layer"], conflict["effective_identity"]["id"])],
            conflict["rule"],
            conflict["effective_stage"], "conflict",
            f"{len(conflict['identities'])} policies conflict at {conflict['scope']} scope — "
            f"effective stage {conflict['effective_stage']} ({conflict['resolution']})",
            conflict_context))

    for rep in sorted(relevant, key=lambda item: (item["rule"], item["id"])):
        rule_id = rep["rule"]
        source_identities = rep.get("source_identities", [rep["identity"]])
        group = [policies_by_identity[(identity["layer"], identity["id"])]
                 for identity in source_identities
                 if (identity["layer"], identity["id"]) in policies_by_identity]
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
                events.append(_emit(root, boundary, rep, rule_id, eff, "fire",
                                    _RULE1_MSG.format(did=did), attribution))
            for did in errors:
                events.append(_emit(root, boundary, rep, rule_id, eff, "evaluation-error",
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
                        root, boundary, rep, rule_id, eff, "fire",
                        f"delegation {row['delegation_id']} changed {len(outside)} file(s) outside "
                        "its structured declared scope",
                        {**row, "outside_scope": outside}))
            for row in errors:
                row.update({"evaluable": False, "fired": False})
                events.append(_emit(
                    root, boundary, rep, rule_id, eff, "evaluation-error",
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
                events.append(_emit(root, boundary, rep, rule_id, eff, "fire", msg,
                                    {"task_ids": [f["task_id"] for f in out["fires"]],
                                     "round_id": context.get("round_id"), "unlinked": out["unlinked"]}))
            if out["evaluation_errors"]:
                events.append(_emit(root, boundary, rep, rule_id, eff, "evaluation-error",
                                    f"{out['evaluation_errors']} review file(s) could not be evaluated",
                                    {"round_id": context.get("round_id")}))
        elif rule_id in ("env-manifest-mutation-v1", "review-skipped-closes-v1",
                          "done-without-evidence-v1"):
            out = _round_rule_at_boundary(root, rule_id, context, params)
            if out is None:
                continue
            round_id = (_round_record_at_boundary(root, context) or {}).get("round_id")
            fired = out["fired"]
            attribution = {"round_id": round_id}
            if isinstance(round_id, str):
                attribution["snapshot"] = round_id
            if rule_id == "env-manifest-mutation-v1":
                attribution["manifest_paths"] = out.get("fires") or []
            elif rule_id == "done-without-evidence-v1":
                attribution["task_ids"] = out.get("fires") or []
            else:
                attribution["consecutive"] = params.get("consecutive", 2)
                attribution["diff_files_threshold"] = params.get("diff_files_threshold", 20)
                attribution["open_blocker_threshold"] = params.get("open_blocker_threshold", 1)
                attribution["risk_reason"] = out.get("current_risk_reason")
            if out.get("evaluable", True) is not True:
                attribution.update({"evaluable": False, "fired": False,
                                    "coverage_reason": out.get("coverage_reason") or "unknown"})
                events.append(_emit(
                    root, boundary, rep, rule_id, eff, "evaluation-error",
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
                               "satisfied apply-verdict or structured main-session verification")
                else:
                    if out.get("current_risk_reason"):
                        message = (f"round {round_id} closes without review feedback and meets "
                                   f"high-risk condition {out['current_risk_reason']}")
                    else:
                        message = (f"round {round_id} reaches {out['current_streak']} consecutive "
                                   "closes without an intervening review feedback ingest")
                events.append(_emit(
                    root, boundary, rep, rule_id, eff, "fire", message, attribution))
            if out.get("evaluation_errors"):
                events.append(_emit(
                    root, boundary, rep, rule_id, eff, "evaluation-error",
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
    raw_delegation = cfg.get("delegation")
    if raw_delegation is not None and not isinstance(raw_delegation, dict):
        return None, False
    delegation = raw_delegation or {}
    value = delegation.get("env_prep")
    if value is not None and (not isinstance(value, list)
                              or any(not isinstance(item, str) for item in value)):
        return None, False
    return value, True


def _main_verification_evidence(root: Path, session_id: str | None) -> dict:
    """L2-B's structured session verification/build signal for a directly executed task."""
    if not isinstance(session_id, str):
        return {"evaluable": False, "positive": False,
                "coverage_reason": "main-verification-unavailable"}
    path = project_state_path(root) / "improve" / "sessions.jsonl"
    if not path.is_file():
        return {"evaluable": False, "positive": False,
                "coverage_reason": "main-verification-unavailable"}
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"evaluable": False, "positive": False,
                "coverage_reason": "main-verification-invalid", "evaluation_errors": 1}
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("session_id") == session_id and row.get("kind") == "main"]
    if len(matches) != 1:
        return {"evaluable": False, "positive": False,
                "coverage_reason": ("main-verification-unavailable" if not matches
                                    else "main-verification-conflict")}

    def passing(field: str) -> bool:
        value = matches[0].get(field)
        return (isinstance(value, dict) and type(value.get("runs")) is int
                and value["runs"] > 0 and value.get("failed", 0) == 0)

    verification = matches[0].get("verification")
    build = matches[0].get("build")
    if not all(isinstance(value, dict) and type(value.get("runs")) is int
               and type(value.get("failed", 0)) is int for value in (verification, build)):
        return {"evaluable": False, "positive": False,
                "coverage_reason": "main-verification-invalid", "evaluation_errors": 1}
    verification_passed = passing("verification")
    build_passed = passing("build")
    return {
        "evaluable": True, "positive": verification_passed or build_passed,
        "coverage_reason": None,
        "evidence_kind": ("main-session-verification" if verification_passed
                          else "main-session-build" if build_passed else "main-session-no-verification"),
        "session_id": session_id,
    }


def _delegation_evidence_index(root: Path) -> tuple[dict[str, list[dict]], int]:
    """Scan the delegation corpus once and index canonical acceptance evidence by task id."""
    import delegate

    index: dict[str, list[dict]] = {}
    unattributed_errors = 0
    directory = delegate._delegations_dir(root)
    if not directory.is_dir():
        return index, 0
    try:
        records = sorted(path for path in directory.iterdir() if path.is_dir())
    except OSError:
        return index, 1
    for record in records:
        try:
            exposure = json.loads((record / "exposure.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unattributed_errors += 1
            continue
        task_id = exposure.get("task_id") if isinstance(exposure, dict) else None
        if not isinstance(task_id, str):
            unattributed_errors += 1
            continue
        evidence = {"delegation_id": record.name, "evaluable": True, "positive": False,
                    "evidence_kind": "no-apply-verdict", "coverage_reason": None}
        try:
            status = json.loads((record / "status.json").read_text(encoding="utf-8"))
            if not isinstance(status, dict) or not isinstance(status.get("state"), str):
                raise WorkflowError("invalid delegation status")
            latest = delegate.latest_canonical_verdict(record)
            if latest is not None:
                _path, verdict = latest
                if verdict["decision"] == "discard":
                    evidence["evidence_kind"] = "discard-verdict"
                else:
                    packet = delegate._load_packet(record)
                    packet_criteria = packet["acceptance"]
                    verdict_criteria = verdict["criteria"]
                    verdict_acceptance = [item.get("criterion") for item in verdict_criteria]
                    exact = (len(verdict_acceptance) == len(packet_criteria)
                             and set(verdict_acceptance) == set(packet_criteria))
                    satisfied = (bool(verdict_criteria) and exact
                                 and all(item.get("met") is True and bool(item.get("evidence"))
                                         for item in verdict_criteria))
                    applied = status["state"] == "applied"
                    evidence["positive"] = satisfied and applied
                    evidence["evidence_kind"] = (
                        "satisfied-apply-verdict" if satisfied and applied
                        else "unresolved-apply-judgment" if not applied
                        else "unmet-apply-verdict")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                WorkflowError, KeyError, TypeError):
            evidence = {"delegation_id": record.name, "evaluable": False, "positive": False,
                        "coverage_reason": "delegation-evidence-invalid", "evaluation_errors": 1}
        index.setdefault(task_id, []).append(evidence)
    return index, unattributed_errors


def _task_done_evidence(task_id: str, delegation_index: dict[str, list[dict]],
                        delegation_errors: int, main_verification: dict) -> dict:
    records = delegation_index.get(task_id, [])
    if records:
        if any(row.get("positive") is True for row in records):
            return {"task_id": task_id, "evaluable": True, "positive": True,
                    "evidence_kind": "satisfied-apply-verdict", "coverage_reason": None,
                    "delegation_ids": sorted(row["delegation_id"] for row in records)}
        if any(row.get("evaluable") is not True for row in records):
            return {"task_id": task_id, "evaluable": False, "positive": False,
                    "coverage_reason": "delegation-evidence-invalid",
                    "evaluation_errors": sum(row.get("evaluation_errors", 0) for row in records)}
        return {"task_id": task_id, "evaluable": True, "positive": False,
                "evidence_kind": records[-1].get("evidence_kind"), "coverage_reason": None,
                "delegation_ids": sorted(row["delegation_id"] for row in records)}
    if delegation_errors:
        return {"task_id": task_id, "evaluable": False, "positive": False,
                "coverage_reason": "delegation-attribution-unknown",
                "evaluation_errors": delegation_errors}
    return {"task_id": task_id, **main_verification}


def _capture_round_evidence(root: Path, base_sha: str | None, head_sha: str | None,
                            task_scopes: dict[str, list[str]], done_task_ids: list[str], *,
                            task_scope_coverage: dict[str, str], session_id: str | None,
                            review_mode: str) -> dict:
    from common import git_rc

    base = base_sha if isinstance(base_sha, str) and base_sha else None
    head = head_sha if isinstance(head_sha, str) and head_sha else None
    delegation_index, delegation_errors = _delegation_evidence_index(root)
    main_verification = _main_verification_evidence(root, session_id)
    task_document = load_tasks(root)
    open_blocker_task_ids = sorted(
        task["id"] for task in (task_document.get("tasks") or [])
        if isinstance(task, dict) and isinstance(task.get("id"), str)
        and task.get("severity") == "blocker"
        and task.get("status") not in ("done", "dropped"))
    payload = {
        "evaluable": False, "coverage_reason": "round-diff-unavailable",
        "changed_files": [], "manifest_paths": [], "env_prep_changed": None,
        "env_prep_before": None, "env_prep_after": None, "env_prep_change_kind": None,
        "review_mode": review_mode,
        "open_blocker_task_ids": open_blocker_task_ids,
        "task_scopes": {task_id: list(scopes) for task_id, scopes in sorted(task_scopes.items())},
        "task_scope_coverage": dict(sorted(task_scope_coverage.items())),
        "done_task_ids": sorted(set(done_task_ids)),
        "done_evidence": [_task_done_evidence(
            task_id, delegation_index, delegation_errors, main_verification)
                          for task_id in sorted(set(done_task_ids))],
    }
    if any(reason == "task-scope-invalid" for reason in task_scope_coverage.values()):
        return {**payload, "coverage_reason": "task-scope-invalid"}
    if base is None or head is None:
        reason = ("task-scope-unknown" if any(
            value == "task-scope-unknown" for value in task_scope_coverage.values())
                  else payload["coverage_reason"])
        return {**payload, "coverage_reason": reason}
    rc, out, _err = git_rc(root, "diff", "--name-only", base, head, "--")
    if rc != 0:
        return payload
    changed = sorted({line.strip() for line in out.splitlines() if line.strip()})
    manifests = [path for path in changed if _is_dependency_manifest(path)]
    before_env, before_ok = _config_env_prep_at(root, base)
    after_env, after_ok = _config_env_prep_at(root, head)
    if not (before_ok and after_ok):
        return {**payload, "changed_files": changed,
                "manifest_paths": manifests,
                "coverage_reason": "env-prep-comparison-unavailable"}
    before_effective = before_env if isinstance(before_env, list) and before_env else None
    after_effective = after_env if isinstance(after_env, list) and after_env else None
    if before_effective == after_effective:
        change_kind = "unchanged"
    elif before_effective is None:
        change_kind = "added"
    elif after_effective is None:
        change_kind = "removed"
    else:
        change_kind = "updated"
    scope_unknown = any(reason == "task-scope-unknown"
                        for reason in task_scope_coverage.values())
    return {
        **payload, "evaluable": not scope_unknown,
        "coverage_reason": "task-scope-unknown" if scope_unknown else None,
        "changed_files": changed,
        "manifest_paths": manifests, "env_prep_before": before_env, "env_prep_after": after_env,
        "env_prep_change_kind": change_kind,
        "env_prep_changed": before_env != after_env,
    }


def _write_round_exposure_file(root: Path, round_id: str, exposure: dict):
    edir = _exposure_dir(root)
    _mkdir_or_refuse(edir)
    base = edir / f"round-{round_id}.json"
    path = base
    number = 2
    content = json.dumps(exposure, ensure_ascii=False, indent=2) + "\n"
    while True:
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return path, exposure
        except FileExistsError:
            path = base.with_name(f"{base.stem}-{number}{base.suffix}")
            number += 1
        except BaseException:
            # open('x') made this path ours; a failed write must not leave a partial immutable record.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise


def reclose_round_exposure(root: Path, previous_path: Path, previous: dict, head_sha: str):
    """Rebind an already closed round to its committed PR closeout without touching repo files."""
    _ensure_project_state_or_refuse(root)
    exposure = json.loads(json.dumps(previous))
    branch_rc, branch, _branch_error = git_rc(root, "branch", "--show-current")
    if branch_rc != 0 or not branch:
        branch = "(detached)"
    exposure["at"] = _now_iso()
    exposure["head_sha"] = head_sha
    exposure["project"]["branch"] = branch
    exposure["reclosed_from"] = str(previous_path)
    return _write_round_exposure_file(root, exposure["round_id"], exposure)


def write_round_exposure(root: Path, round_id: str, head_sha: str | None, watermark: str | None,
                         session_id: str | None = None, *, base_sha: str | None = None,
                         task_scopes: dict[str, list[str]] | None = None,
                         task_scope_coverage: dict[str, str] | None = None,
                         done_task_ids: list[str] | None = None,
                         routes: list[dict] | None = None,
                         reviewers: list[str] | None = None):
    """Immutable per-round exposure record written at close (§9/#4). A re-close of the same round-id
    gets a `-2`/`-3` suffix (H4 precedent — existing records are never overwritten)."""
    _ensure_project_state_or_refuse(root)
    fp, bindings = _profile_summary(root)
    cfg = load_config(root)
    env_prep = (cfg.get("delegation") or {}).get("env_prep")
    review_mode = (cfg.get("review") or {}).get("mode", "packet")

    def file_fingerprint(path: Path) -> str | None:
        return "sha256:" + content_hash(path.read_bytes()) if path.is_file() else None

    config_fingerprint = file_fingerprint(Path(root) / ".waystone.yml")
    committed_policy_fingerprint = file_fingerprint(_project_policy_path(root))
    routing_policy_fingerprint = file_fingerprint(ROUTING_POLICY_PATH)
    try:
        round_evidence = _capture_round_evidence(
            root, base_sha, watermark, task_scopes or {}, done_task_ids or [],
            task_scope_coverage=task_scope_coverage or {}, session_id=session_id,
            review_mode=review_mode)
    except Exception:  # noqa: BLE001 — warning snapshots degrade to explicit unknown
        round_evidence = {
            "evaluable": False, "fired": False, "coverage_reason": "round-snapshot-error",
            "evaluation_errors": 1, "changed_files": [], "manifest_paths": [],
            "env_prep_before": None, "env_prep_after": None, "env_prep_change_kind": None,
            "review_mode": review_mode, "task_scopes": {},
            "open_blocker_task_ids": None,
            "task_scope_coverage": dict(sorted((task_scope_coverage or {}).items())),
            "done_task_ids": sorted(set(done_task_ids or [])), "done_evidence": [],
        }
    policy_composition = compose_policy(root, round_id=round_id)
    branch_rc, branch, _branch_error = git_rc(root, "branch", "--show-current")
    if branch_rc != 0 or not branch:
        branch = "(detached)"
    exposure = {
        "schema": "waystone-round-exposure-1", "round_id": round_id, "at": _now_iso(),
        "session_id": session_id,
        "project": {
            "pslug": _project_slug(root), "root": str(Path(root).resolve()),
            "name": cfg.get("project"), "branch": branch,
        },
        "head_sha": head_sha, "config_watermark": watermark, "base_sha": base_sha,
        "profile_fingerprint": fp, "bindings": bindings,
        "reviewers": list(reviewers or []),
        "start_level": cfg["policy"]["start_level"],
        "routes": list(routes or []),
        "config_fingerprint": config_fingerprint,
        "committed_policy_fingerprint": committed_policy_fingerprint,
        "routing_policy_fingerprint": routing_policy_fingerprint,
        "env_prep": env_prep, "review_mode": review_mode, "round_evidence": round_evidence,
        "overlays_active": [{
            "identity": d["identity"], "status": d["stage"],
            **({"origin_delta_id": d["origin_delta_id"]}
               if isinstance(d.get("origin_delta_id"), str) else {}),
        } for d in policy_composition["effective"]],
        "policy_composition": policy_composition,
        # Adapt & Enforce has not shipped: null means no effective guard engine and [] means no
        # recorded waivers. These are truthful contract values, not missing-data fallbacks.
        "guards": None, "waivers": [],
    }
    return _write_round_exposure_file(root, round_id, exposure)


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
    with hold_project_lock(root):
        migrate_project_state(root)
    return root


def _cli_add(rest: list[str]) -> int:
    pos, opts = _parse_opts(
        rest, value=("rule", "summary", "expected-effect", "risk", "candidate-scope", "from-rec",
                     "title", "root"),
        repeat=("pointers",))
    if not pos:
        raise WorkflowError("add requires a <delta-id>")
    if not opts.get("rule"):
        raise WorkflowError("add requires --rule <rule-id>")
    if opts.get("summary") is None:
        raise WorkflowError("add requires --summary <text>")
    root = _resolve_root(opts.get("root"))
    with hold_project_lock(root):
        delta = add_delta(
            root, pos[0], rule=opts["rule"], summary=opts["summary"],
            pointers=opts.get("pointers"), expected_effect=opts.get("expected-effect", ""),
            risk=opts.get("risk", ""), candidate_scope=opts.get("candidate-scope", "unresolved"),
            from_rec=opts.get("from-rec"), title=opts.get("title", ""))
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
    with hold_project_lock(root):
        delta = promote(root, pos[0])
    print(f"promoted {delta['id']} -> {delta['status']}")
    return 0


def _cli_demote(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("demote requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_project_lock(root):
        delta = demote(root, pos[0])
    print(f"demoted {delta['id']} -> {delta['status']}")
    return 0


def _cli_suspend(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "note"))
    if not pos:
        raise WorkflowError("suspend requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_project_lock(root):
        delta = suspend(root, pos[0], note=opts.get("note"))
    print(f"suspended {delta['id']}")
    return 0


def _cli_retire(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root", "note"))
    if not pos:
        raise WorkflowError("retire requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_project_lock(root):
        delta = retire(root, pos[0], note=opts.get("note"))
    print(f"retired {delta['id']}")
    return 0


def _cli_replay(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",))
    if not pos:
        raise WorkflowError("replay requires a <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_project_lock(root):
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
    with hold_project_lock(root):
        entry = set_round_override(
            root, opts["round"], pos[0], opts["stage"], opts["reason"])
    print(f"round override {entry['rule']} -> {entry['stage']} ({opts['round']})")
    return 0


def _cli_materialize(rest: list[str]) -> int:
    pos, opts = _parse_opts(rest, value=("root",), boolean=("consent-recorded",))
    if len(pos) != 1:
        raise WorkflowError("materialize requires one <delta-id>")
    root = _resolve_root(opts.get("root"))
    with hold_project_lock(root):
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
        identity = e["policy_identity"]
        print(f"[{marker}] {e['rule']} [{identity['layer']}:{identity['id']}]: {e['message']}")
    for e in (e for e in events if e["event"] == "evaluation-error"):
        print(f"[eval-error] {e['rule']}: {e['message']}")
    try:
        import review
        pending = review.pending_reviews(root)
        if pending:
            print(f"waystone warn: {len(pending)} pending review(s)", file=sys.stderr)
            for row in pending:
                print(f"  {review.format_pending_review(row)}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — pending reminders never change check's exit code
        print(f"waystone warn: pending review check unavailable ({e})", file=sys.stderr)
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
