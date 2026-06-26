---
name: review
description: This skill should be used when the user runs "/jahns-workflow:review", pastes an external review reply (e.g. from web ChatGPT / GPT reviewer) to be processed, or asks to "ingest the review", "process the reviewer feedback", "record the external review". Preserves the review verbatim and triages findings into the task registry.
argument-hint: "[round-slug] — first save the reply: cat > /tmp/review.md, paste, Ctrl-D"
---

# jahns-workflow: review

Ingest an external review reply: preserve it verbatim (reviews are otherwise ephemeral chat
text), verify each finding, and register real findings as tracked tasks.

Requires an initialized project. Plugin root = two directories above this skill's base directory.

## Step 1 — Locate the reply and round

The reviewer's reply is at the fixed drop-file `/tmp/review.md` — the user saves it there in a
separate shell (`cat > /tmp/review.md`, paste, `Ctrl-D`) so it never passes through the model.
Round id from the argument, else the newest `<reviews_dir>/*-request.md`.

## Step 2 — Preserve verbatim (deterministic copy — never re-type it)

Do **not** write the verbatim copy yourself (a model re-emitting text is not byte-exact). Run the
deterministic ingest, which copies `/tmp/review.md` byte-exact into the feedback file under a
metadata header and consumes the drop-file:

```bash
uv run <plugin-root>/scripts/jw.py review ingest . --round <round-id> --reviewer "<model, e.g. gpt-5.5-pro>"
```

Besides the byte-exact copy, ingest **appends** (never edits the verbatim body) an *identity check*
and a *finding triage skeleton* beneath it.

- **Default `raw-zip` flow:** a loose domain reviewer's reply usually carries **no**
  `jw-review-summary` marker, so ingest records the identity as `no-marker` and **proceeds to
  triage** (exit 0). That is expected, not a failure — the binding is the brief's "Reviewed HEAD"
  the reviewer was asked to confirm (verify in Step 0 below that the SHA the reviewer reports matches
  the brief). If a marker *is* present, ingest cross-checks `reviewed_sha` against the live `HEAD`
  (plus `round_id`/`project`); a mismatch here may simply mean **HEAD advanced since you sent the
  review** (you committed more while waiting) — confirm the reviewer's SHA is the one you sent before
  deciding it reviewed the wrong tree.

  **Step 0 (raw-zip only):** read the reviewer's reported `git rev-parse HEAD` from their reply and
  confirm it equals the brief's `Reviewed HEAD`. If they differ, the reviewer reviewed a stale/wrong
  zip — re-send the correct one; don't triage. (This is the implementer-side check the loose flow
  relies on, since there is no SHA-pinned bundle record.)
- **`strict-bundle` / PR mode:** ingest parses the `jw-review-summary:v1` marker and cross-checks
  it against the round's bundle record (`<round-id>-bundle.yaml`) on every identity axis. **If it
  exits 3 (identity MISMATCH)** the reply reviewed a different target than was shipped — do NOT
  triage; re-bundle the correct head or get a reply bound to it. For a PR-mode cycle pass `--pr <N>`
  so the cross-check binds to the frozen cycle head.

If ingest reports `no review at /tmp/review.md`, tell the user to save the reply first
(`cat > /tmp/review.md`, paste, `Ctrl-D`) and stop. Then read
`<reviews_dir>/<round-id>-feedback.md` to triage.

## Step 3 — Verify, then triage (never blindly implement)

Reviewer findings are claims, not facts. For each distinct finding:

1. **Verify against the actual code/SSOT** before accepting. Verdicts:
   - `REAL` — confirmed against evidence,
   - `REJECTED` — demonstrably wrong (state the evidence),
   - `NEEDS-RULING` — turns on an SSOT interpretation → register a `decision/...` task instead of acting.
2. Register each REAL finding in `tasks.yaml`: appropriate type (`fix`/`perf`/`docs`), explanatory title, `severity: blocker|major|minor`, `origin: review-<round-id>`, and `anchor:` when the finding binds to an SSOT section. The guard hook validates on save.

A loose (`raw-zip`) reply has free-form findings, so ingest parses **no** `JW-GPT-NNN` rows and
notes "triage the verbatim reply manually" — triage each finding in the body directly (verdict →
evidence → register REAL ones), no table required. A `strict-bundle`/PR reply follows the output
contract, so ingest appends a `JW-GPT-NNN` triage-skeleton table — fill each row (in the user's
configured language; quoted reviewer text verbatim): verdict → evidence → task id.

## Step 4 — Report

Report in the user's configured language: counts by verdict and severity, blockers listed
first with their task IDs. Remind that blockers must be resolved before the next round
consumes downstream work; offer to start on them. Suggested commit message:
`docs(review): ingest <round-id> feedback`.

Then **refresh the re-entry pointer** (the review moved the frontier): get its path with
`uv run <plugin-root>/scripts/jw.py resume --start-here-path .` and **Write** (overwrite, ≤ ~35
lines) the post-review frontier — open blockers/decisions and what to pick up next, detail linked
to the feedback/PROGRESS files. The SessionStart hook injects this so the next session resumes
without re-explaining. (Same file the round skill writes; see round Step 6.)

## PR mode (review.mode: pr) — SHA-bound cycle + merge gate

When the project uses PR-mode review, the same verify-then-register discipline applies, plus:

- After adjudicating a cycle's Codex findings, post a resolution marker so the merge gate can
  see it — a PR comment containing `<!-- jw-findings:v1\ncycle: <N>\nresolved: true\n-->`
  (only after every REAL finding is fixed/deferred-with-cause).
- A finding fixed in code produces a NEW head SHA, which makes the frozen cycle stale. Do not
  merge against a stale cycle — re-freeze (`jw review freeze --pr <N>`) so reviewers re-examine
  the new SHA. Codex re-reviews the new head; the macro reviewer does a full or delta review.
- The merge is gated, not judged: `uv run <plugin-root>/scripts/jw.py round merge --pr <N> .`
  prints PASS only when the cycle is fresh, CI ok (if required), a fresh Codex review + resolved
  findings + a macro result are all bound to the current head, zero open blockers/decisions, and
  a human approval is bound to the current head. The user approves with
  `jw approve --pr <N> --sha <current-head>` (a new push auto-invalidates it). Only run
  `round merge --pr <N> --execute --squash|--rebase|--merge` once the gate passes and the user
  has approved — never merge on natural-language judgement.
