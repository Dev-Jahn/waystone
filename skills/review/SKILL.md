---
name: review
description: This skill should be used when the user runs "/jahns-workflow:review", pastes an external review reply (e.g. from web ChatGPT / GPT reviewer) to be processed, or asks to "ingest the review", "process the reviewer feedback", "record the external review". Preserves the review verbatim and triages findings into the task registry.
argument-hint: "[round-slug] — paste the review text in the same or next message"
---

# jahns-workflow: review

Ingest an external review reply: preserve it verbatim (reviews are otherwise ephemeral chat
text), verify each finding, and register real findings as tracked tasks.

Requires an initialized project. Plugin root = two directories above this skill's base directory.

## Step 1 — Obtain text and round

The review text comes from the invocation or the user's paste; if absent, ask the user to
paste it and stop. Round id from the argument, else the newest `<reviews_dir>/*-request.md`.

## Step 2 — Preserve verbatim

Write `<reviews_dir>/<round-id>-feedback.md`: a short metadata header (date, reviewer model if
known, round, request-packet pointer) followed by the review **verbatim — no paraphrasing,
no trimming**.

## Step 3 — Verify, then triage (never blindly implement)

Reviewer findings are claims, not facts. For each distinct finding:

1. **Verify against the actual code/SSOT** before accepting. Verdicts:
   - `REAL` — confirmed against evidence,
   - `REJECTED` — demonstrably wrong (state the evidence),
   - `NEEDS-RULING` — turns on an SSOT interpretation → register a `decision/...` task instead of acting.
2. Register each REAL finding in `tasks.yaml`: appropriate type (`fix`/`perf`/`docs`), explanatory title, `severity: blocker|major|minor`, `origin: review-<round-id>`, and `anchor:` when the finding binds to an SSOT section. The guard hook validates on save.

Append a triage table to the feedback file (in the user's configured language; quoted reviewer text verbatim): finding → verdict → evidence → task id.

## Step 4 — Report

Report in the user's configured language: counts by verdict and severity, blockers listed
first with their task IDs. Remind that blockers must be resolved before the next round
consumes downstream work; offer to start on them. Suggested commit message:
`docs(review): ingest <round-id> feedback`.

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
