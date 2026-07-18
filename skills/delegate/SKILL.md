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

Resolve the profile path with `waystone paths --root <project-root>`, then inspect only the selected
workflow role's binding in `{project_root}/.waystone/profile.yml`. This skill's implementation
handoff uses the `implementer` role. Before choosing the execution, explicitly check all eight
SessionStart routing questions in policy order:

1. `reasoning` — required reasoning level;
2. `context-inheritance` — whether current context must carry over;
3. `independent-perspective` — whether isolation of perspective matters;
4. `bounded-scope` — whether the scope is clear and bounded;
5. `repetitive-tools` — whether repeatable tool execution dominates;
6. `retry-cost` — cost of a failed attempt and retry;
7. `independent-verification` — who verifies final quality;
8. `budget-sensitivity` — the user's execution-budget sensitivity.

Questions `reasoning`, `independent-perspective`, `bounded-scope`, and
`independent-verification` are the policy questions whose preferences can admit
`external-runner`; `context-inheritance`, `repetitive-tools`, `retry-cost`, and
`budget-sensitivity` name host-guided executions only. A question admitting external execution does
not override the selected profile binding. Route the task by that binding's `execution` and
`backend`:

- `implementer` + `external-runner`: continue to `waystone delegate run`; this is the only role and
  execution pair that command can start.
- `main-session`, `clean-subagent`, `forked-subagent`, or `deterministic-workflow`: do not call
  `delegate run`. Dispatch the bound role/execution/backend through the host's native main-session,
  subagent, or workflow mechanism. Preserve the role attribution and record the task as done or
  touched in `waystone round close` plus the round's PROGRESS entry. For a deterministic-workflow
  binding, follow the carrier block below.

Do not translate a host-guided binding into a headless runner. Other external-runner roles are not
an implemented `delegate run` surface; verifier execution is handled separately in Step 4.

### Deterministic-workflow carrier

`deterministic-workflow` names an orchestration procedure, not a host tool: a fixed plan
manifest (from `waystone delegate plan --json`) executed under declared ordering, concurrency,
and aggregation rules. On Claude Code its carrier is the host's native Workflow tool; this
skill instruction is the legitimate opt-in for that tool. On a host without a carrier (Codex),
this binding is not executable — do not simulate it with prose-driven sequential dispatch;
rebind the role explicitly for that host and record the route as actually bound.

HARD rules for the workflow carrier:

- Main owns decomposition, packet boundaries, and acceptance before dispatch. Produce the plan
  with `waystone delegate plan <task-id>... --json`; the workflow only carries that manifest.
- Derive every `agent()` override from the manifest's carrier bindings; never hardcode past
  them and never accept free-form model/effort arguments. Effort maps `none|minimal|low → low`,
  `medium → medium`, `high → high`, `xhigh → xhigh`; `ultra` is a Codex-CLI-only leaf effort —
  a claude-backend binding naming it fails loud, never substitutes.
- A workflow agent's success or structured output is a non-authoritative carrier report, never
  acceptance. Every implementer leg runs `waystone delegate run … --expect-packet-sha
  --expect-profile --carrier claude-workflow --carrier-instance <correlation-id> --json-events`
  and each resulting delegation still passes Steps 3–5 individually, with facts re-derived from
  its on-disk record. Workflow-only completion is valid solely for non-implementation work.
- Leaf agents start `delegate run` in background execution and await completion; the runner can
  outlive a foreground tool timeout. Tasks run in the same parallel batch only when their
  declared scopes are pairwise disjoint; overlapping or undeclared scope runs sequentially —
  there is no parallel override.
- Instantiate the canonical carrier for three or more decided tasks: read
  `${CLAUDE_PLUGIN_ROOT}/templates/hosts/claude-code/delegate-fanout.workflow.js` and pass it
  verbatim as the Workflow tool's `script` input with `args: {plan: <plan json>}`. For one or
  two tasks, direct background dispatch is simpler and loses nothing. Do not resume a fan-out
  workflow run; re-plan from disk state and invoke fresh (`--expect-packet-sha` refuses stale
  dispatch). Before a project's first fan-out, pre-allow `waystone delegate plan/run/status/show`
  in project permission settings — a background leaf cannot answer permission prompts.
- Afterwards record the route with `waystone round close --route-note
  <role>,deterministic-workflow,<backend>`; the note must equal the live binding.

For an external-runner route, record the budget-sensitivity judgment as one free, single-line
main-session note in the immutable packet:

```bash
waystone delegate run <task-id> --routing-note "<budget judgment>" --root <project-root>
```

For a host-guided route, preserve the same judgment in PROGRESS and use the round route note in
Step 2 of the round skill; no delegation packet exists on that route.

When the task's path scope is exactly derivable from owner-authored task, SSOT, or review material,
record each repo-relative prefix before either route runs:

```bash
waystone task set <task-id> --scope-add "<repo-relative-prefix>"
```

Preserve existing `scope:` entries and append only missing exact prefixes. Do not invent a broad
prefix merely to make scope-drift evaluation available.

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

If the bound external backend is `claude:<model>`, the command is structurally unsandboxed and is
refused by default. Do not add an override autonomously. Only after escalation 9 produces explicit
user consent, run the same command with both recorded fields:

```bash
waystone delegate run <task-id> --allow-unsandboxed-runner \
  --reason "<user-approved reason>" --root <project-root>
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
`{project_root}/.waystone/profile.yml`. A verifier binding should normally omit `execution` and
`entry`; Waystone owns the verification transport and prompt. Codex verification always uses
host-independent `codex exec` in a read-only sandbox.

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
| 2 | The project profile is missing, a binding is invalid, or the selected role/backend has no implemented execution surface. |
| 3 | An unresolved blocker remains, the judgment favors apply, and no `agent_checks` evidence concretely refutes that finding. |
| 4 | Two run attempts for the same task have been consumed in this main-session. |
| 5 | The verifier transport still fails after one retry. |
| 6 | Apply drift is not completely explained by this main-session's own edits. Never commit or stash the user's uncommitted work; report it and wait. |
| 7 | The runner failure is deterministic; preserve its record and worktree evidence. |
| 8 | A `waystone warn conflict` stderr line reports an overlay conflict that requires a policy choice. |
| 9 | A `claude:<model>` external-runner binding would require `--allow-unsandboxed-runner --reason`; get the user's explicit consent to that unsandboxed execution first. |
| 10 | The user explicitly requested review of the task. |

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
