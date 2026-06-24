# jahns-workflow

A Claude Code plugin that harnesses SSOT-anchored agentic development: one global naming
convention across all projects, a machine-validated task registry, zero-token roadmap/digest
generation, round-based progress discipline, external-review ingestion, and scoped SSOT audits.

Generalizes a research-workflow battle-tested on theory-heavy repos, but applies to any
programming project (web, systems, ML) that anchors work on one design document.

## What it enforces (from adoption onward — never retroactively)

- **Task IDs** `<type>/<kebab-slug>` (`feat|fix|perf|gate|spike|decision|docs|chore`) registered in `tasks.yaml` with explanatory titles before first use. Bare codenames (`P0`, `E3`, `Q1`) are rejected by a validator hook.
- **Severities** `blocker > major > minor` on review/audit findings.
- **One home per fact**: SSOT (design), `tasks.yaml` (registry), `ROADMAP.md` (generated view), `PROGRESS.md` (log, auto-archived monthly), ADRs (decisions), reviews dir (external feedback, verbatim).
- **SSOT discipline**: §-anchor citations only; generated section split + INDEX + session-injected DIGEST; "binding but falsifiable" discrepancy rule; bulk-edit quarantine (>100 changed lines → audit before dependent work).

Full convention: [references/conventions.md](references/conventions.md).

## Components

| | |
|---|---|
| `/jahns-workflow:init` | One-click setup; non-destructive retrofit for in-progress projects (incl. agent-memory cleanup, project registration) |
| `/jahns-workflow:round` | Close a work round: sync registry → PROGRESS entry + archive → refresh views → review-request packet |
| `/jahns-workflow:review` | Ingest an external review reply: preserve verbatim → verify each finding → register as tasks |
| `/jahns-workflow:audit` | Scoped, tiered SSOT audit (consistency / independent re-derivation / cheap-oracle checks). Cost cap is wall-clock, not hardware — never runs production suites or expensive builds |
| `/jahns-workflow:status` | Cross-project terminal dashboard (branches, rounds, active/blocked tasks). Registry entries can be local (`path`) or remote (`repo: owner/name`, fetched via `gh api` — for projects not cloned on this machine) |
| `spec-auditor` agent | Independent verifier fanned out by the audit skill |
| SessionStart hook | Injects digest + active tasks on startup/resume/clear/**compact** (capped ~8KB; no-ops in ~30ms in non-initialized projects) |
| PostToolUse hook | On `tasks.yaml` edits only: schema validation (exit-2 feedback) + deterministic `ROADMAP.md` regeneration. ~13ms no-op otherwise |

All rendering/validation is plain Python (`scripts/jw_*.py`, run via `uv`) — zero LLM tokens.
A unified front door `scripts/jw.py` dispatches `jw <group>`
(validate/roadmap/ssot/status/remote/review/approve/round/lanes/resume).

## Round closeout & resume (v0.2.1)

- **`jw round close . --round <id> --done <ids> --touched <ids>`** does the whole deterministic
  closeout ritual atomically: comment-preserving status/round flips on tasks.yaml, validate,
  regenerate ROADMAP/SSOT views, advance the `last_round_commit` watermark, and report SSOT churn
  (flagging the >100-line bulk-edit quarantine). No more hand-edited watermarks or per-task flips.
- **PreCompact / SessionEnd hooks** snapshot a re-entry pointer (HEAD, branch, active round,
  active/blocked tasks, next-actionable) to a plugin-local file — closing the "update memory
  before compaction" loop. The SessionStart injection now also lists **next-actionable** tasks
  (deps satisfied, including stale-`blocked` ones whose deps are now done).
- **`jw lanes verify .`** checks each task's `lane:` manifest — that the lane branch *contains*
  its recorded `base_sha` (the correct invariant; not descent from the moving integration tip).
  For parallel worktree lanes, set `worktree.baseRef: "head"` in Claude Code settings so lanes
  branch from local state, and record each lane's `base_sha` at creation.

## Review profiles (v0.2.0)

Set `review.mode` in `.jahns-workflow.yml`:

- **`packet`** (default) — close a round, push, paste a request packet to a web reviewer.
- **`pr`** — SHA-bound review cycles on a GitHub PR, with a deterministic merge gate. A review
  is identified by `(reviewer, cycle, reviewed_sha)`, stored as machine-readable markers in PR
  comments (GitHub is the canonical store, never inferred from filenames). A new push makes a
  cycle stale; a merge is blocked by `jw round merge` unless — at the *current* head — the cycle
  is fresh, CI is ok (if `require_ci`), a fresh Codex review + resolved findings + a macro-reviewer
  result are bound to the head, there are zero open blockers/decisions, and a human approval
  (`jw approve --pr N --sha <head>`) is bound to the head. The gate is computed, never judged.

Both modes share a hard push gate (`jw remote verify`): no review is requested against an
unpushed HEAD. The deterministic core has a regression suite maintained on the `dev` branch
(`uv run scripts/tests/run_tests.py`); `main` ships the plugin runtime only.

**Provenance binding (v0.2.2).** A review marker is only believed when its provenance binds: a
result must come from a configured reviewer, for the latest cycle, at the current head, with a
merge-compatible verdict and no unresolved decision; an approval must be authored by a trusted
approver (the repo owner or `review.approvers`) and bound to the head; markers quoted inside
fenced code blocks are ignored. The merge gate reads config/tasks from the **PR head SHA** (not
the local checkout), refuses non-OPEN/draft PRs, and merges with `--match-head-commit` so a push
between the gate check and the merge aborts it. CI is strict — only `SUCCESS` is passing.

**Two-axis provenance (v0.2.3).** A marker now binds on *both* the logical reviewer it claims
*and* the GitHub actor who posted it. `review.operators` (default: the repo owner) lists who may
post `cycle`/`result`/`findings` markers — so a collaborator can't forge a macro reviewer's
verdict by writing its name in a comment. An approval's `by` must equal the GitHub login that
posted it; conflicting freeze markers for the same cycle fail closed; `findings` uses the latest
trusted state (a later `resolved: false` re-blocks). **Codex** is bound to the head — by a formal
review whose `commit_id` is the head, or by the SHA the Codex bot names in its own review comment
(its normal no-issue path; the bot login is GitHub-verified, so only Codex can author it). Timing
is irrelevant once the tree is pinned; a bare 👍 reaction, which can't be SHA-bound, fail-closes.
The PR-head `tasks.yaml`
must pass schema validation, not merely parse. PR-head file reads use an explicit `GET` (a bare
`-f` flips `gh api` to POST, which the contents endpoint rejects). `jw round close` now rolls the
primary files back if any step of the commit phase raises, and lets a dependency and its
dependent close in the same round.

**Closing the trust boundary (v0.2.4).** The merge **policy** (reviewers, operators, approvers,
`require_ci`) is read from the PR's **base SHA** — the protected target branch — never the
candidate head, so a branch can't make itself an operator/approver, drop reviewers, or disable CI
to wave itself through (only the *content* — `tasks.yaml` blockers/decisions — comes from the
head). A review cycle is frozen against **both** the head and the base SHA; a base advance makes
it stale (the merged tree would differ from what was reviewed). Every signal is the *latest*
trusted state, source-bound: each configured macro reviewer must have a latest merge-compatible
result (a later not-shipped cancels an earlier shipped); a Codex signal newer than the findings
resolution or the human approval re-blocks both; a Codex comment must name the head in its exact
`Reviewed commit:` field (no loose substring); CI passes only on a `SUCCESS` conclusion; REST
review pages are `--slurp`-merged (correct past 30 reviews); and `jw round close` rolls the whole
generated SSOT view set back together on failure.

**Cycle-bound evidence (v0.2.5).** Evidence can't survive a re-freeze. A Codex signal counts only
if it post-dates the latest freeze (so re-freezing to a new cycle/base on the same head forces a
re-review); the human approval is bound to `(cycle, head, base)` and must post-date every piece of
evidence (the newest Codex signal, the latest macro result, the latest findings resolution), so an
approval can't be reused for a later cycle or be granted before the evidence it clears. Markers
sharing the newest timestamp with conflicting content fail closed (a same-second
shipped/not-shipped tie does not pass); any configured reviewer other than `codex` is a mandatory
macro reviewer (never inferred from its name); and two freezes for one cycle that disagree on
*either* head or base fail closed.

**Canonical event log + strict ordering (v0.2.6).** Order checks are *strictly after*, never
"at or after" — a Codex signal must post-date the freeze, findings the newest Codex signal, the
approval all evidence; an equal timestamp is order-ambiguous and fails closed (re-post to resolve).
The freeze boundary is the *latest* marker of the highest cycle, so re-posting a cycle advances the
boundary (a Codex review from before it goes stale). PR comments are read as a paginated REST event
log (`issues/{pr}/comments --slurp`, not the 100-cap `gh pr view`), keyed on each comment's
*effective* time (`updated_at`), so a 101st state-flipping comment is seen and an edited old comment
can't pose as old. The Codex `Reviewed commit:` line is matched anchored to its own line (quoted or
negated prose mentioning the SHA is not a signal).

**Frozen-acceptance hardening (v0.2.7).** Three trust boundaries are now structural, not patched:
(A) *PR reducer* — markers are a strict typed protocol (`yaml.safe_dump`/schema-checked, so
`cycle: true`, `review_cycle: 1.0`, a non-40-hex SHA, or `resolved: "yes"` are rejected, never
coerced); evidence must post-date the freeze (`freeze < {codex, macro result, findings} < approval`,
strictly); markers are read only from issue comments (a marker in a PENDING formal-review body is
ignored); and the trust policy is loaded once from the **base SHA** for *every* command
(freeze/status/approve/merge), so a local checkout can't switch a packet-policy repo into pr mode.
(B) *YAML mutation* — `tasks`/`state` edits are bounded by the document AST (`yaml.compose`), so a
decoy `- id:` under `metadata:` or a nested `state:` is never touched, and duplicate/ambiguous
structure fails closed. (C) *Closeout/views* — library helpers raise a catchable `WorkflowError`
(never `sys.exit`, which slipped past rollback); a single pure builder generates all SSOT views so
`jw ssot check` verifies every view's exact bytes (not just `.hash`) and flags missing/extra files;
`deps` elements must be task ids. The threat model and acceptance contract for v0.2 are frozen.

## Requirements

- `git`, `bash`, [`uv`](https://docs.astral.sh/uv/) on PATH (scripts use PEP 723 inline deps; first run downloads `pyyaml` once).

## Install

```bash
# from a marketplace that lists it
/plugin install jahns-workflow

# or for local development
claude --plugin-dir ~/workspace/jahns-workflow
```

Then in each project: `/jahns-workflow:init`. **Restart Claude Code after install** so hooks load.

## Files a project gains

```
.jahns-workflow.yml      # config: SSOT path, dir mapping, oracles, audit watermark
tasks.yaml               # THE codename registry (validated on every edit)
ROADMAP.md               # generated Mermaid dependency graph + task table (GitHub renders it)
PROGRESS.md              # round log (older months auto-archived to docs/progress/)
docs/CONVENTIONS.md      # verbatim copy of the global convention
docs/ssot/               # generated: sections/ split, INDEX.md, DIGEST.md
docs/adr/  docs/reviews/ # decisions; review request/feedback/audit records
CLAUDE.md                # gains a marker-delimited workflow stanza
```

## Token-overhead design

Skills load only on invocation (descriptions ~60 words each); hooks are command-type
(no LLM); the only recurring context cost is the SessionStart injection (digest capped at
150 lines, task summary at 8 lines per state) — which replaces re-reading a 60–150KB SSOT
after every compaction.
