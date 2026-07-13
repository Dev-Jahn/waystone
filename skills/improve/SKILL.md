---
name: improve
description: This skill should be used when the user runs "/jahns-workflow:improve", asks for workflow-improvement suggestions, wants to analyze their Claude Code 작업 이력 (work history), requests 개선 제안 grounded in past sessions, or asks "how can I work better / where am I wasting effort across my projects". It mines the user's existing Claude Code logs plus round/review evidence into deterministic facts, then presents evidence-grounded, provenance-labeled recommendations for the user to accept or reject — recording each decision without applying anything automatically.
argument-hint: "[--source DIR] [--project SLUG] (optional — defaults to all your Claude Code logs)"
---

# jahns-workflow: improve

Produce an **advisory** workflow-improvement report grounded in the user's actual Claude Code
history and review evidence. Record each accept/reject. For the small finite set of mapped
recommendations, separately offer an observation-only overlay; never materialize one without a
second explicit consent.

Plugin root = two directories above this skill's base directory. `improve` reads global logs and
the project registry, so it does **not** require the current directory to be an initialized project.

## Step 1 — Collect the evidence (deterministic)

Run the four deterministic projections in order (each writes into the improve out dir, default
`~/.claude/jahns-workflow/improve/`):

```bash
uv run <plugin-root>/scripts/jw.py improve trace
uv run <plugin-root>/scripts/jw.py improve reviews
uv run <plugin-root>/scripts/jw.py improve evidence
uv run <plugin-root>/scripts/jw.py improve audit
```

- Default source is every Claude Code log (`$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`).
  When the user names targets, pass them through unchanged:
  `improve trace --source <DIR> --project <SLUG>` (both repeatable), and pass each selected project
  to `improve evidence --project <SLUG>`. `reviews` scans the registry; `audit` reads the shared out dir.
- These are free, deterministic, and re-runnable — do **not** re-implement any of their parsing in
  the model; run the scripts and read their outputs.
- `reviews` scans the registered projects; any it cannot reach are listed in
  `reviews_coverage.json` (not silently dropped) — carry that into the report's coverage note.
- If a prior `decisions.jsonl` already exists in the out dir, read it now — Step 4 needs it.

The audit step writes `facts.json`: the 8 audit lenses plus `evidence_link` when evidence.jsonl is
present, each carrying a rule id, provenance, per-project numbers, and ≤5 evidence pointers. That
file is your **only** source of claims. If trace found no
sessions (empty corpus) or audit reports `skipped_lenses`, say so plainly rather than inventing
findings — an empty history is a finding in itself.

## Step 2 — Interpret (model) — grounded and provenance-labeled

Read `facts.json` and derive recommendations. HARD rules (invariant #11):

- **No claim that isn't in facts.json.** Every recommendation cites its lens, the numbers, and an
  evidence pointer (file + line). If the facts don't support it, don't say it.
- **Label evidence strength.** A recommendation built on an `inferred` lens is stated as
  "패턴상 추정(<rule-id>)"; one built on an `explicit` lens may be stated directly. Never present an
  inferred pattern as a certainty — that distinction is the user-facing form of invariant #11.
- **State report-confidence limits.** When `coverage_caveats` is non-trivial (parse errors, skipped
  files, partial tails, unknown record types), say so up front — the report is only as complete as
  the coverage allows.
- **Open with an honest maturity framing.** Per project, when `review_association` reports
  `rounds_with_feedback >= 5` and `findings_total >= 20`, label it
  **"Tune — overlay proposals available"**. Otherwise retain Bootstrap / Calibrate framing and keep
  every recommendation **soft** — observation and suggestion, not a rule to adopt. Do not
  manufacture personalization the data can't support. Tune eligibility does not promote a delta;
  the CLI still requires replay before warning.
- **Context discipline.** Read `facts.json` (and the two small coverage jsons) ONLY. Never open
  `sessions.jsonl`/`delegations.jsonl` (multi-MB aggregates, not model input) and never open the raw
  transcripts behind evidence pointers — cite pointers as-is; the user inspects them on demand. If a
  fact seems to need more detail than facts.json carries, that is a lens-improvement finding to
  report, not a license to read the lake.

## Step 3 — Present and record (approval = RECORDING only)

For each recommendation, use **AskUserQuestion** to get an explicit accept/reject — one question per
recommendation, never a generic wizard and never a batched "apply this plan". Then record the
decision deterministically:

```bash
uv run <plugin-root>/scripts/jw.py improve decide <rec-id> accept|reject [--title "..."] [--note "..."]
```

**Approval is recording.** It does not itself materialize or apply anything. When the user accepts a
recommendation, explain the concrete action. Then apply Step 3.5 only when the recommendation matches
the finite mapping below.

## Step 3.5 — Separately offer observation-only materialization

For each accepted recommendation that matches this table, use a separate **AskUserQuestion**:
"Store this as an overlay delta? It starts in observing (records only, no warning)." Ask once per
recommendation. A no does nothing; the Step 3 decision is already recorded.

| recommendation lens | overlay rule |
|---|---|
| `verification_debt/*` | `delegation-verification-evidence-v1` |
| `review_association/*` only for an unresolved severe-finding pattern | `round-close-open-findings-v1` |

This mapping is exhaustive. HARD: never map another lens or infer a new rule. On yes, fill every
flag from facts already read and use the CLI only:

```bash
uv run <plugin-root>/scripts/jw.py overlay add <rec-id> --rule <mapped-rule> \
  --summary "<observed numbers>" --pointers "<evidence pointer>" --from-rec <rec-id> \
  --expected-effect "<bounded expectation>" --risk "<known friction>" \
  --candidate-scope <project_candidate|user_candidate|unresolved> --observed-in <project-slug>
```

Never write delta JSON directly. After creation, explain that the delta can be considered for warning
only after `jw overlay replay <rec-id>` and then `jw overlay promote <rec-id>`; do not run promotion
as part of improve.

When citing replay, report only that it "would have fired" and the estimated nuisance rate (which is
null while unlabeled). Never use the quality-claim words **prevented**, **improved**, or **benefit**.

## Step 4 — Suppress re-nagging (stable rec ids)

Mint each `rec_id` as `<lens>/<kebab-gist>` so the same recommendation keeps the same id across
cycles (e.g. `main_direct_work/heavy-solo-implementation`). Reuse the same gist for the same
underlying pattern — that stability is what makes the decision log meaningful. A recommendation the
user previously **rejected** (per `decisions.jsonl`) is re-surfaced only when the evidence is
*materially* new — new sessions, a higher rate, a newly affected project — not merely because you
re-ran the audit.

## Step 5 — Report

Report in the user's configured language. Lead with the per-project maturity framing, then list recommendations
ordered by evidence strength and impact — each with its lens, numbers, evidence pointer, and
strength label — and note any coverage caveats. Close by summarizing what was accepted vs rejected,
which observation-only deltas (if any) were separately created, and where the decision log lives
(`~/.claude/jahns-workflow/improve/decisions.jsonl`).

End with the **next-step reminder**:

> Recommendations were recorded, not applied. Any separately accepted overlay starts in observing
> (records only, no warning); replay is required before warning promotion. Re-run
> `/jahns-workflow:improve` after a few more rounds; decisions are remembered, so the next report
> focuses on what's new.
