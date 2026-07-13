---
name: improve
description: This skill should be used when the user runs "/jahns-workflow:improve", asks for workflow-improvement suggestions, wants to analyze their Claude Code 작업 이력 (work history), requests 개선 제안 grounded in past sessions, or asks "how can I work better / where am I wasting effort across my projects". It mines the user's existing Claude Code logs plus round/review evidence into deterministic facts, then presents evidence-grounded, provenance-labeled recommendations for the user to accept or reject — recording each decision without applying anything automatically.
argument-hint: "[--source DIR] [--project SLUG] (optional — defaults to all your Claude Code logs)"
---

# jahns-workflow: improve

Produce an **advisory** workflow-improvement report grounded in the user's actual Claude Code
history and review evidence. This version only advises: it shows recommendations with their
evidence and records each accept/reject — it never changes config or code automatically.

Plugin root = two directories above this skill's base directory. `improve` reads global logs and
the project registry, so it does **not** require the current directory to be an initialized project.

## Step 1 — Collect the evidence (deterministic)

Run the three deterministic projections in order (each writes into the improve out dir, default
`~/.claude/jahns-workflow/improve/`):

```bash
uv run <plugin-root>/scripts/jw.py improve trace
uv run <plugin-root>/scripts/jw.py improve reviews
uv run <plugin-root>/scripts/jw.py improve audit
```

- Default source is every Claude Code log (`$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`).
  When the user names targets, pass them through unchanged:
  `improve trace --source <DIR> --project <SLUG>` (both repeatable). `reviews`/`audit` follow.
- These are free, deterministic, and re-runnable — do **not** re-implement any of their parsing in
  the model; run the scripts and read their outputs.
- `reviews` scans the registered projects; any it cannot reach are listed in
  `reviews_coverage.json` (not silently dropped) — carry that into the report's coverage note.
- If a prior `decisions.jsonl` already exists in the out dir, read it now — Step 4 needs it.

The audit step writes `facts.json`: 8 lenses, each carrying a rule id, provenance, per-project
numbers, and ≤5 evidence pointers. That file is your **only** source of claims. If trace found no
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
- **Open with an honest maturity framing.** When round/review evidence is thin (Bootstrap /
  Calibrate), say so at the top and keep every recommendation **soft** — observation and suggestion,
  not a rule to adopt. Do not manufacture personalization the data can't support.
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

**Approval is recording, nothing more.** This version applies nothing automatically. When the user
accepts a recommendation, tell them concretely what *applying* it would involve — which file, which
command, which habit to change — and stop there. The user applies it.

## Step 4 — Suppress re-nagging (stable rec ids)

Mint each `rec_id` as `<lens>/<kebab-gist>` so the same recommendation keeps the same id across
cycles (e.g. `main_direct_work/heavy-solo-implementation`). Reuse the same gist for the same
underlying pattern — that stability is what makes the decision log meaningful. A recommendation the
user previously **rejected** (per `decisions.jsonl`) is re-surfaced only when the evidence is
*materially* new — new sessions, a higher rate, a newly affected project — not merely because you
re-ran the audit.

## Step 5 — Report

Report in the user's configured language. Lead with the maturity framing, then list recommendations
ordered by evidence strength and impact — each with its lens, numbers, evidence pointer, and
strength label — and note any coverage caveats. Close by summarizing what was accepted vs rejected
and where the decision log lives (`~/.claude/jahns-workflow/improve/decisions.jsonl`).

End with the **next-step reminder**:

> These are recommendations, not changes — nothing was applied. To act on an accepted item, do it
> yourself (the report says exactly what each involves). Re-run `/jahns-workflow:improve` after a few
> more rounds; your accept/reject decisions are remembered, so the next report focuses on what's new.
