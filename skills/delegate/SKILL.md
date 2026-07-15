---
name: delegate
description: Use when the user runs "/waystone:delegate" in Claude Code or "$waystone:delegate" in Codex, asks to delegate an implementation task, wants to inspect or independently verify a delegation result, or needs to apply or discard a reviewable delegated patch.
---

# waystone: delegate

Run one task in an isolated worktree and autonomously record acceptance or rejection with evidence.
Require an initialized project.

## Host contract

- Claude Code: invoke `/waystone:delegate`; assign `$CLAUDE_PLUGIN_ROOT` to
  `WAYSTONE_PLUGIN_ROOT`, then run command examples with `waystone` from `PATH`.
- Codex: invoke `$waystone:delegate`; from this skill's directory walk up two parents, assign that
  absolute path to `WAYSTONE_PLUGIN_ROOT`, then run command examples with
  `$WAYSTONE_PLUGIN_ROOT/bin/waystone-codex`.
- Resolve plugin resources from `$WAYSTONE_PLUGIN_ROOT`. In the steps below, `waystone` means the
  host-appropriate front door above. Use the host's native user-interaction mechanism only for an
  escalation listed in the exhaustive table below; never require one named tool across both hosts.

## Step 1 — Select a delegable task autonomously

Use the task ID from the argument. Otherwise select a task directly from the injected
next-actionable tasks. If that surface is absent, run `waystone task list .` and choose the first task
whose dependencies and project priority make it actionable. Do not prompt for a routine selection.
The harness remains authoritative for registry and state checks: only `pending` or `active` tasks are
delegable. Do not weaken or work around those checks.

Treat SessionStart's `needs-review delegations N` line as work to resume and complete through a
verdict, not as something to ask the user about.

## Step 1.5 — Ensure recorded acceptance criteria

Use existing criteria unchanged. If none exist, synthesize a criterion only when its exact bar is
directly derivable from owner-authored material: the task description, ROADMAP, SSOT, or relevant
review history. Inspect only the bounded material relevant to this task. Persist every synthesized
criterion before delegation, one exact string at a time:

```bash
waystone task set <task-id> --accept-add "<criterion>"
```

Keep a session-local list of the criteria added by the agent so the final report identifies them.
Never invent a generic bar merely to make delegation run. If the owner material does not determine a
real acceptance criterion, use escalation 1. Do not use the ad-hoc `delegate run --accept` path for an
agent-synthesized criterion; the durable `--accept-add` path is the audit trail.

## Step 2 — Run, triage, and retry

```bash
waystone delegate run <task-id> --root <project-root>
```

Retain the command's bounded stdout as operational evidence. Treat every `waystone warn` stderr line
as an input to the decision and preserve the original line in the verdict's `warnings_seen`; warnings
do not by themselves change the command result. The final report summarizes only their plain meaning.

If the run fails, identify its delegation ID from the bounded run/status output and inspect only:

```bash
waystone delegate show <delegation-id> --failure --root <project-root>
```

Classify the failure from that status error and stderr tail. For a transient or environmental
failure, record the conclusion before destroying the worktree, then retry as a new record:

```bash
waystone delegate discard <delegation-id> --reason "<diagnostic conclusion>" --root <project-root>
waystone delegate run <task-id> --note "<why this retry is justified>" --root <project-root>
```

Allow at most two total run attempts for one task in one main-session. Every retry must use a new
record and `--note`; never reuse or rewrite the failed record. Preserve a deterministic runner failure
without discarding it and use escalation 7. Do not read broader runner output to improve the odds of a
retry.

## Step 3 — Summarize the contract without treating claims as facts

Read the bounded contract surface only:

```bash
waystone delegate show <delegation-id> --report --root <project-root>
```

The contract's base, result, patch, and changed-file list are **harness-computed** evidence. The
worker's verification, limitations, risks, and escalations remain **delegate-claimed**.

HARD rules:

- Never promote delegate-claimed content to fact.
- Never insert a verdict into the contract or infer that the contract itself is an acceptance
  verdict. The later verdict is a separate `main-session` artifact.
- Never treat a missing delegate report as proof that verification did not happen; it is a reporting
  absence.

## Step 4 — Produce verification evidence

Use `waystone paths --root <project-root>` to resolve the single project profile at
`{project_root}/.waystone/profile.yml`. A verifier binding should normally omit `execution` so
Waystone derives its transport from the current host.

When a verifier binding exists, always run it:

```bash
waystone delegate verify <delegation-id> --root <project-root>
waystone delegate show <delegation-id> --verify --root <project-root>
```

Preserve the latest payload as **independent-verifier** evidence. Verification is read-only, records
`verify-N.json`, and leaves the delegation `needs-review`; it does not decide acceptance. If the
verifier transport fails, retry it once. If the transport still fails, use escalation 5.

When no verifier binding exists, use the contract to normalize the preserved worktree to its exact
base plus `changes.patch` when the patch is non-empty. Mirror the harness normalization exactly:

```bash
git -C <worktree> checkout --force --detach <contract-base-sha>
git -C <worktree> clean -fd
git -C <worktree> apply <record-path>/artifact/changes.patch  # non-empty patch only
```

Then run only focused test, lint, build, or inspection commands that are traceable to a criterion.
Record each exact command, integer exit code, and concise observed result in the verdict's
`agent_checks[]`. Do not replace command evidence with a claim that checks probably passed.

## Step 5 — Judge, record, and resolve

Create a temporary `verdict.json` matching the public input contract in
`templates/verdict-input-schema.json`, then pass it to the harness. The harness enriches the accepted
input into the stored artifact contract in `templates/verdict-schema.json`. The
criteria array must reproduce every packet acceptance string exactly, with no additions, omissions,
or rewriting. For each criterion, record `met`, cite concrete evidence, and include a rationale and
honest limitations. Copy every captured warning line into `warnings_seen`. Use
`decided_by: main-session` unless a user decision after an escalation is being recorded.

```json
{
  "schema": "waystone-verdict-1",
  "decision": "apply",
  "decided_by": "main-session",
  "criteria": [{"criterion": "<packet text>", "met": true, "evidence": ["<citation>"]}],
  "agent_checks": [{"cmd": "<exact command>", "exit": 0, "summary": "<observed result>"}],
  "warnings_seen": [],
  "rationale": "<why the evidence supports the decision>",
  "limitations": []
}
```

```bash
waystone delegate verdict <delegation-id> --file <verdict.json> --root <project-root>
```

An apply verdict normally requires every criterion to be met and the latest verifier evidence to
have no unresolved blocker. A `main-session` blocker override is allowed only when `agent_checks`
specifically refute each blocker finding: add one `overrides[]` item per finding with its
`finding_index` and the supporting `refuted_by` agent-check indices, then run:

```bash
waystone delegate verdict <delegation-id> --file <verdict.json> --override-blocker \
  --reason "<why the cited checks refute the finding>" --root <project-root>
```

Without that concrete refutation, an unresolved blocker plus an apply judgment is escalation 3. The
recorded `--override-unmet --reason` escape hatch is only for a deliberate, evidence-explained waiver
of an unmet criterion. `apply --override-no-verdict --reason` is an emergency CLI path and is never
part of this autonomous loop; the ordinary loop always records a verdict before resolution.

Run the command selected by the recorded decision:

```bash
waystone delegate apply <delegation-id> --root <project-root>
waystone delegate discard <delegation-id> --reason "<verdict conclusion>" --root <project-root>
```

Capture any warnings from verdict and resolution for the report. If apply reports drift, never use a
3-way apply or auto-stash fallback. Retry only after safely removing changes made entirely by this
main-session. If the drift is not completely explained by this session, use escalation 6.

## Step 6 — REPORT, the only routine user-facing surface

After resolution, give one plain-language report and omit internal gate, schema, and provenance
jargon. Include:

- what was done and whether the change was accepted or rejected;
- one line per criterion with the decision and a concrete evidence citation;
- independent verification finding counts by severity;
- which criteria, if any, the agent wrote from owner material;
- each captured warning's plain-language meaning, without raw delta IDs or raw internal lines;
- the run-attempt and retry history;
- exactly one final record pointer.

For a non-empty patch, end with `전체 기록·되돌리기: <record path>`. For an empty patch, omit the
undo wording and end with `전체 기록: <record path>`. Do not print a raw reverse-apply command or the
record path anywhere else in the report.

## Escalation table — exhaustive

| # | Escalate only when |
|---|---|
| 1 | Acceptance criteria cannot be derived from owner-authored task, ROADMAP, SSOT, or review material without inventing a hollow bar. |
| 2 | The project profile is missing, a binding is invalid, or the selected backend is not supported by the Codex-backed runner. |
| 3 | An unresolved blocker remains, the judgment favors apply, and no `agent_checks` evidence concretely refutes that finding. |
| 4 | Two run attempts for the same task have been consumed in this main-session. |
| 5 | The verifier transport still fails after one retry. |
| 6 | Apply drift is not completely explained by this main-session's own edits. Never commit or stash the user's uncommitted work; report it and wait. |
| 7 | The runner failure is deterministic; preserve its record and worktree evidence. |
| 8 | A `waystone warn conflict` stderr line reports an overlay conflict that requires a policy choice. |
| 9 | The user explicitly requested review of the task. |

These are the only escalation cases. Otherwise, do not ask the user; continue through verdict and
apply or discard. For an escalation, summarize the bounded evidence and use the host's native
user-interaction mechanism, then record any resulting decision through the same verdict gate.

## Residual safety rails

Keep the one-nonterminal-delegation owner lock, corrupt-record fail-safe, profile and binding
fail-loud behavior, empty-acceptance refusal, verifier read-only sandbox and record lock, atomic plain
`git apply`, permanent delegation record, warnings/exposure trail, and context discipline unchanged.
`discard --reason` records the conclusion before worktree cleanup. Never weaken a mechanical refusal
to keep the autonomous loop moving.

## Context discipline

Use command stdout plus `contract.yaml` and the latest `verify-<n>.json` through the `show` surfaces.
Do not read `runner.jsonl` or traverse the preserved worktree wholesale. Inspect a specific file only
when a particular acceptance criterion requires it. The no-verifier path may execute only the bounded
commands needed to produce `agent_checks` evidence.
