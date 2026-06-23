#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Deterministic merge guard + SHA-bound human approval (PR-mode review profile).

The merge decision is computed by a pure function, never left to natural-language judgement.
A merge is blocked unless EVERY condition holds at the *current* head SHA:
  - the latest review cycle is fresh (head has not advanced past the frozen SHA)
  - CI passing (when require_ci)
  - a fresh Codex review exists at this head, and its findings are marked resolved
  - an external (GPT) review result is bound to this head
  - zero open blocker tasks and zero unresolved decision/ tasks
  - a human approval is bound to this exact head SHA

Subcommands (also `jw approve` / `jw round merge`):
  approve --pr N --sha X [root]            record a human approval bound to SHA X (must == PR head)
  merge   --pr N [--execute --squash|--rebase|--merge] [root]
                                           check the gate; with --execute + a method, perform the merge
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from jw_common import load_config  # noqa: E402
import jw_review  # noqa: E402
import jw_validate  # noqa: E402


# ---- pure gate logic ---------------------------------------------------------
def tasks_gate_counts(data: dict) -> dict:
    raw = data.get("tasks", []) if isinstance(data, dict) else []
    tasks = [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else []
    open_blockers = [t.get("id") for t in tasks
                     if t.get("severity") == "blocker" and t.get("status") not in ("done", "dropped")]
    open_decisions = [t.get("id") for t in tasks
                      if str(t.get("id", "")).startswith("decision/") and t.get("status") not in ("done", "dropped")]
    return {"open_blockers": open_blockers, "open_decisions": open_decisions}


def merge_gate(g: dict) -> tuple[bool, list[str]]:
    """Pure. `g` is a flat dict of facts; returns (ok, failures)."""
    f = []
    if not g.get("head_read_ok", True):
        f.append("could not read policy@base / tasks@head — cannot evaluate the gate safely")
        return (False, f)
    if g.get("pr_state") and g["pr_state"] != "OPEN":
        f.append(f"PR is not OPEN (state={g['pr_state']})")
    if g.get("is_draft"):
        f.append("PR is a draft")
    if g.get("expected_base") and g.get("base") and g["base"] != g["expected_base"]:
        f.append(f"PR base is {g['base']!r}, expected {g['expected_base']!r}")
    if not g.get("cycle_fresh"):
        f.append("review cycle is stale: head or base advanced past the frozen SHAs — re-freeze (jw review freeze)")
    if g.get("require_ci"):
        if g.get("ci") == "failing":
            f.append("CI is failing")
        elif g.get("ci") != "passing":
            f.append(f"CI is not passing (state={g.get('ci')}) and require_ci is set")
    if g.get("want_codex"):
        if not g.get("codex_fresh"):
            f.append("no fresh Codex review at the current head")
        if not g.get("findings_resolved"):
            f.append("Codex findings for the current cycle are not marked resolved")
    if g.get("want_pro") and not g.get("pro_result_at_head"):
        f.append("no external (macro) review result bound to the current head")
    nb = len(g.get("open_blockers", []))
    if nb:
        f.append(f"{nb} open blocker task(s): {', '.join(g['open_blockers'])}")
    nd = len(g.get("open_decisions", []))
    if nd:
        f.append(f"{nd} unresolved decision task(s): {', '.join(g['open_decisions'])}")
    if not g.get("approved_at_head"):
        f.append("no human approval bound to the current head (jw approve --pr N --sha <head>)")
    if g.get("remote_contains_head") is False:
        f.append("local HEAD is not pushed to its upstream")
    return (not f, f)


# ---- CLI ---------------------------------------------------------------------
def approve(root: Path, pr: int, sha: str) -> int:
    ctx = jw_review.pr_context(root, pr)
    if ctx is None:
        return 1
    if ctx["policy"] is None:
        print("jw approve: cannot read the base-branch policy at the PR base SHA.", file=sys.stderr)
        return 1
    repo, bundle = ctx["repo"], ctx["bundle"]
    head = bundle["head"]
    base_sha = bundle.get("base_sha", "")
    if len(sha) < 7 or not head.startswith(sha):
        print(f"jw approve: refusing — --sha {sha} is not the current PR head ({head[:12]}). "
              f"Approval must bind to the exact current head.", file=sys.stderr)
        return 3
    # bind the approval to the current cycle, so a later re-freeze (new cycle/base) invalidates it.
    # operators come from the BASE policy (consistent with the gate), not the local checkout.
    owner = repo.split("/", 1)[0] if repo else ""
    operators = tuple({owner, *ctx["policy"]["review"].get("operators", [])} - {""})
    lc = jw_review.latest_cycle(jw_review.parse_bodies(bundle["bodies"]), operators)
    if lc is None:
        print("jw approve: no review cycle is frozen yet — run `jw review freeze` first.", file=sys.stderr)
        return 1
    rc, login = jw_review._gh(root, "api", "user", "-q", ".login")
    if rc != 0 or not login:
        print("jw approve: could not resolve your GitHub login (gh api user) — an approval must "
              "bind to a real actor whose identity matches who posts it.", file=sys.stderr)
        return 1
    by = login
    marker = jw_review.emit_marker("approval",
                                   {"sha": head, "base_sha": base_sha, "cycle": lc["cycle"], "by": by})
    body = (f"Approved for merge at `{head[:12]}` (cycle {lc['cycle']}, base `{base_sha[:12]}`). "
            f"A new push, a base advance, or a re-freeze invalidates this automatically.\n\n{marker}\n")
    rc, out = jw_review._gh(root, "pr", "comment", str(pr), "--body", body)
    if rc != 0:
        print(f"jw approve: gh pr comment failed: {out}", file=sys.stderr)
        return 1
    print(f"approval recorded for PR #{pr} at {head[:12]} (cycle {lc['cycle']}) by {by}")
    return 0


def _gather(root: Path, pr: int) -> dict | None:
    """Build gate facts. The trust POLICY (reviewers/operators/approvers/require_ci) is read from
    the PR's BASE SHA — the protected target branch — NOT the PR head, so a candidate branch
    cannot relax its own merge rules (add itself as operator/approver, drop reviewers, disable
    CI). The CONTENT being merged (tasks.yaml blockers/decisions) is read from the head."""
    ctx = jw_review.pr_context(root, pr)
    if ctx is None:
        return None
    repo, bundle = ctx["repo"], ctx["bundle"]
    head, base_sha = ctx["head"], ctx["base_sha"]
    policy = ctx["policy"]              # POLICY @ base SHA (None if unreadable)
    read_ok = policy is not None
    data = {}
    if read_ok and repo and head:
        tasks_text = jw_review.file_at_ref(root, repo, "tasks.yaml", head)  # CONTENT @ head
        if tasks_text is None:
            read_ok = False
        else:
            try:
                data = yaml.safe_load(tasks_text) or {}
            except yaml.YAMLError:
                read_ok = False
            else:
                # head tasks.yaml must be schema-valid, not merely parseable — a malformed
                # registry must not let the gate read zero blockers/decisions from garbage.
                if jw_validate.validate(data):
                    read_ok = False
    else:
        read_ok = False
    if policy is None:
        policy = load_config(root)  # for facts only; read_ok is already False → gate blocks
    facts = jw_review.facts_from_bundle(bundle, policy, repo)
    reviewers = policy["review"]["reviewers"]
    # any configured reviewer other than codex is a mandatory macro reviewer — never guess by name
    # (a reviewer like 'research-auditor' must not be silently dropped from the gate).
    macro = [r for r in reviewers if r != "codex"]
    return {
        **facts,
        "head_read_ok": read_ok,
        "policy_mode": policy["review"]["mode"],
        "require_ci": policy["review"]["require_ci"],
        "want_codex": "codex" in reviewers,
        "want_pro": bool(macro),
        "remote_contains_head": None,
        "expected_base": jw_review._gh(root, "repo", "view", "--json", "defaultBranchRef",
                                       "-q", ".defaultBranchRef.name")[1] or None,
        **tasks_gate_counts(data),
    }


def merge(root: Path, pr: int, execute: bool, method: str | None) -> int:
    g = _gather(root, pr)
    if g is None:
        return 1
    # the merge guard applies only under a pr-mode BASE policy (not the local checkout) — a branch
    # can't switch a packet-policy repo into pr mode to wave itself through an empty reviewer set.
    if g.get("policy_mode") != "pr":
        print("jw merge: the base-branch review.mode is not 'pr'; the merge guard is for PR mode.",
              file=sys.stderr)
        return 1
    ok, failures = merge_gate(g)
    if not ok:
        print(f"MERGE BLOCKED for PR #{pr} at {g['current_head'][:12]} — {len(failures)} unmet condition(s):",
              file=sys.stderr)
        for x in failures:
            print(f"  ✗ {x}", file=sys.stderr)
        return 3
    print(f"MERGE GATE PASS for PR #{pr} at {g['current_head'][:12]} — all conditions met.")
    if not execute:
        print("  (dry run — re-run with --execute --squash|--rebase|--merge to perform the merge)")
        return 0
    if method not in ("squash", "rebase", "merge"):
        print("jw merge --execute requires a method: --squash | --rebase | --merge", file=sys.stderr)
        return 1
    # bind the merge to the exact validated head — a push between gate and merge aborts it
    rc, out = jw_review._gh(root, "pr", "merge", str(pr), f"--{method}",
                            "--match-head-commit", g["current_head"])
    if rc != 0:
        print(f"jw merge: gh pr merge failed (head may have moved since the gate): {out}", file=sys.stderr)
        return 1
    print(f"merged PR #{pr} via {method} at {g['current_head'][:12]}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("approve", "merge"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    root = jw_review._root(rest)
    if root is None:
        print("jw_merge: no initialized project (missing .jahns-workflow.yml)", file=sys.stderr)
        return 1
    pr_s = jw_review._opt(rest, "--pr")
    if not pr_s:
        print(f"jw {sub}: --pr N is required", file=sys.stderr)
        return 1
    if sub == "approve":
        sha = jw_review._opt(rest, "--sha")
        if not sha:
            print("jw approve: --sha X is required", file=sys.stderr)
            return 1
        return approve(root, int(pr_s), sha)
    method = next((m[2:] for m in ("--squash", "--rebase", "--merge") if m in rest), None)
    return merge(root, int(pr_s), "--execute" in rest, method)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
