#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""SHA-bound review cycles for the PR-mode review profile.

A review means "reviewer R examined tree SHA X". That fact is stored as machine-readable
markers in PR comments (GitHub is the canonical event store), never inferred from filenames.
Identity of a review is (reviewer, review_cycle, reviewed_sha) — so a re-review after
remediation (cycle 2 at SHA B) is distinct from cycle 1 at SHA A, and a new push makes prior
cycles stale.

Markers (HTML comments embedded in PR comment bodies):
  jw-review-cycle  : a freeze — {round_id, cycle, target_sha, reviewers}
  jw-review-result : an external reviewer reply footer — {reviewer, review_cycle, reviewed_sha, verdict, decision_required}
  jw-findings      : adjudication outcome for a cycle — {cycle, resolved}
  jw-approval      : SHA-bound human approval — {sha, by}

Subcommands (also `jw review <sub>`):
  freeze --pr N [--round ID] [root]   stamp the current PR head as a new review cycle + post request
  status [--pr N] [root]              show per-cycle review status (PR mode) or packet pairs (packet mode)

The marker emit/parse and status classification are pure functions (tested); gh I/O is isolated.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from jw_common import find_project_root, git_full_sha, load_config  # noqa: E402

CODEX_BOT = "chatgpt-codex-connector[bot]"
MARKER_RE = re.compile(r"<!--\s*jw-([a-z-]+):v1\s*\n(.*?)\n\s*-->", re.DOTALL)


# ---- pure marker logic -------------------------------------------------------
def emit_marker(kind: str, fields: dict) -> str:
    lines = [f"<!-- jw-{kind}:v1"]
    for k, v in fields.items():
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    lines.append("-->")
    return "\n".join(lines)


def parse_markers(text: str, kind: str | None = None) -> list[dict]:
    """Extract all jw-*:v1 markers from a blob (or list of blobs joined). Returns dicts with
    an added '_kind' key; filter by `kind` if given."""
    out = []
    for m in MARKER_RE.finditer(text or ""):
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


def latest_cycle(markers: list[dict]) -> dict | None:
    cycles = [m for m in markers if m.get("_kind") == "review-cycle" and isinstance(m.get("cycle"), int)]
    return max(cycles, key=lambda m: m["cycle"]) if cycles else None


def next_cycle_number(markers: list[dict]) -> int:
    lc = latest_cycle(markers)
    return (lc["cycle"] + 1) if lc else 1


def classify(markers: list[dict], current_head: str) -> dict:
    """Pure classification of PR review state vs the current head SHA."""
    lc = latest_cycle(markers)
    results = [m for m in markers if m.get("_kind") == "review-result"]
    approvals = [m for m in markers if m.get("_kind") == "approval"]
    findings = [m for m in markers if m.get("_kind") == "findings"]
    head_matches = bool(lc) and str(lc.get("target_sha")) == current_head
    pro_at_head = any(str(r.get("reviewed_sha")) == current_head for r in results)
    approved_at_head = any(str(a.get("sha")) == current_head for a in approvals)
    cyc = lc["cycle"] if lc else None
    findings_resolved = any(f.get("cycle") == cyc and bool(f.get("resolved")) for f in findings)
    return {
        "current_head": current_head,
        "latest_cycle": cyc,
        "frozen_sha": (lc or {}).get("target_sha"),
        "cycle_fresh": head_matches,         # no push since freeze
        "pro_result_at_head": pro_at_head,
        "approved_at_head": approved_at_head,
        "findings_resolved": findings_resolved,
        "n_results": len(results),
        "n_approvals": len(approvals),
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


def pr_bundle(root: Path, pr: int) -> dict | None:
    """Fetch the PR's head SHA, comment bodies, and reviews via gh; return a normalized bundle."""
    rc, out = _gh(root, "pr", "view", str(pr), "--json",
                  "headRefOid,comments,reviews,statusCheckRollup,mergeStateStatus")
    if rc != 0:
        print(f"jw_review: gh pr view {pr} failed: {out}", file=sys.stderr)
        return None
    pr_json = json.loads(out)
    bodies = []
    for c in pr_json.get("comments", []):
        bodies.append({"body": c.get("body", ""), "author": (c.get("author") or {}).get("login", ""),
                       "at": c.get("createdAt", "")})
    for r in pr_json.get("reviews", []):
        bodies.append({"body": r.get("body", ""), "author": (r.get("author") or {}).get("login", ""),
                       "at": r.get("submittedAt", ""), "state": r.get("state", "")})
    checks = pr_json.get("statusCheckRollup", []) or []
    return {
        "head": pr_json.get("headRefOid", ""),
        "bodies": bodies,
        "all_marker_text": "\n".join(b["body"] for b in bodies),
        "checks": checks,
        "merge_state": pr_json.get("mergeStateStatus", ""),
    }


def codex_fresh(bundle: dict, since_at: str | None) -> bool:
    """Codex bot activity at-or-after the latest freeze comment timestamp."""
    for b in bundle["bodies"]:
        if b["author"] == CODEX_BOT and (since_at is None or (b.get("at") or "") >= since_at):
            return True
    return False


def ci_state(bundle: dict) -> str:
    checks = bundle.get("checks", [])
    if not checks:
        return "none"
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for s in states):
        return "failing"
    if any(s in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED") for s in states):
        return "pending"
    return "passing"


def pr_facts(root: Path, pr: int) -> dict | None:
    """Everything the merge gate and status need about a PR, normalized."""
    bundle = pr_bundle(root, pr)
    if bundle is None:
        return None
    markers = parse_markers(bundle["all_marker_text"])
    cls = classify(markers, bundle["head"])
    lc = latest_cycle(markers)
    freeze_at = None
    if lc:
        for b in bundle["bodies"]:
            if f"cycle: {lc['cycle']}" in b["body"] and "jw-review-cycle:v1" in b["body"]:
                freeze_at = b.get("at")
                break
    cls["codex_fresh"] = codex_fresh(bundle, freeze_at)
    cls["ci"] = ci_state(bundle)
    cls["merge_state"] = bundle.get("merge_state", "")
    return cls


# ---- CLI ---------------------------------------------------------------------
def _opt(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i < len(argv) - 1:
            return argv[i + 1]
    return None


def _root(argv: list[str]) -> Path | None:
    positional = [a for a in argv if not a.startswith("--")
                  and argv[argv.index(a) - 1] not in ("--pr", "--round")]
    if positional:
        return Path(positional[-1]).resolve()
    return find_project_root(Path.cwd())


def freeze(root: Path, pr: int, round_id: str | None) -> int:
    cfg = load_config(root)
    if cfg["review"]["mode"] != "pr":
        print("jw_review freeze: review.mode is 'packet'; freeze is for PR mode. "
              "Set review.mode: pr in .jahns-workflow.yml.", file=sys.stderr)
        return 1
    bundle = pr_bundle(root, pr)
    if bundle is None:
        return 1
    head = bundle["head"] or git_full_sha(root, "HEAD")
    markers = parse_markers(bundle["all_marker_text"])
    n = next_cycle_number(markers)
    reviewers = cfg["review"]["reviewers"]
    marker = emit_marker("review-cycle", {
        "round_id": round_id or "(unset)", "cycle": n, "target_sha": head, "reviewers": reviewers,
    })
    body = (f"## Review cycle {n} — frozen at `{head[:12]}`\n\n"
            f"This is the immutable review target for cycle {n}. A new push makes this cycle stale.\n\n"
            + ("@codex review\n\n" if "codex" in reviewers else "")
            + ("External macro reviewer: review this PR at the SHA above; end your reply with a "
               "`jw-review-result` footer carrying `reviewed_sha: " + head + "`.\n\n"
               if any("gpt" in r or "pro" in r for r in reviewers) else "")
            + marker + "\n")
    rc, out = _gh(root, "pr", "comment", str(pr), "--body", body)
    if rc != 0:
        print(f"jw_review freeze: gh pr comment failed: {out}", file=sys.stderr)
        return 1
    print(f"review cycle {n} frozen at {head[:12]} on PR #{pr} (reviewers: {', '.join(reviewers)})")
    return 0


def status(root: Path, pr: int | None) -> int:
    cfg = load_config(root)
    if pr is not None:
        facts = pr_facts(root, pr)
        if facts is None:
            return 1
        print(f"PR #{pr} review status:")
        print(f"  current head:   {facts['current_head'][:12]}")
        print(f"  latest cycle:   {facts['latest_cycle']} (frozen {str(facts['frozen_sha'])[:12]})")
        print(f"  cycle fresh:    {facts['cycle_fresh']}  (False = a push happened after freeze → re-freeze)")
        print(f"  codex fresh:    {facts['codex_fresh']}")
        print(f"  CI:             {facts['ci']}")
        print(f"  pro result@head:{facts['pro_result_at_head']}  ({facts['n_results']} result(s) total)")
        print(f"  findings resolved: {facts['findings_resolved']}")
        print(f"  approved@head:  {facts['approved_at_head']}  ({facts['n_approvals']} approval(s) total)")
        return 0
    # packet mode: derive request/feedback pairs from reviews_dir
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


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("freeze", "status"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    root = _root(rest)
    if root is None:
        print("jw_review: no initialized project (missing .jahns-workflow.yml)", file=sys.stderr)
        return 1
    pr_s = _opt(rest, "--pr")
    if sub == "freeze":
        if not pr_s:
            print("jw_review freeze: --pr N is required", file=sys.stderr)
            return 1
        return freeze(root, int(pr_s), _opt(rest, "--round"))
    return status(root, int(pr_s) if pr_s else None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
