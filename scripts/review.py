#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""SHA-bound PR review cycles and packet-request publication.

A review means "reviewer R examined tree SHA X". That fact is stored as machine-readable
markers in PR comments (GitHub is the canonical event store), never inferred from filenames.
Identity of a review is (reviewer, review_cycle, reviewed_sha). A marker is only believed if
its provenance binds on TWO axes: the logical reviewer it claims AND the GitHub actor who
posted it. A result must come from a trusted operator (`_author` ∈ review.operators ∪ owner),
name a configured reviewer, be the latest cycle, at the current head, with a merge-compatible
verdict and no unresolved decision. Findings/freeze markers are likewise only believed from a
trusted operator; an approval only from a trusted approver whose claimed `by` equals who posted
it. Codex is bound differently: a formal Codex review whose `commit_id` equals the head, or the
SHA the Codex bot names in its own review comment (timing is irrelevant once the tree is pinned).
Markers in fenced code blocks are ignored.

Markers (HTML comments embedded in PR comment bodies):
  waystone-review-cycle  : a freeze — {round_id, cycle, target_sha, base_sha, reviewers,
                                       profile_fingerprint}
  waystone-review-result : an external reviewer reply footer — {reviewer, review_cycle, reviewed_sha, verdict, decision_required}
  waystone-findings      : adjudication outcome for a cycle — {cycle, resolved}
  waystone-approval      : SHA-bound human approval — {sha, by}

Subcommands (also `waystone review <sub>`):
  freeze --pr N [--round ID] [root]   stamp the current PR head as a new review cycle + post request
  status [--pr N] [root]              show per-cycle review status (PR mode) or packet pairs (packet mode)
  pending [root]                       list packet requests still awaiting ingested feedback
  prepare --round ID --narrative PATH [root]
                                       render and bind a request from the round exposure
  ingest [--round ID] [--force]  parse the reply header, byte-exact copy /tmp/review.md →
                                 <id>-feedback.md, then append triage
  triage --round ID --file PATH [root] replace only the marked triage tail
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from common import (
    CONFIG_NAME, WorkflowError, find_project_root, git_full_sha, git_rc, hold_project_lock,
    is_ancestor, load_config, migrate_project_state, normalize_config, parse_iso_timestamp,
    project_state_path, upstream_ref, write_bytes_atomic,
)  # noqa: E402

CODEX_BOT = "chatgpt-codex-connector[bot]"  # REST `user.login` form
INBOX = Path("/tmp/review.md")  # fixed drop-file: user saves the reviewer reply here, byte-exact
ROUND_REQUEST_BINDING_SCHEMA = "waystone-round-request-binding-1"
PR_FREEZE_BINDING_SCHEMA = "waystone-pr-freeze-binding-1"
PACKET_REVIEWING_RE = re.compile(
    r"- Reviewing: ([0-9a-f]{40})   \(diff against ([0-9a-f]{40}|\(root\))\)")
PACKET_REVIEWING_FORMAT = (
    "- Reviewing: <40-lowercase-hex-sha>   "
    "(diff against <40-lowercase-hex-sha-or-(root)>)"
)
REVIEW_REPLY_HEADER_MAX_LINES = 32
REVIEW_REPLY_HEADER_MAX_BYTES = 16 * 1024
FEEDBACK_HEADER_MAX_BYTES = 32 * 1024
FEEDBACK_HEADER_SEPARATOR = b"\n\n---\n\n"
REVIEW_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra")
_REPLY_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
_REPLY_MODEL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._/-]*)?")
_REVIEW_TARGET_RE = re.compile(
    r"(?P<first>[0-9a-fA-F]{12,40})(?:-(?P<second>[0-9a-fA-F]{12,40}))?")
_FEEDBACK_METADATA_PREFIX = "reply-metadata-json: "
TRIAGE_BEGIN = b"<!-- waystone triage: BEGIN -->"
TRIAGE_END = b"<!-- waystone triage: END -->"
REVIEW_REQUEST_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "review-request.md"
NARRATIVE_HEADINGS = (
    "## What changed and why",
    "## Read these first",
    "## Claims to attack",
    "## Evidence already produced (mine — inspect, don't trust)",
    "## Known weak spots",
    "## Domain lens",
)
# Matched per line AFTER wrapper normalization (leading whitespace / blockquote / list markers
# stripped) so indentation or quoting cannot smuggle a protocol lookalike past the renderer.
_NARRATIVE_REFERENCE_RE = re.compile(
    r"^(?:[-*+]\s*)?(?:Reviewing|Reviewer|Project|Branch)\s*:")
_NARRATIVE_REPLY_KEY_RE = re.compile(
    r"(?i)^(?:[-*+]\s*)?(?:model|effort|review-target)\s*:")
_NARRATIVE_WRAPPER_RE = re.compile(r"^(?:\s+|>\s*|[-*+]\s+|\d{1,9}[.)]\s+|\[[ xX]\]\s+)")
_TEMPLATE_TOKEN_RE = re.compile(r"\[\[[A-Z_]+\]\]")


def is_codex(login: str | None) -> bool:
    """Codex bot author match, robust to the `[bot]` suffix: GraphQL (`gh pr view`) drops it
    (`chatgpt-codex-connector`), REST keeps it (`chatgpt-codex-connector[bot]`)."""
    return (login or "").removesuffix("[bot]") == "chatgpt-codex-connector"


MARKER_RE = re.compile(r"<!--\s*(?:waystone|jw)-([a-z-]+):v1\s*\n(.*?)\n\s*-->", re.DOTALL)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
MERGE_OK_VERDICTS = {"shipped", "shipped-with-risk", "approved", "approve", "lgtm"}
# output-contract finding blocks: `### JW-GPT-NNN — <title>` then a `- Severity: <x>` line.
FINDING_RE = re.compile(r"(?m)^#{2,4}\s+(JW-GPT-\d+)\s*[-—:]\s*(.+?)\s*$")
SEVERITY_RE = re.compile(r"(?im)^\s*[-*]?\s*Severity\s*:\s*`?(blocker|major|minor)`?")


# ---- pure marker logic -------------------------------------------------------
def emit_marker(kind: str, fields: dict) -> str:
    """Encode a marker as real YAML (lists stay lists, ints stay ints) — never hand-joined text,
    so the round-trip is a typed protocol, not a string blob."""
    body = yaml.safe_dump(dict(fields), sort_keys=False, default_flow_style=False,
                          allow_unicode=True).strip()
    return f"<!-- waystone-{kind}:v1\n{body}\n-->"


def _reply_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def normalize_reviewer_model(value: object) -> str | None:
    """Canonical reply identity: one ASCII model slug, optionally provider-qualified.

    Case is insignificant. A provider-qualified configured route may match a bare declared model
    slug, but two provider-qualified values must match in full; no other aliasing is performed.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if _REPLY_MODEL_RE.fullmatch(candidate) is None:
        return None
    return candidate.lower()


def normalize_review_effort(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if candidate in REVIEW_EFFORT_VALUES else None


def normalize_review_target(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _REVIEW_TARGET_RE.fullmatch(candidate) is not None else None


def reviewer_model_matches(declared: str, configured: str) -> bool:
    """Documented identity normalization used by ingest and the configured-feedback guard."""
    left = normalize_reviewer_model(declared)
    right = normalize_reviewer_model(configured)
    if left is None or right is None:
        return False
    if left == right:
        return True
    left_provider, left_sep, left_model = left.partition(":")
    right_provider, right_sep, right_model = right.partition(":")
    if bool(left_sep) == bool(right_sep):
        return False
    return (left_model if left_sep else left_provider) == (
        right_model if right_sep else right_provider)


def parse_review_reply_header(body: bytes) -> dict:
    """Parse only the reply's leading key/value block.

    Leading blank lines and one optional Markdown fence are ignored; key case, order, and colon
    whitespace are insignificant. The block ends at its first blank/non-key line (or closing
    fence), is bounded to 32 lines/16 KiB, and is classified only if it contains ``model`` or
    ``review-target``. Unknown keys are retained. The whole header block must be valid UTF-8;
    duplicate fields are never guessed.
    """
    not_detected = {
        "detected": False, "metadata": {}, "model": None, "effort": None,
        "review_target": None, "warnings": [],
    }
    raw_header: list[bytes] = []
    metadata: dict[str, str] = {}
    duplicates: set[str] = set()
    warnings: list[str] = []
    seen: set[str] = set()
    started = False
    fenced = False
    consumed = 0

    # Slice before splitting so the scan is bounded by construction — body bytes past the cap
    # never participate (a line truncated by the slice always trips the consumed check below).
    for line_number, raw in enumerate(
            body[:REVIEW_REPLY_HEADER_MAX_BYTES + 1].splitlines(), 1):
        if line_number > REVIEW_REPLY_HEADER_MAX_LINES:
            warnings.append("header-limit-exceeded")
            break
        consumed += len(raw) + 1
        if consumed > REVIEW_REPLY_HEADER_MAX_BYTES:
            warnings.append("header-limit-exceeded")
            break
        stripped = raw.strip()
        if not started and not stripped:
            raw_header.append(raw)
            continue
        if not started and re.fullmatch(rb"```[^`]*", stripped):
            started = True
            fenced = True
            raw_header.append(raw)
            continue
        if fenced and stripped == b"```":
            break
        if started and not stripped:
            break
        raw_key, separator, _raw_value = raw.partition(b":")
        if (not separator
                or re.fullmatch(rb"[A-Za-z][A-Za-z0-9_-]*", raw_key.strip()) is None):
            break
        raw_header.append(raw)
        started = True

    try:
        header = b"\n".join(raw_header).decode("utf-8")
    except UnicodeDecodeError:
        return not_detected

    for line in header.splitlines():
        match = _REPLY_KEY_RE.fullmatch(line)
        if match is None:
            continue
        key = _reply_key(match.group(1))
        value = match.group(2).strip()
        if key in seen:
            metadata.pop(key, None)
            duplicates.add(key)
            warnings.append(f"duplicate-{key}")
            continue
        seen.add(key)
        metadata[key] = value

    detected = "model" in seen or "review-target" in seen
    if not detected:
        return not_detected

    model = None if "model" in duplicates else normalize_reviewer_model(metadata.get("model"))
    effort = None if "effort" in duplicates else normalize_review_effort(metadata.get("effort"))
    target = (None if "review-target" in duplicates
              else normalize_review_target(metadata.get("review-target")))
    for key, value, invalid in (
            ("model", model, "invalid-model"),
            ("effort", effort, "invalid-effort"),
            ("review-target", target, "invalid-review-target")):
        if key not in seen:
            warnings.append(f"missing-{key}")
        elif value is None and key not in duplicates:
            warnings.append(invalid)
        if value is None:
            metadata.pop(key, None)
    return {
        "detected": True, "metadata": metadata, "model": model, "effort": effort,
        "review_target": target, "warnings": list(dict.fromkeys(warnings)),
    }


def review_target_matches_binding(declared: str | None, binding: dict | None) -> bool | None:
    target = normalize_review_target(declared)
    if target is None or binding is None:
        return None
    match = _REVIEW_TARGET_RE.fullmatch(target)
    if match is None:  # normalize_review_target is the single semantic gate
        return None
    first, second = match.group("first"), match.group("second")
    bound_target = str(binding.get("target_sha") or "").lower()
    bound_base = str(binding.get("base_sha") or "").lower()
    if second is None:
        return bool(bound_target) and bound_target.startswith(first)
    return (bool(bound_base) and bool(bound_target)
            and bound_base.startswith(first) and bound_target.startswith(second))


def assess_review_reply(parsed: dict, binding: dict | None) -> dict:
    target_matches = review_target_matches_binding(parsed.get("review_target"), binding)
    reviewers = binding.get("reviewers") if isinstance(binding, dict) else None
    model = parsed.get("model")
    model_configured = (isinstance(model, str) and isinstance(reviewers, list)
                        and any(reviewer_model_matches(model, item) for item in reviewers))
    if binding is None:
        reason = "round-binding-unavailable"
    elif model is None:
        reason = "reviewer-identity-unavailable"
    elif parsed.get("review_target") is None:
        reason = "review-target-unavailable"
    elif target_matches is not True:
        reason = "review-target-mismatch"
    elif not model_configured:
        reason = "reviewer-not-configured"
    else:
        reason = None
    return {
        "review_target_matches": target_matches,
        "reviewer_configured": True if reason is None else None,
        "reviewer_coverage_reason": reason,
    }


def write_round_request_binding(root: Path, round_id: str, target_sha: str, base_sha: str | None,
                                reviewers: list[str], *, mode: str,
                                directory: Path | None = None) -> Path:
    """Append an immutable round-bound sidecar for packet request projection.

    A PR request row records the pre-freeze relationship but is never promoted to a reviewed-SHA
    fact; successful PR freeze has its own cycle-bound local evidence.
    """
    if not _is_sha(target_sha) or (base_sha is not None and not _is_sha(base_sha)):
        raise WorkflowError("round request binding requires full target/base commit SHAs")
    if mode not in ("packet", "pr") or not _is_strlist(reviewers):
        raise WorkflowError("round request binding requires packet|pr mode and literal reviewers")
    if directory is None:
        cfg = load_config(root)
        directory = Path(root) / cfg["reviews_dir"]
    else:
        directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": ROUND_REQUEST_BINDING_SCHEMA, "round_id": round_id,
        "target_sha": target_sha, "base_sha": base_sha, "reviewers": reviewers,
        "mode": mode, "canonical_store": "github-pr-comment" if mode == "pr" else "local-packet",
        "at": datetime.now().astimezone().isoformat(),
    }
    contract = {key: value for key, value in row.items() if key != "at"}
    base = directory / f"{round_id}-request.binding.json"
    prior: list[tuple[Path, dict]] = []
    for existing in sorted(directory.glob(f"{round_id}-request.binding*.json")):
        previous = read_round_request_binding(existing, expected_round_id=round_id)
        prior.append((existing, previous))
    if prior:
        existing, previous = max(
            prior, key=lambda item: round_request_binding_order(item[0], item[1]))
        if {key: value for key, value in previous.items() if key != "at"} == contract:
            return existing
    path = base
    number = 2
    content = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    while True:
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return path
        except FileExistsError:
            path = base.with_name(f"{base.stem}-{number}{base.suffix}")
            number += 1


def read_round_request_binding(path: Path, *, expected_round_id: str | None = None) -> dict:
    """Load one request sidecar without silently accepting damaged projection evidence."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt review binding {path}: {type(e).__name__}") from e
    if (not isinstance(data, dict) or data.get("schema") != ROUND_REQUEST_BINDING_SCHEMA
            or not isinstance(data.get("round_id"), str) or not data["round_id"]
            or not _is_sha(data.get("target_sha"))
            or (data.get("base_sha") is not None and not _is_sha(data.get("base_sha")))
            or not _is_strlist(data.get("reviewers"))
            or data.get("mode") not in ("packet", "pr")
            or data.get("canonical_store") != (
                "local-packet" if data.get("mode") == "packet" else "github-pr-comment")
            or parse_iso_timestamp(data.get("at")) is None):
        raise WorkflowError(f"corrupt review binding {path}: invalid schema or fields")
    if expected_round_id is not None and data["round_id"] != expected_round_id:
        raise WorkflowError(
            f"corrupt review binding {path}: round_id {data['round_id']!r} does not match "
            f"{expected_round_id!r}")
    return data


def round_request_binding_order(path: Path, row: dict) -> tuple[int, float, str]:
    """Canonical newest-sidecar order shared by publication and improve projection: the immutable
    reissue sequence wins, and parsed offset-aware timestamps only break ties within a sequence —
    raw local-offset strings must never decide recency (they invert across timezone changes)."""
    match = re.search(r"-request\.binding(?:-(\d+))?\.json$", Path(path).name)
    sequence = int(match.group(1)) if match and match.group(1) else 1
    at = parse_iso_timestamp(str(row.get("at") or ""))
    return sequence, (at.timestamp() if at is not None else float("-inf")), str(path)


def ingest_round_binding(root: Path, round_id: str, cfg: dict) -> tuple[dict | None, str | None]:
    """Return only publication-time sidecar evidence; never rebuild it from current config."""
    directory = Path(root) / cfg["reviews_dir"]
    mode = (cfg.get("review") or {}).get("mode", "packet")
    try:
        if mode == "packet":
            paths = sorted(directory.glob(f"{round_id}-request.binding*.json"))
            if not paths:
                return None, "missing-round-request-sidecar"
            rows = [(path, read_round_request_binding(path, expected_round_id=round_id))
                    for path in paths]
            path, row = max(rows, key=lambda item: round_request_binding_order(*item))
            return {**row, "source": str(path)}, None

        paths = sorted(directory.glob(f"{round_id}-freeze-*.binding*.json"))
        if not paths:
            return None, "missing-pr-freeze-sidecar"
        rows = [(path, read_pr_freeze_binding(path, expected_round_id=round_id))
                for path in paths]
        latest_cycle = max(row["cycle"] for _path, row in rows)
        latest = [(path, row) for path, row in rows if row["cycle"] == latest_cycle]
        contracts = {(row["target_sha"], row["base_sha"], tuple(row["reviewers"]))
                     for _path, row in latest}
        if len(contracts) != 1:
            return None, "conflicting-pr-freeze-sidecars"
        path, row = max(latest, key=lambda item: (item[1]["at"], str(item[0])))
        return {**row, "source": str(path)}, None
    except (OSError, WorkflowError) as e:
        return None, f"corrupt-round-binding:{type(e).__name__}"


def _unknown_feedback_metadata(reason: str) -> dict:
    return {
        "metadata": {}, "model": None, "effort": None, "review_target": None,
        "review_target_matches": None, "reviewer_configured": None,
        "reviewer_coverage_reason": reason,
    }


def read_feedback_reply_metadata(path: Path, *, expected_round_id: str | None = None,
                                 binding: dict | None = None) -> dict:
    """Project reply identity from the bounded feedback header and the supplied round binding.

    Stored declaration fields are parser output and are projected as-is. Stored assessment
    booleans are ignored; every projection compares the declarations with current binding evidence.
    """
    unknown = _unknown_feedback_metadata("reply-metadata-unavailable")
    try:
        with Path(path).open("rb") as stream:
            prefix = stream.read(FEEDBACK_HEADER_MAX_BYTES + len(FEEDBACK_HEADER_SEPARATOR))
    except OSError:
        return _unknown_feedback_metadata("feedback-file-unavailable")
    separator = prefix.find(FEEDBACK_HEADER_SEPARATOR)
    if separator < 0 or separator >= FEEDBACK_HEADER_MAX_BYTES:
        return unknown
    try:
        header = prefix[:separator].decode("utf-8")
    except UnicodeDecodeError:
        return unknown
    lines = header.splitlines()
    candidates = [line[len(_FEEDBACK_METADATA_PREFIX):]
                  for line in lines if line.startswith(_FEEDBACK_METADATA_PREFIX)]
    rounds = [line[len("round: "):] for line in lines if line.startswith("round: ")]
    if len(candidates) != 1 or len(rounds) != 1 or not rounds[0]:
        return unknown
    try:
        payload = json.loads(candidates[0])
    except json.JSONDecodeError:
        return unknown
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if (not isinstance(metadata, dict)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in metadata.items())):
        return unknown
    projected = {
        "metadata": metadata,
        "model": metadata.get("model"),
        "effort": metadata.get("effort"),
        "review_target": metadata.get("review-target"),
    }
    if expected_round_id is not None and rounds[0] != expected_round_id:
        return {
            **projected, "review_target_matches": None, "reviewer_configured": None,
            "reviewer_coverage_reason": "feedback-round-mismatch",
        }
    assessment = assess_review_reply(projected, binding)
    return {
        **projected,
        "review_target_matches": assessment["review_target_matches"],
        "reviewer_configured": assessment["reviewer_configured"],
        "reviewer_coverage_reason": assessment["reviewer_coverage_reason"],
    }


def pending_reviews(root: Path, *, now: datetime | None = None) -> list[dict]:
    """Derive packet requests whose latest binding has no matching ingested feedback."""
    cfg = load_config(root)
    directory = Path(root) / cfg["reviews_dir"]
    if not directory.is_dir():
        return []
    local_now = now or datetime.now().astimezone()
    local_timezone = local_now.tzinfo if now is not None else None
    if local_now.tzinfo is None or local_now.utcoffset() is None:
        raise WorkflowError("pending review age requires a timezone-aware local clock")

    pending: list[dict] = []
    for request in sorted(directory.glob("*-request.md")):
        round_id = request.name.removesuffix("-request.md")
        binding_paths = sorted(directory.glob(f"{round_id}-request.binding*.json"))
        binding = None
        binding_path = None
        if binding_paths:
            # One damaged sidecar must not abort every other round's derivation — the damaged
            # round itself stays listed as pending with honest-unknown fields (binding None).
            rows = []
            for path in binding_paths:
                try:
                    rows.append((path, read_round_request_binding(path, expected_round_id=round_id)))
                except WorkflowError:
                    continue
            if rows:
                binding_path, binding = max(
                    rows, key=lambda item: round_request_binding_order(item[0], item[1]))
                if binding["mode"] != "packet":
                    # PR-mode rounds are completion-tracked by the PR machinery, not this
                    # packet-pending surface.
                    continue

        feedback = directory / f"{round_id}-feedback.md"
        metadata = read_feedback_reply_metadata(
            feedback, expected_round_id=round_id, binding=binding)
        # Complete = an ingested reply whose declared review-target matches the LATEST binding.
        # Reviewer-identity configuration is coverage reporting, not receipt — a reply from an
        # unconfigured model still ends the wait.
        if binding is not None and metadata["review_target_matches"] is True:
            continue

        requested_date = None
        if binding is not None:
            requested_at = parse_iso_timestamp(binding["at"])
            if requested_at is None:  # read_round_request_binding already enforces this invariant
                raise WorkflowError(f"review binding {binding_path} has no valid timestamp")
            requested_date = requested_at.astimezone(local_timezone).date()
        else:
            try:
                requested_date = datetime.strptime(round_id[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
        pending.append({
            "round_id": round_id,
            "age_days": ((local_now.date() - requested_date).days
                         if requested_date is not None else None),
            "target_sha": binding["target_sha"] if binding is not None else None,
            "reviewers": list(binding["reviewers"]) if binding is not None else [],
        })
    return pending


def format_pending_review(row: dict) -> str:
    age = f"{row['age_days']}d" if row.get("age_days") is not None else "unknown"
    target = row.get("target_sha") or "(binding unavailable)"
    reviewers = ", ".join(row.get("reviewers") or []) or "(binding unavailable)"
    return (f"round {row['round_id']} | age {age} | target {target} | "
            f"reviewers {reviewers}")


def read_pr_freeze_binding(path: Path, *, expected_round_id: str | None = None,
                           expected_cycle: int | None = None) -> dict:
    """Load a PR freeze sidecar strictly so a damaged predecessor cannot be hidden by a suffix."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"corrupt review binding {path}: {type(e).__name__}") from e
    if (not isinstance(data, dict) or data.get("schema") != PR_FREEZE_BINDING_SCHEMA
            or not isinstance(data.get("round_id"), str) or not data["round_id"]
            or type(data.get("pr")) is not int or data["pr"] < 1
            or not _is_cycle(data.get("cycle"))
            or not _is_sha(data.get("target_sha")) or not _is_sha(data.get("base_sha"))
            or not _is_strlist(data.get("reviewers"))
            or (data.get("profile_fingerprint") is not None
                and not _nonempty_str(data.get("profile_fingerprint")))
            or data.get("mode") != "pr"
            or data.get("canonical_store") != "local-freeze-evidence"
            or parse_iso_timestamp(data.get("at")) is None):
        raise WorkflowError(f"corrupt review binding {path}: invalid schema or fields")
    if expected_round_id is not None and data["round_id"] != expected_round_id:
        raise WorkflowError(
            f"corrupt review binding {path}: round_id {data['round_id']!r} does not match "
            f"{expected_round_id!r}")
    if expected_cycle is not None and data["cycle"] != expected_cycle:
        raise WorkflowError(
            f"corrupt review binding {path}: cycle {data['cycle']!r} does not match "
            f"{expected_cycle!r}")
    return data


def write_pr_freeze_binding(root: Path, round_id: str, pr: int, cycle: int,
                            target_sha: str, base_sha: str, reviewers: list[str],
                            profile_fingerprint: str | None, reviews_dir: str) -> Path:
    """Record the successful PR freeze as local, immutable, round-bound improve evidence."""
    if (not isinstance(round_id, str) or not round_id.strip() or type(pr) is not int or pr < 1
            or not _is_cycle(cycle) or not _is_sha(target_sha) or not _is_sha(base_sha)
            or not _is_strlist(reviewers)
            or (profile_fingerprint is not None and not _nonempty_str(profile_fingerprint))
            or not _nonempty_str(reviews_dir)):
        raise WorkflowError("PR freeze binding requires round, PR, cycle, SHAs, and reviewers")
    directory = Path(root) / reviews_dir
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": PR_FREEZE_BINDING_SCHEMA, "round_id": round_id, "pr": pr,
        "cycle": cycle, "target_sha": target_sha, "base_sha": base_sha,
        "reviewers": reviewers, "profile_fingerprint": profile_fingerprint,
        "mode": "pr", "canonical_store": "local-freeze-evidence",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    contract_fields = {
        key: value for key, value in row.items() if key != "at"
    }
    for existing in sorted(directory.glob(f"{round_id}-freeze-{cycle}.binding*.json")):
        previous = read_pr_freeze_binding(
            existing, expected_round_id=round_id, expected_cycle=cycle)
        if ({key: value for key, value in previous.items() if key != "at"}
                == contract_fields):
            return existing
    base = directory / f"{round_id}-freeze-{cycle}.binding.json"
    path = base
    number = 2
    content = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    while True:
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return path
        except FileExistsError:
            path = base.with_name(f"{base.stem}-{number}{base.suffix}")
            number += 1


def parse_packet_request_binding(path: Path) -> tuple[str, str | None] | None:
    """Read the packet template's one byte-shape-exact structured Reviewing line."""
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"review: warning: cannot read packet request {path}: {e}", file=sys.stderr)
        return None
    candidates = re.findall(r"(?m)^- Reviewing:.*$", text)
    match = PACKET_REVIEWING_RE.fullmatch(candidates[0]) if len(candidates) == 1 else None
    if match is None:
        print(
            f"review: warning: packet request {path} must contain exactly one line in this "
            f"exact format: {PACKET_REVIEWING_FORMAT}", file=sys.stderr)
        return None
    target_sha, raw_base = match.groups()
    return target_sha, None if raw_base == "(root)" else raw_base


def stored_narrative_path(root: Path, round_id: str) -> Path:
    """Host-local copy of the validated narrative prepare rendered with — freeze re-renders from
    it instead of re-trusting the on-disk request file."""
    return project_state_path(root) / "review-requests" / f"{round_id}-narrative.md"


def prepared_request_path(root: Path, round_id: str, *, mode: str | None = None) -> Path:
    """Mode-specific request carrier: tracked packet file or host-local PR comment source."""
    cfg = load_config(root)
    selected = mode or (cfg.get("review") or {}).get("mode", "packet")
    if selected == "packet":
        return Path(root) / cfg["reviews_dir"] / f"{round_id}-request.md"
    if selected == "pr":
        return project_state_path(root) / "review-requests" / f"{round_id}-request.md"
    raise WorkflowError(f"review prepare: unsupported review.mode {selected!r}")


def _round_exposure_order(path: Path, row: dict) -> tuple[str, int, str]:
    base = f"round-{row['round_id']}"
    match = re.fullmatch(rf"{re.escape(base)}-(\d+)\.json", path.name)
    return str(row["at"]), int(match.group(1)) if match else 1, str(path)


def read_round_closeout_exposure(root: Path, round_id: str) -> tuple[Path, dict]:
    """Load the newest immutable exposure for exactly ``round_id`` as the render binding."""
    import overlay

    directory = overlay._exposure_dir(root)
    filename_re = re.compile(rf"^round-{re.escape(round_id)}(?:-(\d+))?\.json$")
    paths = [path for path in sorted(directory.glob("round-*.json"))
             if filename_re.fullmatch(path.name)] if directory.is_dir() else []
    if not paths:
        raise WorkflowError(
            f"round exposure is missing for {round_id}; reclose the round before preparing review")
    rows: list[tuple[Path, dict]] = []
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise WorkflowError(f"corrupt round exposure {path}: {type(e).__name__}") from e
        project = row.get("project") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or row.get("schema") != "waystone-round-exposure-1"
                or row.get("round_id") != round_id or parse_iso_timestamp(row.get("at")) is None
                or not _is_sha(row.get("head_sha"))
                or (row.get("base_sha") is not None and not _is_sha(row.get("base_sha")))
                or row.get("review_mode") not in ("packet", "pr")
                or not _is_strlist(row.get("reviewers")) or not row["reviewers"]
                or not isinstance(project, dict) or not _nonempty_str(project.get("name"))
                or not _nonempty_str(project.get("branch"))):
            raise WorkflowError(
                f"round exposure {path} lacks a deterministic review binding; "
                "reclose the round with the current Waystone version")
        rows.append((path, row))
    return max(rows, key=lambda item: _round_exposure_order(*item))


def _narrative_lookalike_line(text: str) -> str | None:
    """Return the first narrative line that impersonates a protocol surface, else None.

    Each line is normalized by repeatedly stripping leading whitespace, blockquote (>) and
    list markers before matching, so wrappers cannot hide a lookalike; fenced code blocks
    get no exemption — the renderer owns every protocol surface."""
    for raw in text.splitlines():
        line = raw
        while True:
            stripped = _NARRATIVE_WRAPPER_RE.sub("", line, count=1)
            if stripped == line:
                break
            line = stripped
        if _NARRATIVE_REFERENCE_RE.match(line) or _NARRATIVE_REPLY_KEY_RE.match(line):
            return raw
    return None


def _read_review_narrative(path: Path) -> str:
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WorkflowError(f"cannot read UTF-8 review narrative {path}: {e}") from e
    if "\r" in text:
        text = text.replace("\r\n", "\n")
        if "\r" in text:
            raise WorkflowError("review narrative contains unsupported bare carriage returns")
    headings = re.findall(r"(?m)^## .+$", text)
    if tuple(headings) != NARRATIVE_HEADINGS:
        raise WorkflowError(
            "review narrative must contain exactly the six canonical narrative sections in order")
    for index, heading in enumerate(NARRATIVE_HEADINGS):
        start = text.index(heading) + len(heading)
        end = text.index(NARRATIVE_HEADINGS[index + 1]) if index + 1 < len(headings) else len(text)
        if not text[start:end].strip():
            raise WorkflowError(f"review narrative section is empty: {heading}")
    lookalike = _narrative_lookalike_line(text)
    if lookalike is not None:
        raise WorkflowError(
            f"review narrative contains a protocol-field lookalike ({lookalike.strip()!r}); "
            "reference and reply-header fields are rendered only by the template")
    if _TEMPLATE_TOKEN_RE.search(text):
        raise WorkflowError("review narrative contains a reserved template token")
    return text.strip() + "\n"


def _render_review_request(round_id: str, exposure: dict, narrative: str) -> str:
    try:
        template = REVIEW_REQUEST_TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WorkflowError(f"review request template unavailable: {e}") from e
    project = exposure["project"]
    reviewers = exposure["reviewers"]
    target_sha = exposure["head_sha"]
    replacements = {
        "[[ROUND_ID]]": round_id,
        "[[PROJECT]]": project["name"],
        "[[BRANCH]]": project["branch"],
        "[[REVIEWERS]]": ", ".join(reviewers),
        "[[REVIEWING_SHA]]": target_sha,
        "[[DIFF_BASE]]": exposure.get("base_sha") or "(root)",
        "[[NARRATIVE]]": narrative.rstrip(),
        "[[REPLY_MODEL]]": reviewers[0],
        "[[REVIEW_TARGET]]": target_sha,
    }
    found = set(_TEMPLATE_TOKEN_RE.findall(template))
    if found != set(replacements) or any(template.count(token) != 1 for token in replacements):
        raise WorkflowError("review request template token contract is incomplete or ambiguous")
    rendered = template
    for token, value in replacements.items():
        if "\n" in value and token != "[[NARRATIVE]]":
            raise WorkflowError(f"review request binding contains a newline for {token}")
        rendered = rendered.replace(token, value)
    if _TEMPLATE_TOKEN_RE.search(rendered):
        raise WorkflowError("review request rendering left an unresolved template token")
    canonical_lines = (
        "- Project:", "- Branch:", "- Reviewer:", "- Reviewing:",
    )
    for prefix in canonical_lines:
        if len(re.findall(rf"(?m)^{re.escape(prefix)}.*$", rendered)) != 1:
            raise WorkflowError(f"rendered review request must contain one canonical {prefix} line")
    for key in ("model", "effort", "review-target"):
        if len(re.findall(rf"(?mi)^{re.escape(key)}\s*:", rendered)) != 1:
            raise WorkflowError(f"rendered review request must contain one canonical {key} key")
    candidates = re.findall(r"(?m)^- Reviewing:.*$", rendered)
    match = PACKET_REVIEWING_RE.fullmatch(candidates[0]) if len(candidates) == 1 else None
    if match is None or match.group(1) != target_sha:
        raise WorkflowError("rendered Reviewing field does not match the round exposure target")
    return rendered if rendered.endswith("\n") else rendered + "\n"


def prepare_review_request(root: Path, round_id: str, narrative_path: Path) -> int:
    """Render exact fields from the round exposure and merge only validated narrative input."""
    try:
        cfg = load_config(root)
        mode = (cfg.get("review") or {}).get("mode", "packet")
        _exposure_path, exposure = read_round_closeout_exposure(root, round_id)
        if exposure["review_mode"] != mode:
            raise WorkflowError(
                f"round exposure review.mode {exposure['review_mode']!r} differs from current "
                f"mode {mode!r}; reclose the round")
        head = git_full_sha(root, "HEAD")
        if head != exposure["head_sha"]:
            raise WorkflowError(
                f"current HEAD {head or '?'} differs from round exposure closeout head "
                f"{exposure['head_sha']}; reclose the round before preparing review")
        request = prepared_request_path(root, round_id, mode=mode)
        expected = {
            "target_sha": exposure["head_sha"], "base_sha": exposure.get("base_sha"),
            "reviewers": exposure["reviewers"], "mode": mode,
        }
        existing_paths = sorted(request.parent.glob(f"{round_id}-request.binding*.json"))
        for path in existing_paths:
            row = read_round_request_binding(path, expected_round_id=round_id)
            if any(row.get(key) != value for key, value in expected.items()):
                raise WorkflowError(
                    f"existing request sidecar {path} disagrees with the round exposure; "
                    "reclose and prepare with a new round id instead of superseding immutable evidence")
        narrative = _read_review_narrative(narrative_path)
        rendered = _render_review_request(round_id, exposure, narrative)
        request.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(request, rendered.encode("utf-8"))
        narrative_store = stored_narrative_path(root, round_id)
        narrative_store.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(narrative_store, narrative.encode("utf-8"))
        binding_path = write_round_request_binding(
            root, round_id, expected["target_sha"], expected["base_sha"],
            expected["reviewers"], mode=mode, directory=request.parent)
    except (OSError, WorkflowError, ValueError) as e:
        print(f"review prepare: {e}", file=sys.stderr)
        return 1
    print(f"prepared {mode} review request: {request}")
    print(f"prepared review request binding: {binding_path}")
    return 0


def prepare_packet_request(root: Path, round_id: str, narrative_path: Path | None = None) -> int:
    """Compatibility name for callers of the former packet-only prepare function."""
    if narrative_path is None:
        print("review prepare: --narrative PATH is required", file=sys.stderr)
        return 1
    return prepare_review_request(root, round_id, narrative_path)


def _remote_blob(root: Path, spec: str) -> bytes | None:
    """Exact bytes of `<sha>:<path>` from the object store, or None when absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", spec],
            capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def verify_packet_publication(root: Path, round_id: str) -> int:
    """Direct-binding publication gate. The only proposition proven: this round's packet
    (request + LATEST binding sidecar) is byte-identical in the remote-tracking tree, and the
    closeout SHA the binding names is contained in that same remote. Reviewed-commit identity
    comes from the binding literal; publication comes from ONE pinned remote SHA resolved at the
    start. No ancestry topology — merge parents, first-parent chains, HEAD — is inspected
    (direct-binding ruling 2026-07-17; the ancestry class was the audited pot crack)."""
    cfg = load_config(root)
    if (cfg.get("review") or {}).get("mode", "packet") != "packet":
        print("remote: --round publication verification requires review.mode packet", file=sys.stderr)
        return 1
    rdir = root / cfg["reviews_dir"]
    if rdir.is_symlink() or not rdir.is_dir():
        print(f"remote: reviews directory must be a real directory: {rdir}", file=sys.stderr)
        return 1
    request = rdir / f"{round_id}-request.md"
    if request.is_symlink() or not request.is_file():
        print(f"remote: packet request must be a regular file: {request}", file=sys.stderr)
        return 1
    request_binding = parse_packet_request_binding(request)
    if request_binding is None:
        return 1
    sidecar_paths = sorted(rdir.glob(f"{round_id}-request.binding*.json"))
    if not sidecar_paths:
        print(f"remote: packet request binding is missing for round {round_id}", file=sys.stderr)
        return 1
    if any(path.is_symlink() or not path.is_file() for path in sidecar_paths):
        print(f"remote: packet binding sidecars must be regular files for round {round_id}",
              file=sys.stderr)
        return 1
    rows: list[tuple[Path, dict]] = []
    try:
        for path in sidecar_paths:
            row = read_round_request_binding(path, expected_round_id=round_id)
            if row["mode"] != "packet":
                raise WorkflowError(f"corrupt review binding {path}: expected packet mode")
            rows.append((path, row))
    except WorkflowError as e:
        print(f"remote: {e}", file=sys.stderr)
        return 1
    binding_path, binding = max(
        rows, key=lambda item: round_request_binding_order(item[0], item[1]))
    if (binding["target_sha"], binding.get("base_sha")) != request_binding:
        print(
            f"remote: packet request and latest binding disagree for round {round_id}",
            file=sys.stderr)
        return 1

    # Pin the publication evidence once: the remote-tracking ref resolves to one immutable SHA
    # here, and every git access below uses that literal or the binding's literal target.
    up = upstream_ref(root)
    if not up:
        print("remote: no upstream tracking branch — packet publication unverifiable",
              file=sys.stderr)
        return 1
    rc, remote_sha, error = git_rc(root, "rev-parse", "--verify", f"refs/remotes/{up}^{{commit}}")
    if rc != 0 or not remote_sha:
        print(f"remote: cannot resolve refs/remotes/{up}: {error or 'unknown ref'}",
              file=sys.stderr)
        return 1
    if not is_ancestor(root, binding["target_sha"], remote_sha):
        print(
            f"remote: packet Reviewing target {binding['target_sha']} is not contained in {up}",
            file=sys.stderr)
        return 1
    for artifact in (request, binding_path):
        relative = artifact.resolve().relative_to(root.resolve()).as_posix()
        published = _remote_blob(root, f"{remote_sha}:{relative}")
        if published is None:
            print(f"remote: {relative} is not published in {up}", file=sys.stderr)
            return 1
        if published != artifact.read_bytes():
            print(f"remote: {relative} differs from the published copy in {up}", file=sys.stderr)
            return 1
    # The SHAs a reviewer can trust are the two the gate actually verified: the pinned remote
    # tip whose tree carries the packet bytes, and the closeout the binding names. The local
    # checkout appears nowhere in this judgment.
    print(f"remote: round {round_id} request and binding are published in "
          f"{up}@{remote_sha[:12]} — Reviewing {binding['target_sha']}")
    return 0


# ---- strict marker schema (a marker is BELIEVED only if every field is the exact type) --------
def _is_sha(v: object) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(r"[0-9a-f]{40}", v))


def _is_cycle(v: object) -> bool:
    return type(v) is int and v >= 1  # `type(... ) is int` rejects bool (a subtype) and float


def _is_strlist(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _nonempty_str(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _literal_reviewer(v: object) -> bool:
    return _nonempty_str(v) and not str(v).startswith("role:")


def marker_valid(m: dict) -> bool:
    """Type-strict schema gate. `cycle: true`, `review_cycle: 1.0`, `reviewed_sha: <not-40-hex>`,
    `decision_required: {}`, `resolved: "yes"` etc. are all rejected here (ignored), never coerced.
    SHA/base_sha are validated when present; binding to head/base is a separate freshness check."""
    k = m.get("_kind")
    if k == "review-cycle":
        return (_is_cycle(m.get("cycle")) and _is_sha(m.get("target_sha"))
                and (m.get("base_sha") is None or _is_sha(m.get("base_sha")))
                and (m.get("reviewers") is None or (
                    _is_strlist(m.get("reviewers"))
                    and all(_literal_reviewer(value) for value in m["reviewers"])))
                and (m.get("profile_fingerprint") is None
                     or _nonempty_str(m.get("profile_fingerprint"))))
    if k == "review-result":
        return (_is_cycle(m.get("review_cycle")) and _is_sha(m.get("reviewed_sha"))
                and _literal_reviewer(m.get("reviewer")) and _nonempty_str(m.get("verdict"))
                and _is_strlist(m.get("decision_required", [])))
    if k == "findings":
        return _is_cycle(m.get("cycle")) and type(m.get("resolved")) is bool
    if k == "approval":
        return (_is_sha(m.get("sha")) and _is_cycle(m.get("cycle")) and _nonempty_str(m.get("by"))
                and (m.get("base_sha") is None or _is_sha(m.get("base_sha"))))
    return False


def parse_markers(text: str, kind: str | None = None) -> list[dict]:
    """Extract waystone-*:v1 markers from a blob. Markers inside ``` fenced blocks are ignored
    (a quoted example must not be read as live state)."""
    out = []
    clean = FENCE_RE.sub("", text or "")
    for m in MARKER_RE.finditer(clean):
        k, body = m.group(1), m.group(2)
        if kind and k != kind:
            continue
        try:
            d = yaml.safe_load(body) or {}
        except yaml.YAMLError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        d["_kind"] = k
        out.append(d)
    return out


def parse_bodies(bodies: list[dict]) -> list[dict]:
    """Parse markers per comment, preserving author / effective-timestamp / id as _author/_at/_id.
    `_at` is the comment's EFFECTIVE time (updated_at, not created_at) so editing an old comment
    into a marker can't masquerade as having been posted at the old time."""
    out = []
    for b in bodies:
        for m in parse_markers(b.get("body", "")):
            m["_author"] = b.get("author", "")
            m["_at"] = b.get("at", "")
            m["_id"] = b.get("id")
            out.append(m)
    return out


def latest_cycle(markers: list[dict], operators: tuple = ()) -> dict | None:
    """The freeze boundary: the LATEST marker (by timestamp) of the highest cycle number — a
    re-post of the same cycle advances the boundary. When `operators` is given, only freeze markers
    POSTED by a trusted operator count, so an untrusted actor can't inject a higher cycle to hijack
    the frozen target."""
    cycles = [m for m in markers if m.get("_kind") == "review-cycle" and marker_valid(m)
              and (not operators or m.get("_author") in operators)]
    return max(cycles, key=lambda m: (m["cycle"], m.get("_at") or "")) if cycles else None


def next_cycle_number(markers: list[dict]) -> int:
    lc = latest_cycle(markers)
    return (lc["cycle"] + 1) if lc else 1


def classify(markers: list[dict], current_head: str, macro_reviewers: tuple = (),
             approvers: tuple = (), operators: tuple = (), current_base: str | None = None,
             codex_signal_at: str | None = None) -> dict:
    """Strict, provenance-bound classification of PR review state vs the current head/base.

    A marker's GitHub author (`_author`, the actor who posted it) is a separate provenance from
    the logical `reviewer`/`by` it claims. When `operators`/`approvers` are given, cycle/result/
    findings markers are only believed from a trusted operator, and an approval only from a
    trusted approver whose `by` matches who actually posted it.

    Each fact is the LATEST trusted state, never "one past success": every configured macro
    reviewer must have a latest merge-compatible result (a later not-shipped cancels an earlier
    shipped); findings use the latest resolution. A cycle is fresh only if BOTH the frozen head
    and the frozen base equal the current head/base (`current_base` given) — base drift means the
    merged tree differs from what was reviewed. The human approval is bound to (cycle, head, base)
    and must POST-DATE every piece of evidence (the newest Codex signal, the latest macro result,
    the latest findings resolution), so re-freezing to a new cycle/base cannot reuse a stale
    approval. Markers sharing the newest timestamp with conflicting content fail closed.
    Conflicting freeze markers for the latest cycle fail closed."""
    if any(isinstance(reviewer, str) and reviewer.startswith("role:")
           for reviewer in macro_reviewers):
        raise WorkflowError(
            "role:reviewer must be resolved from the profile before classification")

    def at(m: dict) -> str:
        return m.get("_at") or ""

    trusted_cycles = [m for m in markers if m.get("_kind") == "review-cycle" and marker_valid(m)
                      and (not operators or m.get("_author") in operators)]
    # the freeze boundary is the LATEST marker of the highest cycle (a re-post of the same cycle is
    # a new boundary — Codex must review after it). Same cycle with a different (head, base) → block.
    if trusted_cycles:
        max_cycle = max(m["cycle"] for m in trusted_cycles)
        same_cycle = [m for m in trusted_cycles if m["cycle"] == max_cycle]
        conflict = len({(
            str(m.get("target_sha")), str(m.get("base_sha")),
            tuple(m.get("reviewers") or ()), str(m.get("profile_fingerprint")),
        ) for m in same_cycle}) > 1
        lc = max(same_cycle, key=at)
    else:
        conflict, lc = False, None
    cyc = lc["cycle"] if lc else None
    frozen = (lc or {}).get("target_sha")
    frozen_base = (lc or {}).get("base_sha")
    freeze_at = at(lc) if lc else ""
    base_ok = current_base is None or str(frozen_base) == current_base
    head_matches = bool(lc) and not conflict and str(frozen) == current_head and base_ok

    def latest_group(items: list[dict]) -> list[dict]:
        """All markers sharing the newest timestamp — so a same-timestamp conflict fails closed
        instead of arbitrarily picking the first of a tie."""
        mx = max((at(i) for i in items), default="")
        return [i for i in items if at(i) == mx]

    # results: valid, this head+cycle, trusted operator, and posted AFTER the freeze (evidence that
    # predates the frozen target can't be retroactively applied to it). Per macro reviewer the
    # newest result(s) must ALL be merge-compatible (a same-second shipped/not-shipped tie → not ok).
    results = [m for m in markers if m.get("_kind") == "review-result" and marker_valid(m)
               and str(m.get("reviewed_sha")) == current_head and m.get("review_cycle") == cyc
               and (not operators or m.get("_author") in operators) and at(m) > freeze_at]

    def mergeable(r: dict) -> bool:
        return str(r.get("verdict", "")).lower() in MERGE_OK_VERDICTS and not r.get("decision_required")

    def reviewer_ok(reviewer: str) -> bool:
        rs = [r for r in results if r.get("reviewer") == reviewer]
        return bool(rs) and all(mergeable(r) for r in latest_group(rs))

    pro_ok = all(reviewer_ok(rv) for rv in macro_reviewers) if macro_reviewers else True

    # findings: this cycle, trusted operator; the newest resolution(s) must all be resolved AND
    # post-date the newest Codex signal (a later 'resolved: false' or a fresh Codex finding blocks).
    cyc_findings = [m for m in markers if m.get("_kind") == "findings" and marker_valid(m)
                    and m.get("cycle") == cyc
                    and (not operators or m.get("_author") in operators) and at(m) > freeze_at]
    findings_group = latest_group(cyc_findings) if cyc_findings else []
    findings_at = max((at(f) for f in findings_group), default="")
    findings_resolved = (bool(findings_group) and all(f.get("resolved") is True for f in findings_group)
                         and (codex_signal_at is None or findings_at > codex_signal_at))

    # the approval must come AFTER every piece of evidence at this head — and after the freeze,
    # so the chronology freeze < {codex, macro result, findings} < approval is enforced.
    evidence = [freeze_at] if lc else []
    if results:
        evidence.append(max(at(r) for r in results))
    if cyc_findings:
        evidence.append(findings_at)
    if codex_signal_at:
        evidence.append(codex_signal_at)
    evidence_at = max(evidence) if evidence else None

    def approval_ok(a: dict) -> bool:
        author = a.get("_author", "")
        return (marker_valid(a)
                and str(a.get("sha")) == current_head
                and a.get("cycle") == cyc                              # bound to THIS cycle
                and (current_base is None or str(a.get("base_sha")) == current_base)  # and base
                and bool(author) and not author.endswith("[bot]")
                and (not approvers or author in approvers)
                and str(a.get("by", "")) == author  # claimed approver must equal who posted it
                and (evidence_at is None or at(a) > evidence_at))  # strictly after all evidence

    approvals = [m for m in markers if m.get("_kind") == "approval"]
    return {
        "current_head": current_head,
        "latest_cycle": cyc,
        "round_id": (lc or {}).get("round_id"),
        "profile_fingerprint": (lc or {}).get("profile_fingerprint"),
        "frozen_sha": frozen,
        "frozen_base": frozen_base,
        "cycle_conflict": conflict,
        "cycle_fresh": head_matches,
        "pro_result_at_head": pro_ok,
        "approved_at_head": any(approval_ok(a) for a in approvals),
        "findings_resolved": findings_resolved,
        "n_results": len(results),
        "n_approvals": len(approvals),
    }


def completed_pr_feedback_event(facts: dict, pr: int) -> dict | None:
    """Project a fully completed canonical PR review cycle to the shared feedback event shape."""
    reviewers = facts.get("reviewers")
    if not isinstance(reviewers, list):
        return None
    round_id = facts.get("round_id")
    cycle = facts.get("latest_cycle")
    head = facts.get("current_head")
    if (not isinstance(round_id, str) or not round_id or round_id == "(unset)"
            or type(cycle) is not int or not _is_sha(head)
            or facts.get("cycle_fresh") is not True or facts.get("approved_at_head") is not True):
        return None
    if "codex" in reviewers and (facts.get("codex_fresh") is not True
                                 or facts.get("findings_resolved") is not True):
        return None
    if any(reviewer != "codex" for reviewer in reviewers) \
            and facts.get("pro_result_at_head") is not True:
        return None
    return {
        "event": "review-feedback", "source": "pr-marker", "round_id": round_id,
        "event_id": f"pr:{pr}:cycle:{cycle}:head:{head}",
    }


# ---- gh I/O (isolated) -------------------------------------------------------
def _gh(root: Path, *args: str) -> tuple[int, str]:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, cwd=str(root))
    except (OSError, subprocess.TimeoutExpired) as e:
        return (127, str(e))
    return (out.returncode, out.stdout.strip() if out.returncode == 0 else out.stderr.strip())


def resolve_repo(root: Path) -> str | None:
    rc, out = _gh(root, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    return out if rc == 0 and out else None


def file_at_ref(root: Path, repo: str, path: str, ref: str) -> str | None:
    """Read a file's contents from the PR head SHA on GitHub (decouples the gate from the local
    checkout, which may be a different/dirty tree). `--method GET` is mandatory: a bare `-f`
    flips `gh api` to POST, which the read-only contents endpoint rejects (404)."""
    rc, out = _gh(root, "api", "--method", "GET", f"repos/{repo}/contents/{path}",
                  "-f", f"ref={ref}", "-q", ".content")
    if rc != 0 or not out:
        return None
    try:
        return base64.b64decode(out).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def rest_reviews(root: Path, repo: str, pr: int) -> list[dict]:
    """Formal PR reviews via REST — the only source that carries `commit_id`, the SHA a review
    was submitted against (`gh pr view --json reviews` omits it). `--slurp` is required with
    `--paginate`: without it gh concatenates one JSON array per page (invalid combined JSON), so a
    PR with >30 reviews would fail to parse and silently drop reviews. Empty on any failure."""
    rc, out = _gh(root, "api", "--method", "GET", f"repos/{repo}/pulls/{pr}/reviews",
                  "--paginate", "--slurp")
    if rc != 0 or not out:
        return []
    try:
        pages = json.loads(out)
    except json.JSONDecodeError:
        return []
    flat = []
    for page in (pages if isinstance(pages, list) else []):
        flat.extend(page if isinstance(page, list) else [page])
    return [{"id": r.get("id"), "author": (r.get("user") or {}).get("login", ""),
             "body": r.get("body", ""), "state": r.get("state", ""),
             "commit_id": r.get("commit_id", ""), "at": r.get("submitted_at", "")}
            for r in flat if isinstance(r, dict)]


def rest_comments(root: Path, repo: str, pr: int) -> list[dict]:
    """ALL PR issue comments via REST (paginated) — `gh pr view --json comments` caps at the first
    100, so a 101st comment that flips state (a new freeze, a not-shipped result, a reopened
    finding) would be invisible. `at` is the EFFECTIVE time (updated_at) so an edited old comment
    can't pose as old. Empty on any failure."""
    rc, out = _gh(root, "api", "--method", "GET", f"repos/{repo}/issues/{pr}/comments",
                  "--paginate", "--slurp")
    if rc != 0 or not out:
        return []
    try:
        pages = json.loads(out)
    except json.JSONDecodeError:
        return []
    flat = []
    for page in (pages if isinstance(pages, list) else []):
        flat.extend(page if isinstance(page, list) else [page])
    return [{"id": c.get("id"), "author": (c.get("user") or {}).get("login", ""),
             "body": c.get("body", ""),
             "at": c.get("updated_at") or c.get("created_at") or "",
             "created_at": c.get("created_at", ""), "updated_at": c.get("updated_at", "")}
            for c in flat if isinstance(c, dict)]


def pr_bundle(root: Path, pr: int, repo: str | None = None) -> dict | None:
    rc, out = _gh(root, "pr", "view", str(pr), "--json",
                  "headRefOid,baseRefOid,statusCheckRollup,mergeStateStatus,state,isDraft,baseRefName,headRefName")
    if rc != 0:
        print(f"review: gh pr view {pr} failed: {out}", file=sys.stderr)
        return None
    j = json.loads(out)
    if repo is None:
        repo = resolve_repo(root)
    # comments + formal reviews are both fetched via paginated REST (the canonical event log) — a
    # marker can live in either, and operator/author filtering decides whether it's believed.
    # Markers (cycle/result/findings/approval) live ONLY in issue comments. Formal reviews are used
    # solely as Codex signals (commit_id) — a marker in a PENDING/unsubmitted review body must NOT
    # count, so review bodies are deliberately NOT parsed for markers.
    comments = rest_comments(root, repo, pr) if repo else []
    bodies = [{"id": c["id"], "body": c["body"], "author": c["author"], "at": c["at"]} for c in comments]
    reviews = rest_reviews(root, repo, pr) if repo else []
    return {
        "head": j.get("headRefOid", ""), "base_sha": j.get("baseRefOid", ""),
        "bodies": bodies, "reviews": reviews,
        "checks": j.get("statusCheckRollup", []) or [],
        "merge_state": j.get("mergeStateStatus", ""), "state": j.get("state", ""),
        "is_draft": bool(j.get("isDraft")), "base": j.get("baseRefName", ""), "head_ref": j.get("headRefName", ""),
    }


# Codex prints exactly "**Reviewed commit:** `<sha>`" on its OWN line — match only that, anchored
# to line start/end with required backticks, never a loose substring. Rejects quoted ("> ..."),
# negated ("Not reviewed commit", "I did not review ... Reviewed commit: ..."), and inline-prose
# occurrences of the SHA.
REVIEWED_COMMIT_RE = re.compile(
    r"(?mi)^\s*\*{0,2}Reviewed commit:\*{0,2}\s*`([0-9a-f]{10,40})`\s*\.?\s*$")


def _codex_comment_reviews(body: str, target_sha: str) -> bool:
    return any(target_sha.startswith(h.lower()) for h in REVIEWED_COMMIT_RE.findall(body or ""))


def codex_signals_at_head(reviews: list[dict], comment_bodies: list[dict],
                          target_sha: str | None, since_at: str | None = None) -> list[dict]:
    """Every Codex signal bound to the EXACT target tree, as {kind, id, at}. Two recordings count:
      (1) a formal Codex review whose `commit_id == target_sha`, or
      (2) a Codex-bot COMMENT whose `Reviewed commit:` field names target_sha (the connector's
          normal no-issue path posts a comment, not a formal review object).
    Only the GitHub-verified Codex bot login is trusted (un-spoofable). A bare 👍 reaction can't be
    SHA-bound and is not a signal — re-request a textual `@codex review`. When `since_at` is given
    (the latest freeze time), only signals STRICTLY AFTER it count, so a re-freeze (new cycle/base
    on the same head) cannot reuse a Codex review from a previous cycle; an equal timestamp is
    order-ambiguous and fails closed."""
    if not target_sha:
        return []

    def fresh(ts: str) -> bool:
        return since_at is None or (ts or "") > since_at

    out = []
    for r in reviews:
        if (is_codex(r.get("author")) and r.get("commit_id") == target_sha
                and r.get("state") in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED")
                and fresh(r.get("at") or "")):
            out.append({"kind": "review", "id": r.get("id"), "at": r.get("at") or ""})
    for b in comment_bodies:
        if (is_codex(b.get("author")) and _codex_comment_reviews(b.get("body") or "", target_sha)
                and fresh(b.get("at") or "")):
            out.append({"kind": "comment", "id": b.get("id"), "at": b.get("at") or ""})
    return out


def codex_fresh(reviews: list[dict], comment_bodies: list[dict], target_sha: str | None) -> bool:
    return bool(codex_signals_at_head(reviews, comment_bodies, target_sha))


def ci_state(bundle: dict) -> str:
    """Strict: only SUCCESS counts as passing. Unknown/neutral/skipped/action-required are
    treated as non-passing (fail-closed under require_ci)."""
    checks = bundle.get("checks", [])
    if not checks:
        return "none"
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(s in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", "REQUESTED") for s in states):
        return "pending"
    # Only a SUCCESS *conclusion* passes. COMPLETED is a run *status* (it finished), not a verdict;
    # NEUTRAL/SKIPPED/ACTION_REQUIRED and any unknown enum fail closed.
    if all(s == "SUCCESS" for s in states):
        return "passing"
    return "failing"


def resolve_reviewer_set(root: Path, configured: list[str] | tuple[str, ...]) \
        -> tuple[list[str], str | None]:
    """Resolve `role:reviewer` to a namespaced backend identity and profile fingerprint."""
    if not any(reviewer == "role:reviewer" for reviewer in configured):
        return list(configured), None
    import delegate

    try:
        profile, fingerprint = delegate._load_profile(root)
    except WorkflowError as e:
        raise WorkflowError(
            f"{e}\nAlternatively, keep literal reviewer compatibility in .waystone.yml, e.g. "
            "`review: {reviewers: [codex, gpt-5.5-pro]}`.") from e
    bindings = profile.get("bindings")
    binding = bindings.get("reviewer") if isinstance(bindings, dict) else None
    if not isinstance(binding, dict):
        raise WorkflowError(
            f"review.reviewers uses 'role:reviewer' but profile has no binding for role "
            f"'reviewer' at {delegate._profile_path(root)}; add that binding or keep a literal "
            "compatibility list in .waystone.yml, e.g. `reviewers: [codex, gpt-5.5-pro]`")
    delegate._validate_profile_binding("reviewer", binding)
    backend = binding["backend"]
    resolved = [backend if reviewer == "role:reviewer" else reviewer for reviewer in configured]
    if any(reviewer.startswith("role:") for reviewer in resolved):
        raise WorkflowError("review reviewer identities must be literal after profile resolution")
    return resolved, fingerprint


def resolve_reviewers(root: Path, configured: list[str] | tuple[str, ...]) -> list[str]:
    """Compatibility wrapper for request renderers that only need the resolved identities."""
    return resolve_reviewer_set(root, configured)[0]


def facts_from_bundle(bundle: dict, cfg: dict, repo: str | None,
                      *, root: Path | None = None) -> dict:
    owner = (repo.split("/", 1)[0] if repo else "")
    approvers = tuple({owner, *cfg["review"].get("approvers", [])} - {""})
    operators = tuple({owner, *cfg["review"].get("operators", [])} - {""})
    configured = cfg["review"]["reviewers"]
    markers = parse_bodies(bundle["bodies"])
    # Codex signals must be bound to the exact head AND post-date the latest freeze — so a re-freeze
    # (new cycle/base, same head) can't reuse a Codex review from a previous cycle. The newest
    # signal's timestamp also gates findings/approval freshness.
    lc = latest_cycle(markers, operators)
    frozen_reviewers = lc.get("reviewers") if lc else None
    reviewers = list(frozen_reviewers) if isinstance(frozen_reviewers, list) else []
    frozen_contract_ok = isinstance(frozen_reviewers, list) or not configured
    reviewer_profile_drift = False
    if any(reviewer == "role:reviewer" for reviewer in configured):
        if root is None:
            reviewer_profile_drift = True
        else:
            try:
                current_reviewers, current_fingerprint = resolve_reviewer_set(root, configured)
            except WorkflowError:
                reviewer_profile_drift = True
            else:
                reviewer_profile_drift = (
                    not lc
                    or lc.get("profile_fingerprint") != current_fingerprint
                    or reviewers != current_reviewers
                )
    macro = tuple(r for r in reviewers if r != "codex")
    freeze_at = lc.get("_at") if lc else None
    signals = codex_signals_at_head(bundle.get("reviews", []), bundle.get("bodies", []),
                                    bundle["head"], since_at=freeze_at)
    codex_at = max((s["at"] for s in signals), default=None) if signals else None
    cls = classify(markers, bundle["head"], macro_reviewers=macro, approvers=approvers,
                   operators=operators, current_base=bundle.get("base_sha") or None,
                   codex_signal_at=codex_at)
    if not frozen_contract_ok or reviewer_profile_drift:
        cls["cycle_fresh"] = False
    cls["reviewers"] = reviewers
    cls["reviewer_profile_drift"] = reviewer_profile_drift
    cls["codex_fresh"] = bool(signals)
    cls["ci"] = ci_state(bundle)
    cls["pr_state"] = bundle["state"]
    cls["is_draft"] = bundle["is_draft"]
    cls["base"] = bundle["base"]
    cls["merge_state"] = bundle["merge_state"]
    return cls


def pr_context(root: Path, pr: int) -> dict | None:
    """The canonical PR context shared by freeze/status/approve/merge. The trust POLICY is read
    from the PR's BASE SHA (the protected target branch) — never head or the local checkout — so
    every command agrees on one policy and a candidate branch can't enable pr-mode or widen its own
    reviewer/operator/approver set. `policy` is None if the base config can't be read/parsed."""
    repo = resolve_repo(root)
    bundle = pr_bundle(root, pr, repo)
    if bundle is None:
        return None
    base_sha = bundle.get("base_sha")
    policy = None
    if repo and base_sha:
        txt = file_at_ref(root, repo, CONFIG_NAME, base_sha)
        if txt is not None:
            try:
                policy = normalize_config(yaml.safe_load(txt))
            except (yaml.YAMLError, ValueError):
                policy = None
    return {"repo": repo, "pr": pr, "bundle": bundle, "head": bundle["head"],
            "base_sha": base_sha, "base": bundle.get("base"), "policy": policy}


# ---- CLI ---------------------------------------------------------------------
def _opt(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i < len(argv) - 1:
            return argv[i + 1]
    return None


def _root(argv: list[str]) -> Path | None:
    flags = ("--pr", "--round", "--sha", "--commit", "--reviewer", "--file", "--narrative")
    positional = [a for i, a in enumerate(argv)
                  if not a.startswith("--") and (i == 0 or argv[i - 1] not in flags)]
    if positional:
        return Path(positional[-1]).resolve()
    return find_project_root(Path.cwd())


def freeze(root: Path, pr: int, round_id: str | None) -> int:
    ctx = pr_context(root, pr)
    if ctx is None:
        return 1
    policy = ctx["policy"]
    if policy is None:
        print("review freeze: cannot read the base-branch policy (.waystone.yml at the PR "
              "base SHA) — pr-mode review is gated on the protected base config.", file=sys.stderr)
        return 1
    if policy["review"]["mode"] != "pr":
        print("review freeze: the base branch's review.mode is not 'pr'. PR-mode review applies "
              "only once the base policy is pr — review the packet→pr transition PR in packet mode "
              "first, merge it, then pr-mode applies from the next PR.", file=sys.stderr)
        return 1
    bundle = ctx["bundle"]
    head = bundle["head"] or git_full_sha(root, "HEAD")
    base_sha = bundle.get("base_sha", "")
    markers = parse_bodies(bundle["bodies"])
    n = next_cycle_number(markers)
    reviewers, profile_fingerprint = resolve_reviewer_set(
        root, policy["review"]["reviewers"])
    request_text = None
    if round_id is not None:
        request = prepared_request_path(root, round_id, mode="pr")
        sidecar_paths = sorted(request.parent.glob(f"{round_id}-request.binding*.json"))
        if not request.is_file() or not sidecar_paths:
            print(
                "review freeze: prepared PR request or its binding is missing; run "
                f"waystone review prepare --round {round_id} --narrative PATH first",
                file=sys.stderr)
            return 1
        try:
            rows = [(path, read_round_request_binding(path, expected_round_id=round_id))
                    for path in sidecar_paths]
            _binding_path, request_sidecar = max(
                rows, key=lambda item: round_request_binding_order(*item))
            # The published carrier is RE-RENDERED from the same inputs prepare used (round
            # exposure + stored narrative) — the on-disk request file is never re-trusted, so
            # a post-prepare edit cannot reach the PR comment. Re-rendering IS the integrity.
            _exposure_path, exposure = read_round_closeout_exposure(root, round_id)
            narrative = _read_review_narrative(stored_narrative_path(root, round_id))
            request_text = _render_review_request(round_id, exposure, narrative)
        except (OSError, UnicodeDecodeError, WorkflowError) as e:
            print(
                f"review freeze: prepared PR request is not reproducible ({e}); re-run "
                f"waystone review prepare --round {round_id} --narrative PATH",
                file=sys.stderr)
            return 1
        request_binding = (exposure["head_sha"], exposure.get("base_sha"))
        if (request_sidecar["mode"] != "pr"
                or (request_sidecar["target_sha"], request_sidecar.get("base_sha"))
                != request_binding
                or request_sidecar["target_sha"] != head
                or request_sidecar["reviewers"] != reviewers):
            print(
                "review freeze: prepared PR request does not match the current PR head/reviewer "
                "binding; reclose and prepare again with a new round id",
                file=sys.stderr)
            return 1
    marker = emit_marker("review-cycle", {
        "round_id": round_id or "(unset)", "cycle": n, "target_sha": head,
        "base_sha": base_sha, "reviewers": reviewers,
        "profile_fingerprint": profile_fingerprint,
    })
    macro = [r for r in reviewers if r != "codex"]
    body = (f"## Review cycle {n} — frozen at `{head[:12]}` (base `{base_sha[:12]}`)\n\n"
            f"Immutable review target for cycle {n}. A new push — or a base advance — makes this "
            f"cycle stale.\n\n"
            + ("@codex review\n\n" if "codex" in reviewers else "")
            + (f"Macro reviewer(s) — {', '.join(macro)}: review at the SHA above; end your reply with "
               f"a `waystone-review-result` footer carrying `reviewed_sha: {head}` and `review_cycle: {n}`.\n\n"
               if macro else "")
            + ((request_text.rstrip() + "\n\n") if request_text is not None else "")
            + marker + "\n")
    rc, out = _gh(root, "pr", "comment", str(pr), "--body", body)
    if rc != 0:
        print(f"review freeze: gh pr comment failed: {out}", file=sys.stderr)
        return 1
    if round_id is not None:
        try:
            write_pr_freeze_binding(
                root, round_id, pr, n, head, base_sha, reviewers, profile_fingerprint,
                policy["reviews_dir"])
        except (OSError, WorkflowError) as e:
            print(
                f"review freeze: PR comment posted but local freeze binding was not recorded: {e}",
                file=sys.stderr,
            )
            return 1
    print(f"review cycle {n} frozen at {head[:12]} on PR #{pr} (reviewers: {', '.join(reviewers)})")
    return 0


def status(root: Path, pr: int | None) -> int:
    if pr is not None:
        ctx = pr_context(root, pr)
        if ctx is None:
            return 1
        if ctx["policy"] is None:
            print("review status: cannot read the base-branch policy at the PR base SHA.", file=sys.stderr)
            return 1
        facts = facts_from_bundle(ctx["bundle"], ctx["policy"], ctx["repo"], root=root)
        print(f"PR #{pr} review status ({facts['pr_state']}{', DRAFT' if facts['is_draft'] else ''}):")
        print(f"  current head:   {facts['current_head'][:12]}")
        print(f"  latest cycle:   {facts['latest_cycle']} (frozen {str(facts['frozen_sha'])[:12]})")
        print(f"  cycle fresh:    {facts['cycle_fresh']}  (False = push after freeze → re-freeze)")
        print(f"  profile drift:  {facts['reviewer_profile_drift']}")
        print(f"  codex fresh:    {facts['codex_fresh']}")
        print(f"  CI:             {facts['ci']}")
        print(f"  pro result@head:{facts['pro_result_at_head']}  ({facts['n_results']} result(s))")
        print(f"  findings resolved: {facts['findings_resolved']}")
        print(f"  approved@head:  {facts['approved_at_head']}  ({facts['n_approvals']} approval(s))")
        round_id = facts.get("round_id")
        if (isinstance(round_id, str) and round_id and round_id != "(unset)"
                and _is_cycle(facts.get("latest_cycle"))
                and _is_sha(facts.get("frozen_sha"))
                and _is_sha(facts.get("frozen_base"))
                and _is_strlist(facts.get("reviewers"))
                and facts.get("cycle_conflict") is False):
            try:
                write_pr_freeze_binding(
                    root, round_id, pr, facts["latest_cycle"], facts["frozen_sha"],
                    facts["frozen_base"], facts["reviewers"],
                    facts.get("profile_fingerprint"), ctx["policy"]["reviews_dir"])
            except (OSError, WorkflowError) as e:
                print(f"review status: trusted PR cycle could not be recorded locally: {e}",
                      file=sys.stderr)
                return 1
        event = completed_pr_feedback_event(facts, pr)
        if event is not None:
            try:
                import overlay
                overlay.record_review_feedback(
                    root, event["round_id"], source=event["source"], event_id=event["event_id"])
            except Exception as e:  # noqa: BLE001 — feedback observation never changes status exit
                print(f"review status: overlay PR feedback observation unavailable ({e}) — "
                      "status still succeeded", file=sys.stderr)
        return 0
    cfg = load_config(root)  # packet-mode status uses the local config (no PR to read a base from)
    rdir = root / cfg["reviews_dir"]
    if not rdir.is_dir():
        print("no reviews dir yet")
        return 0
    reqs = sorted(p.stem[: -len("-request")] for p in rdir.glob("*-request.md"))
    awaiting = pending_reviews(root)
    print(f"packet reviews: {len(reqs)} requested, {len(awaiting)} awaiting feedback")
    for row in awaiting:
        print(f"  pending: {row['round_id']}")
    return 0


def pending(root: Path) -> int:
    rows = pending_reviews(root)
    print(f"pending packet reviews: {len(rows)}")
    for row in rows:
        print(f"  {format_pending_review(row)}")
    return 0


def _parse_findings(text: str) -> list[dict]:
    """Parse the output contract's finding blocks into a triage skeleton. Best-effort: a reply that
    does not follow the contract yields []. Verdicts stay blank — triage is verify-then-register."""
    heads = list(FINDING_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sev = SEVERITY_RE.search(text[m.end():end])
        out.append({"id": m.group(1), "title": m.group(2).strip(), "severity": sev.group(1) if sev else "?"})
    return out


_FEEDBACK_HEADER_SEPARATOR = b"\n\n---\n\n"
_VERBATIM_BYTES_RE = re.compile(rb"^verbatim-bytes: (\d{1,12})$", re.MULTILINE)


def _triage_marker_start(content: bytes) -> int:
    """Derive the canonical tail-marker offset from the script-written header's verbatim-bytes
    record — arithmetic, never a content search, so a reply quoting the marker strings can
    neither mask a damaged canonical marker nor be mistaken for it."""
    sep = content.find(_FEEDBACK_HEADER_SEPARATOR)
    if sep < 0:
        raise WorkflowError(
            "feedback triage marker anchor missing: no ingest header separator")
    header_match = _VERBATIM_BYTES_RE.search(content[:sep])
    if header_match is None:
        raise WorkflowError(
            "feedback triage marker anchor missing: no verbatim-bytes header — "
            "re-ingest with --force to record it")
    body_start = sep + len(_FEEDBACK_HEADER_SEPARATOR)
    marker_start = body_start + int(header_match.group(1)) + len(_FEEDBACK_HEADER_SEPARATOR)
    end_suffix = b"\n" + TRIAGE_END + b"\n"
    if (not content.endswith(end_suffix)
            or content[marker_start - 1:marker_start + len(TRIAGE_BEGIN) + 1]
            != b"\n" + TRIAGE_BEGIN + b"\n"
            or marker_start + len(TRIAGE_BEGIN) >= len(content) - len(end_suffix) + 1):
        raise WorkflowError("feedback triage markers are missing or damaged")
    return marker_start


def _marked_triage(content: bytes) -> bytes:
    return TRIAGE_BEGIN + b"\n" + content + (b"" if content.endswith(b"\n") else b"\n") \
        + TRIAGE_END + b"\n"


def triage(root: Path, round_id: str, src: Path) -> int:
    """Replace the marked feedback tail while leaving every preceding byte untouched."""
    cfg = load_config(root)
    dest = root / cfg["reviews_dir"] / f"{round_id}-feedback.md"
    try:
        content = dest.read_bytes()
        replacement = Path(src).read_bytes()
    except OSError as e:
        raise WorkflowError(f"review triage input unavailable: {e}") from e
    marker_start = _triage_marker_start(content)
    if TRIAGE_BEGIN in replacement or TRIAGE_END in replacement:
        raise WorkflowError("review triage input must not contain triage marker strings")
    write_bytes_atomic(dest, content[:marker_start] + _marked_triage(replacement))
    print(f"updated triage section → {dest}")
    return 0


def ingest(root: Path, round_id: str | None, src: Path = INBOX, reviewer: str | None = None,
           force: bool = False) -> int:
    """Byte-exact ingest of an external review reply.

    The user saves the reviewer's reply to `src` (default /tmp/review.md) in a separate shell
    (`cat > /tmp/review.md`, paste, Ctrl-D); this copies the body VERBATIM into
    <reviews_dir>/<round-id>-feedback.md under a metadata header, then APPENDS a finding triage
    skeleton beneath it. Identity comes only from the reply's leading structured header; the
    deprecated ``reviewer`` argument is never identity evidence. The verbatim body is never edited.
    Round id from --round, else the newest <reviews_dir>/*-request.md."""
    import datetime
    cfg = load_config(root)
    if not src.is_file():
        print(f"review ingest: no review at {src}. In a SEPARATE shell run `cat > {src}`, paste "
              f"the reviewer's reply, press Ctrl-D, then re-run.", file=sys.stderr)
        return 1
    body = src.read_bytes()
    if not body.strip():
        print(f"review ingest: {src} is empty — save the reply there first.", file=sys.stderr)
        return 1
    rdir = root / cfg["reviews_dir"]
    if round_id is None:
        reqs = sorted(p.stem[: -len("-request")] for p in rdir.glob("*-request.md")) if rdir.is_dir() else []
        if reqs:
            round_id = reqs[-1]
        else:
            print("review ingest: no --round given and no *-request.md to infer it from.",
                  file=sys.stderr)
            return 1
    rdir.mkdir(parents=True, exist_ok=True)
    dest = rdir / f"{round_id}-feedback.md"

    parsed_header = parse_review_reply_header(body)
    binding, binding_reason = ingest_round_binding(root, round_id, cfg)
    assessment = assess_review_reply(parsed_header, binding)
    if reviewer is not None:
        print("review ingest: warning: --reviewer is ignored; the reply header is authoritative",
              file=sys.stderr)
    if not parsed_header["detected"]:
        print("review ingest: warning: structured reply header not found; model, effort, and "
              "review-target are unknown", file=sys.stderr)
    for warning in parsed_header["warnings"]:
        print(f"review ingest: warning: reply header {warning}; affected field is unknown",
              file=sys.stderr)
    if binding is None:
        print(f"review ingest: warning: round binding unavailable ({binding_reason}); reply cannot "
              "count as configured feedback", file=sys.stderr)
    elif assessment["review_target_matches"] is False:
        print("review ingest: warning: declared review-target does not match the round binding; "
              "reply cannot count as configured feedback", file=sys.stderr)
    if (parsed_header["model"] is not None and binding is not None
            and assessment["reviewer_coverage_reason"] == "reviewer-not-configured"):
        print("review ingest: warning: declared model does not match a reviewer frozen in the "
              "round binding; reply cannot count as configured feedback", file=sys.stderr)

    findings = _parse_findings(body.decode("utf-8", "replace"))

    # --- marked triage skeleton (beneath the verbatim body, which is never edited) ---
    lines = ["## Findings (triage skeleton — verify each before registering)", ""]
    if findings:
        lines.append("| finding | severity | type | verdict (REAL/REJECTED/NEEDS-RULING) | evidence | task id |")
        lines.append("|---|---|---|---|---|---|")
        for f in findings:
            lines.append(f"| {f['id']} — {f['title']} | {f['severity']} |  |  |  |  |")
    else:
        lines.append("_No `JW-GPT-NNN` finding blocks parsed — triage the verbatim reply directly._")
    triage_body = ("\n".join(lines) + "\n").encode("utf-8")
    appended = b"\n\n---\n\n" + _marked_triage(triage_body)

    metadata_json = json.dumps(
        {"metadata": parsed_header["metadata"]}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"))
    header = (
        "<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact "
        "copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->\n"
        f"round: {round_id}\n"
        f"reviewer: {parsed_header['model'] or '(unknown)'}\n"
        f"reviewer-effort: {parsed_header['effort'] or '(unknown)'}\n"
        f"review-target: {parsed_header['review_target'] or '(unknown)'}\n"
        f"{_FEEDBACK_METADATA_PREFIX}{metadata_json}\n"
        f"ingested: {datetime.date.today().isoformat()}\n"
        f"source: {src}\n"
        f"verbatim-bytes: {len(body)}\n\n---\n\n"
    )
    content = header.encode("utf-8") + body + appended
    prior_content = dest.read_bytes() if force and dest.is_file() else None
    if force:
        write_bytes_atomic(dest, content)
    else:
        try:
            with open(dest, "xb") as f:
                f.write(content)
        except FileExistsError:
            print(f"review ingest: feedback already exists for round {round_id}: {dest}; "
                  "pass --force to replace it", file=sys.stderr)
            return 1
    if force:
        try:
            import overlay
            overlay.record_review_ingest(
                root, round_id, reply_header=parsed_header, force=True)
        except Exception as e:  # noqa: BLE001 — rollback keeps the correction surfaces coherent
            try:
                if prior_content is None:
                    dest.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(dest, prior_content)
            except OSError as rollback_error:
                print(f"review ingest: forced overlay correction failed ({e}) and feedback "
                      f"rollback failed ({rollback_error})", file=sys.stderr)
                return 1
            print(f"review ingest: forced overlay correction failed ({e}); feedback replacement "
                  "was rolled back and the source was preserved", file=sys.stderr)
            return 1
    src.unlink()
    print(f"ingested {len(body)} bytes verbatim → {dest} (consumed {src})")
    print(f"  {len(findings)} finding(s) parsed — verify each before registering")
    # A normal ingest remains useful even if the advisory event store is unavailable. A forced
    # correction is handled above and rolls the feedback file back unless both surfaces update.
    if not force:
        try:
            import overlay
            overlay.record_review_ingest(
                root, round_id, reply_header=parsed_header)
        except Exception as e:  # noqa: BLE001
            print(f"review ingest: overlay ingest observation unavailable ({e}) — "
                  "ingest still succeeded", file=sys.stderr)
    # M2 §6: evaluate overlay warns at the review-ingest boundary (best-effort; never blocks).
    try:
        import overlay
        overlay.evaluate_boundary(root, "review-ingest", {"round_id": round_id})
    except Exception as e:  # noqa: BLE001
        print(f"review ingest: overlay warning unavailable ({e}) — ingest still succeeded",
              file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("freeze", "status", "pending", "prepare", "ingest", "triage"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    root = _root(rest)
    if root is None:
        print("review: no initialized project (missing .waystone.yml)", file=sys.stderr)
        return 1
    try:
        with hold_project_lock(root):
            migrate_project_state(root)
    except (WorkflowError, OSError) as e:
        print(f"waystone review: migration failed: {e}", file=sys.stderr)
        return 1
    try:
        if sub == "prepare":
            round_id = _opt(rest, "--round")
            narrative = _opt(rest, "--narrative")
            if not round_id or not narrative:
                print("review prepare: --round ID and --narrative PATH are required", file=sys.stderr)
                return 1
            with hold_project_lock(root):
                return prepare_review_request(root, round_id, Path(narrative))
        if sub == "ingest":
            with hold_project_lock(root):
                return ingest(root, _opt(rest, "--round"), reviewer=_opt(rest, "--reviewer"),
                              force="--force" in rest)
        if sub == "triage":
            round_id = _opt(rest, "--round")
            source = _opt(rest, "--file")
            if not round_id or not source:
                print("review triage: --round ID and --file PATH are required", file=sys.stderr)
                return 1
            with hold_project_lock(root):
                return triage(root, round_id, Path(source))
        if sub == "pending":
            return pending(root)
        pr_s = _opt(rest, "--pr")
        if sub == "freeze":
            if not pr_s:
                print("review freeze: --pr N is required", file=sys.stderr)
                return 1
            return freeze(root, int(pr_s), _opt(rest, "--round"))
        return status(root, int(pr_s) if pr_s else None)
    except WorkflowError as e:
        print(f"waystone review: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
