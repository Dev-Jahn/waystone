#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""SHA-bound review cycles for the PR-mode review profile.

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
  ingest [--round ID] [--reviewer M] [--force]  byte-exact copy /tmp/review.md →
                                                <id>-feedback.md, then append triage
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

from common import (  # noqa: E402
    CONFIG_NAME, WorkflowError, find_project_root, git_full_sha, hold_lock, load_config,
    migrate_project_state, normalize_config, project_lock_path, write_bytes_atomic,
)

CODEX_BOT = "chatgpt-codex-connector[bot]"  # REST `user.login` form
INBOX = Path("/tmp/review.md")  # fixed drop-file: user saves the reviewer reply here, byte-exact
ROUND_REQUEST_BINDING_SCHEMA = "waystone-round-request-binding-1"
PR_FREEZE_BINDING_SCHEMA = "waystone-pr-freeze-binding-1"
PACKET_REVIEWING_RE = re.compile(
    r"(?m)^- Reviewing:\s*([0-9a-f]{40})\s+\(diff against\s+([0-9a-f]{40}|\(root\))\)\s*$")


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


def write_round_request_binding(root: Path, round_id: str, target_sha: str, base_sha: str | None,
                                reviewers: list[str], *, mode: str) -> Path:
    """Append an immutable round-bound sidecar for packet request projection.

    A PR request row records the pre-freeze relationship but is never promoted to a reviewed-SHA
    fact; successful PR freeze has its own cycle-bound local evidence.
    """
    if not _is_sha(target_sha) or (base_sha is not None and not _is_sha(base_sha)):
        raise WorkflowError("round request binding requires full target/base commit SHAs")
    if mode not in ("packet", "pr") or not _is_strlist(reviewers):
        raise WorkflowError("round request binding requires packet|pr mode and literal reviewers")
    cfg = load_config(root)
    directory = Path(root) / cfg["reviews_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": ROUND_REQUEST_BINDING_SCHEMA, "round_id": round_id,
        "target_sha": target_sha, "base_sha": base_sha, "reviewers": reviewers,
        "mode": mode, "canonical_store": "github-pr-comment" if mode == "pr" else "local-packet",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    base = directory / f"{round_id}-request.binding.json"
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
        try:
            previous = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(previous, dict)
                and {key: value for key, value in previous.items() if key != "at"}
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
    """Read the packet template's single structured Reviewing line; ambiguity stays unknown."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    matches = PACKET_REVIEWING_RE.findall(text)
    if len(matches) != 1:
        return None
    target_sha, raw_base = matches[0]
    return target_sha, None if raw_base == "(root)" else raw_base


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
    flags = ("--pr", "--round", "--sha", "--commit", "--reviewer")
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
    fbs = {p.stem[: -len("-feedback")] for p in rdir.glob("*-feedback.md")}
    pending = [r for r in reqs if r not in fbs]
    print(f"packet reviews: {len(reqs)} requested, {len(pending)} awaiting feedback")
    for r in pending:
        print(f"  pending: {r}")
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


def ingest(root: Path, round_id: str | None, src: Path = INBOX, reviewer: str | None = None,
           force: bool = False) -> int:
    """Byte-exact ingest of an external review reply.

    The user saves the reviewer's reply to `src` (default /tmp/review.md) in a separate shell
    (`cat > /tmp/review.md`, paste, Ctrl-D); this copies the body VERBATIM into
    <reviews_dir>/<round-id>-feedback.md (NO model re-typing — the whole point) under a metadata
    header, then APPENDS a finding triage skeleton beneath it. The verbatim body is never edited.
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

    findings = _parse_findings(body.decode("utf-8", "replace"))

    # --- appended triage skeleton (beneath the verbatim body, which is never edited) ---
    lines = ["", "", "---", "", "## Findings (triage skeleton — verify each before registering)", ""]
    if findings:
        lines.append("| finding | severity | type | verdict (REAL/REJECTED/NEEDS-RULING) | evidence | task id |")
        lines.append("|---|---|---|---|---|---|")
        for f in findings:
            lines.append(f"| {f['id']} — {f['title']} | {f['severity']} |  |  |  |  |")
    else:
        lines.append("_No `JW-GPT-NNN` finding blocks parsed — triage the verbatim reply directly._")
    appended = ("\n".join(lines) + "\n").encode("utf-8")

    header = (
        "<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact "
        "copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->\n"
        f"round: {round_id}\n"
        f"reviewer: {reviewer or '(unknown)'}\n"
        f"ingested: {datetime.date.today().isoformat()}\n"
        f"source: {src}\n\n---\n\n"
    )
    content = header.encode("utf-8") + body + appended
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
    src.unlink()
    if (cfg.get("review") or {}).get("mode", "packet") == "packet":
        request_binding = parse_packet_request_binding(rdir / f"{round_id}-request.md")
        if request_binding is not None:
            target_sha, base_sha = request_binding
            try:
                write_round_request_binding(
                    root, round_id, target_sha, base_sha,
                    resolve_reviewers(root, (cfg.get("review") or {}).get("reviewers", [])),
                    mode="packet")
            except (OSError, WorkflowError) as e:
                print(f"review ingest: packet request binding unavailable ({e}); projection will "
                      "remain unknown", file=sys.stderr)
    print(f"ingested {len(body)} bytes verbatim → {dest} (consumed {src})")
    print(f"  {len(findings)} finding(s) parsed — verify each before registering")
    # The ingest observation is the replayable reset signal for review-skipped-closes-v1. Like the
    # warning itself it is advisory evidence and can never change the completed ingest's exit code.
    try:
        import overlay
        overlay.record_review_ingest(root, round_id, reviewer=reviewer)
    except Exception as e:  # noqa: BLE001
        print(f"review ingest: overlay ingest observation unavailable ({e}) — ingest still succeeded",
              file=sys.stderr)
    # M2 §6: evaluate overlay warns at the review-ingest boundary (best-effort; never blocks).
    try:
        import overlay
        overlay.evaluate_boundary(root, "review-ingest", {"round_id": round_id})
    except Exception as e:  # noqa: BLE001
        print(f"review ingest: overlay warning unavailable ({e}) — ingest still succeeded",
              file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("freeze", "status", "ingest"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    root = _root(rest)
    if root is None:
        print("review: no initialized project (missing .waystone.yml)", file=sys.stderr)
        return 1
    try:
        with hold_lock(project_lock_path(root)):
            migrate_project_state(root)
    except (WorkflowError, OSError) as e:
        print(f"waystone review: migration failed: {e}", file=sys.stderr)
        return 1
    try:
        if sub == "ingest":
            with hold_lock(project_lock_path(root)):
                return ingest(root, _opt(rest, "--round"), reviewer=_opt(rest, "--reviewer"),
                              force="--force" in rest)
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
