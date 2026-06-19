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
from jw_common import find_project_root, head_pushed, load_config, load_tasks  # noqa: E402
import jw_review  # noqa: E402


# ---- pure gate logic ---------------------------------------------------------
def tasks_gate_counts(data: dict) -> dict:
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict)]
    open_blockers = [t.get("id") for t in tasks
                     if t.get("severity") == "blocker" and t.get("status") not in ("done", "dropped")]
    open_decisions = [t.get("id") for t in tasks
                      if str(t.get("id", "")).startswith("decision/") and t.get("status") not in ("done", "dropped")]
    return {"open_blockers": open_blockers, "open_decisions": open_decisions}


def merge_gate(g: dict) -> tuple[bool, list[str]]:
    """Pure. `g` is a flat dict of facts; returns (ok, failures)."""
    f = []
    if not g.get("cycle_fresh"):
        f.append("review cycle is stale: head advanced past the frozen SHA — re-freeze (jw review freeze)")
    if g.get("require_ci"):
        if g.get("ci") == "failing":
            f.append("CI is failing")
        elif g.get("ci") != "passing":
            f.append(f"CI is not passing (state={g.get('ci')}) and require_ci is set")
    if not g.get("codex_fresh"):
        f.append("no fresh Codex review at the current head")
    if not g.get("findings_resolved"):
        f.append("Codex findings for the current cycle are not marked resolved")
    if not g.get("pro_result_at_head"):
        f.append("no external (GPT) review result bound to the current head")
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
    bundle = jw_review.pr_bundle(root, pr)
    if bundle is None:
        return 1
    head = bundle["head"]
    if sha not in (head, head[: len(sha)]) or len(sha) < 7:
        print(f"jw approve: refusing — --sha {sha} is not the current PR head ({head[:12]}). "
              f"Approval must bind to the exact current head.", file=sys.stderr)
        return 3
    marker = jw_review.emit_marker("approval", {"sha": head, "by": "user"})
    body = f"Approved for merge at `{head[:12]}`. A new push invalidates this automatically.\n\n{marker}\n"
    rc, out = jw_review._gh(root, "pr", "comment", str(pr), "--body", body)
    if rc != 0:
        print(f"jw approve: gh pr comment failed: {out}", file=sys.stderr)
        return 1
    print(f"approval recorded for PR #{pr} at {head[:12]}")
    return 0


def _gather(root: Path, pr: int) -> dict | None:
    cfg = load_config(root)
    facts = jw_review.pr_facts(root, pr)
    if facts is None:
        return None
    pushed, _ = head_pushed(root, fetch=False)
    counts = tasks_gate_counts(load_tasks(root))
    return {
        **facts,
        "require_ci": cfg["review"]["require_ci"],
        "remote_contains_head": None,  # PR head is remote by definition; local-clone push is informational
        **counts,
    }


def merge(root: Path, pr: int, execute: bool, method: str | None) -> int:
    cfg = load_config(root)
    if cfg["review"]["mode"] != "pr":
        print("jw merge: review.mode is 'packet'; the merge guard is for PR mode.", file=sys.stderr)
        return 1
    g = _gather(root, pr)
    if g is None:
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
    rc, out = jw_review._gh(root, "pr", "merge", str(pr), f"--{method}")
    if rc != 0:
        print(f"jw merge: gh pr merge failed: {out}", file=sys.stderr)
        return 1
    print(f"merged PR #{pr} via {method}")
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
