# Deterministic-workflow carrier — implementation notes

Design source: `/Users/jahn/.claude/plans/waystone-plugin-harness-delegate-atomic-sunbeam.md`
(external review verdict: "Architecture approved, implementation changes requested").
Binding decisions: `docs/adr/ADR-0001-deterministic-workflow-carrier.md`.

## Retry / failure protocol (plan §6, end)

- Retry = main runs `discard --reason`, then **re-plans and starts a fresh invocation**
  (pass task-specific context through `--note`). `resumeFromRunId` is never used. Main
  enforces a per-task cap of at most 2 attempts.
- If the workflow session is killed mid-run, the detached `delegate run` process still runs
  to completion and converges on disk; SessionStart's "needs-review delegations N" line is
  the recovery entry point. Crash remnants / orphaned records are cleared with
  `discard --reason` or `discard --orphan`.

## Revision history (plan §0) — what was withdrawn vs. adopted

| First draft | Final (this design) |
|---|---|
| "Zero CLI changes possible" | **Withdrawn** — `delegate plan --json` / `run --json-events --expect-packet-sha` / `status --json` / dep gate / carrier attribution fields are all new (Major 3·4). |
| Parse the `worktree:` line from stdout | **Withdrawn** — breaks on snapshot/ref failure (blank output), slug collisions, corrupt rows. Replaced by NDJSON events. |
| `allowScopeOverlap` parallel override | **Removed** — overlapping/undeclared scope is always forced sequential; override only chooses serialization order, never forces parallelism (Major 5). |
| Workflow run ID in the routing-note | **Withdrawn** — unknowable before execution. Replaced by a main/CLI-generated correlation ID recorded as `carrier`/`carrier_instance_id`, joined to the real workflow run ID later via cclog (Major 1). |
| Free-form `leafModel`/`leafEffort` arguments | **Removed** — the template only consumes the resolved carrier manifest (profile-bound), never free arguments (Major 2). |
| `node --check` as the syntax gate | **Demoted to optional lint** — the real compile gate is a `validateOnly` call through the actual Workflow engine, because the dialect (top-level `return`, global `agent`/`parallel`/`phase`/`log`) is not standard ES module and cannot run under `node` (Major 6). |
| Allow `resumeFromRunId` to recover an interrupted run | **Forbidden in v1** — external state isn't a cache key; `--expect-packet-sha` mechanically blocks stale dispatch, but resumption is also disallowed as policy (Major 7). |
| Codex fallback = "record sequential host dispatch as deterministic-workflow" | **Withdrawn** — on a host with no carrier, that binding is unusable (fail-loud); the role must be explicitly rebound instead. Prevents provenance overstatement (Major 1). |
| Effort mapping `xhigh → max` (leftover from an earlier draft) | **Fixed to `xhigh → xhigh`** — the Workflow `agent()` effort enum genuinely includes `xhigh` (confirmed against the harness tool definition); the reviewer's `xhigh → max` suggestion mistook the enum. `max` corresponds to no profile binding value (main's own analysis only). |

## Pointers

- Full design: see the plan file path above, §0 (revision history), §2 (three-axis
  architecture table), §3 (ADR decision items), §4 (CLI contract), §5 (skill/conventions/
  contract text), §6 (workflow template + this retry protocol), §7 (verification matrix).
- Binding record: `docs/adr/ADR-0001-deterministic-workflow-carrier.md`.
- Carrier template: `templates/hosts/claude-code/delegate-fanout.workflow.js`.
