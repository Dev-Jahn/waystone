---
name: delegate
description: Use when the user runs "/waystone:delegate" in Claude Code or "$waystone:delegate" in Codex, asks to delegate an implementation task, wants to inspect or independently verify a delegation result, or needs to apply or discard a reviewable delegated patch.
---

# waystone: delegate

Run one task in an isolated worktree, preserve provenance labels, and leave acceptance to the user.
Require an initialized project.

## Host contract

- Claude Code: invoke `/waystone:delegate`; assign `$CLAUDE_PLUGIN_ROOT` to
  `WAYSTONE_PLUGIN_ROOT`, then run command examples with `waystone` from `PATH`.
- Codex: invoke `$waystone:delegate`; from this skill's directory walk up two parents, assign that
  absolute path to `WAYSTONE_PLUGIN_ROOT`, then run command examples with
  `$WAYSTONE_PLUGIN_ROOT/bin/waystone-codex`.
- Resolve plugin resources from `$WAYSTONE_PLUGIN_ROOT`. Ask required choices through the host's
  native user-interaction mechanism (`AskUserQuestion` on Claude Code, or Codex's native mechanism);
  never require one named tool across both hosts.

## Step 1 — Select a delegable task

Use the task ID from the argument. Otherwise present the injected next-actionable tasks (or run
`waystone task list .`) and ask the user to select one.

Do not invent acceptance criteria. `waystone delegate run` refuses a task without an `accept:` YAML list
or explicit `--accept`. If criteria are missing, explain that the user must add them to the task in
`tasks.yaml`; do not synthesize a bar merely to make the command pass.

## Step 2 — Run the delegation

```bash
waystone delegate run <task-id> --root <project-root>
```

Relay the stdout summary: immutable base, dirty-snapshot flag, worktree, env prep, runner binding,
and artifact path. Relay every `waystone warn` line from stderr unchanged; never hide a warning.

## Step 3 — Summarize the contract without judging it

Read the bounded contract surface only:

```bash
waystone delegate show <delegation-id> --report --root <project-root>
```

Present harness-computed changed files/base/result as explicit evidence. Keep verification,
limitations, risks, and escalations labeled **delegate-claimed**.

HARD rules:

- Never promote delegate-claimed content to fact.
- Never generate a pass/fail or acceptance verdict. The contract intentionally has none.
- Never treat a missing delegate report as proof that verification did not happen; it is a reporting
  absence.

## Step 4 — Offer independent verification

Use the single project profile at `{project_root}/.waystone/profile.yml`; run
`waystone paths --root <project-root>` to see the resolved path. If it has a verifier binding, ask once through
the host-native interaction mechanism whether to run it. The verifier binding's `execution` key
should normally be absent so Waystone derives the transport from the current host. If accepted:

```bash
waystone delegate verify <delegation-id> --root <project-root>
waystone delegate show <delegation-id> --verify --root <project-root>
```

Summarize the latest payload as **independent-verifier** evidence. Preserve that label and do not
turn the payload into an acceptance verdict. Verification leaves the state `needs-review`.

## Step 5 — Ask the user to accept or discard

Ask once through the host-native interaction mechanism for the user's decision: `apply` or
`discard`. Do not choose on their behalf. Run exactly the selected command and report the result:

```bash
waystone delegate apply <delegation-id> --root <project-root>
waystone delegate discard <delegation-id> --root <project-root>
```

Again relay any stderr warning unchanged.

## Context discipline

Use command stdout plus `contract.yaml` and the latest `verify-<n>.json` through the `show` surfaces.
Do not read `runner.jsonl` or traverse the preserved worktree wholesale. Inspect a specific file only
when the user asks or a bounded evidence item requires it.
