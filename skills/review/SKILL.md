---
name: review
description: This skill should be used when the user runs "/waystone:review" in Claude Code or "$waystone:review" in Codex, pastes an external review reply (e.g. from web ChatGPT / GPT reviewer) to be processed, or asks to "ingest the review", "process the reviewer feedback", "record the external review". Preserves the review verbatim and triages findings into the task registry.
argument-hint: "[round-slug] — first save the reply: cat > /tmp/review.md, paste, Ctrl-D"
---

# waystone: review

## Host contract

- Claude Code: invoke `/waystone:review`; assign `$CLAUDE_PLUGIN_ROOT` to
  `WAYSTONE_PLUGIN_ROOT`, then run command examples with `waystone` from `PATH`.
- Codex: invoke `$waystone:review`; from this skill's directory walk up two parents, assign that
  absolute path to `WAYSTONE_PLUGIN_ROOT`, then run command examples with
  `$WAYSTONE_PLUGIN_ROOT/bin/waystone-codex`.
- Resolve plugin resources from `$WAYSTONE_PLUGIN_ROOT`. Ask required choices through the host's native
  user-interaction mechanism; never require a specifically named question tool.

Ingest an external review reply: preserve it verbatim (reviews are otherwise ephemeral chat
text), verify each finding, and register real findings as tracked tasks.

Requires an initialized project.

## Step 1 — Locate the reply and round

The reviewer's reply is at the fixed drop-file `/tmp/review.md` — the user saves it there in a
separate shell (`cat > /tmp/review.md`, paste, `Ctrl-D`) so it never passes through the model.
Round id from the argument, else the newest `<reviews_dir>/*-request.md`.

## Step 2 — Preserve verbatim (deterministic copy — never re-type it)

Do **not** write the verbatim copy yourself (a model re-emitting text is not byte-exact). Run the
deterministic ingest, which copies `/tmp/review.md` byte-exact into the feedback file under a
metadata header and consumes the drop-file:

```bash
waystone review ingest . --round <round-id>
```

The reply itself must begin with the request template's key/value block: `model`, `effort`,
`review-target` (`<target-sha>` or `<base-sha>-<target-sha>`, 12–40 hex characters each), and the
request's exact `request-digest`. Key case/order/colon whitespace and an optional Markdown fence are
tolerated; extra keys are preserved. Missing, duplicate, invalid, or non-UTF-8 values stay unknown,
and a leading key/value block with neither `model` nor `review-target` is ordinary prose. Ingest
resolves an echoed digest to that round's immutable request sidecar generation; a stale or unknown
generation remains pending. A digestless reply can use the ingest-time binding only for a genuine
legacy v1 binding; a v2 reply must be resubmitted with the request's digest line. Ingest never
rebuilds a missing legacy binding from current config or profile. For new projects that sidecar
already freezes the backend resolved from `role:reviewer` at publication time. A model matches an
exact configured identity, or a provider-qualified identity matches the same bare model slug; two
provider-qualified identities must match completely.
Missing/mismatched identity or target does not count as configured feedback for
`review-skipped-closes-v1`.

Besides the byte-exact copy, ingest **appends** (never edits the verbatim body) a marker-delimited
*finding triage skeleton*: if the reply has `JW-GPT-NNN` finding blocks it builds a table, else it
notes "triage the verbatim reply directly". Then read `<reviews_dir>/<round-id>-feedback.md` to
triage. Never edit that feedback file directly: write only the replacement triage content (without
the BEGIN/END markers) to a separate file such as `/tmp/review-triage.md`, then run:

```bash
waystone review triage . --round <round-id> --file /tmp/review-triage.md
```

The command replaces only the marked tail and preserves every preceding byte. Missing or damaged
markers are a refusal, not permission to reconstruct the file.

Relay any review-ingest adaptive-rule output with tri-state wording: **fired**, **did not fire
(evaluable)**, or **unevaluable (<coverage reason>)**. Never turn an unevaluable result into a
non-fire. A `waystone warn conflict` line remains a policy conflict with a least-restrictive effective
stage; it is separate from the finding verdicts below and from a rule fire.

If ingest reports `no review at /tmp/review.md`, tell the user to save the reply first
(`cat > /tmp/review.md`, paste, `Ctrl-D`) and stop.

(PR mode: the macro reviewer's verdict reaches the merge gate through its `waystone-review-result` PR
comment marker, read from GitHub by `waystone round merge` — not through this ingest, which just preserves
the reply locally.)

## Step 3 — Verify, then triage (never blindly implement)

Reviewer findings are claims, not facts. For each distinct finding:

1. **Verify against the actual code/SSOT** before accepting. Verdicts:
   - `REAL` — confirmed against evidence,
   - `REJECTED` — demonstrably wrong (state the evidence),
   - `NEEDS-RULING` — turns on an SSOT interpretation → register a `decision/...` task instead of acting.
2. Assign exactly one taxonomy type to every finding: `correctness`, `scope`, `architecture`,
   `verification`, `reproducibility`, or `reporting`. Use `unknown` only when the evidence cannot
   support one of those six after verification, and write the concrete reason in the evidence cell.
   A blank type is not a completed triage row.
3. Register each REAL finding via the CLI — `waystone task add <fix|perf|docs>/<slug> . --title "..." --severity <blocker|major|minor> --origin review-<round-id> [--anchor §...]` — not by editing `tasks.yaml`. The add is validated and comment-preserving.

If ingest parsed a `JW-GPT-NNN` triage-skeleton table, prepare the complete replacement triage
section in `/tmp/review-triage.md`, filling each row (in the user's configured language; quoted
reviewer text verbatim): verdict → taxonomy type → evidence/reason → task id. A free-form reply has
no such rows — prepare a replacement section that records each finding from the verbatim body
directly (verdict → taxonomy type → evidence → register REAL ones). After task registration, run
`waystone review triage` as shown above; do not patch or rewrite the feedback file itself.

## Step 4 — Report

Report in the user's configured language: counts by verdict and severity, blockers listed
first with their task IDs. Remind that blockers must be resolved before the next round
consumes downstream work; offer to start on them. Suggested commit message:
`docs(review): ingest <round-id> feedback`.

Then **refresh the re-entry pointer** (the review moved the frontier): get its path with
`waystone resume --start-here-path .` and overwrite it (≤ ~35
lines) the post-review frontier — open blockers/decisions and what to pick up next, detail linked
to the feedback/PROGRESS files. The SessionStart hook injects this so the next session resumes
without re-explaining. (Same file the round skill writes; see round Step 6.)

## PR mode (review.mode: pr) — SHA-bound cycle + merge gate

When the project uses PR-mode review, the same verify-then-register discipline applies, plus:

- After adjudicating a cycle's Codex findings, post a resolution marker so the merge gate can
  see it — a PR comment containing `<!-- waystone-findings:v1\ncycle: <N>\nresolved: true\n-->`
  (only after every REAL finding is fixed/deferred-with-cause).
- A finding fixed in code produces a NEW head SHA, which makes the frozen cycle stale. Do not
  merge against a stale cycle — re-freeze (`waystone review freeze --pr <N>`) so reviewers re-examine
  the new SHA. Codex re-reviews the new head; the macro reviewer does a full or delta review.
- The merge is gated, not judged: `waystone round merge --pr <N> .`
  prints PASS only when the cycle is fresh, CI ok (if required), a fresh Codex review + resolved
  findings + a macro result are all bound to the current head, zero open blockers/decisions, and
  a human approval is bound to the current head. The user approves with
  `waystone approve --pr <N> --sha <current-head>` (a new push auto-invalidates it). Only run
  `round merge --pr <N> --execute --squash|--rebase|--merge` once the gate passes and the user
  has approved — never merge on natural-language judgement.
