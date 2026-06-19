---
name: round
description: This skill should be used when the user runs "/jahns-workflow:round", says to "close the round", "wrap up this round", "finish the work cycle", or when an autonomous work round (implement → verify → push) reaches its end and the project CLAUDE.md mandates round closeout. Updates the task registry, PROGRESS, roadmap, SSOT views, and produces the external review packet.
argument-hint: "[round-slug] e.g. lstream-seams"
---

# jahns-workflow: round

Close the current work round: bring the task registry up to date, record the round in
PROGRESS, refresh generated views, and emit a paste-ready external review packet.

Requires an initialized project (`.jahns-workflow.yml`). If missing, stop and point the user
at `/jahns-workflow:init`. Plugin root = two directories above this skill's base directory.

## Step 1 — Determine the round id

`<today YYYY-MM-DD>-<slug>`: slug from the argument if given, else derive a short one from the
round's dominant theme. Check PROGRESS for an existing entry with the same id (extend it
rather than duplicating).

## Step 2 — Sync the task registry

Update `tasks.yaml` to match reality (the guard hook auto-validates and regenerates
ROADMAP.md on save — if it reports violations, fix them immediately):

- Mark finished tasks `done` (a `gate/...` task is done only if the bar actually passed — link evidence in PROGRESS).
- Set `round: <round-id>` on every task worked this round.
- Register newly discovered work as new tasks (proper `<type>/<slug>` IDs + explanatory titles; set `anchor:` to the governing SSOT §-anchor when known — audits scope by it). Unresolved questions for the user become `decision/...` tasks.
- Update `blocked` states from deps.

## Step 3 — SSOT maintenance

Skip if the config has no `ssot:`. Otherwise:

```bash
uv run <plugin-root>/scripts/jw_ssot.py check .
# stale (exit 3) → regenerate:
uv run <plugin-root>/scripts/jw_ssot.py split . && uv run <plugin-root>/scripts/jw_ssot.py digest .
```

Then measure the round's SSOT churn: `git diff <watermark> HEAD --numstat -- <ssot>`, where
the watermark is `state.last_round_commit` from `.jahns-workflow.yml`; if null, fall back to
the most recent commit whose subject starts with `docs(round):`; if neither exists, skip the
measurement and note that. If added+deleted > 100 lines, apply the **bulk-edit quarantine**
rule: state prominently in the report and in PROGRESS that `/jahns-workflow:audit` must run
on the changed sections before dependent work consumes them. Finally, update
`state.last_round_commit` to the current HEAD.

## Step 4 — PROGRESS entry + archive

Append an entry from `<plugin-root>/templates/progress-entry.md` (content in the user's
configured language). Then archive: move dated sections from months before the current one
into `docs/progress/<YYYY-MM>.md` (mechanical cut-paste, newest-first preserved), leaving
PROGRESS.md with the current month + the header pointers.

## Step 5 — Request review

**First, a hard push gate (both modes):** run `uv run <plugin-root>/scripts/jw.py remote verify .`.
A review must point at a pushed commit; if this exits non-zero, STOP and tell the user to push
the round's commits before a packet/cycle is created — do not emit a packet for an unpushed HEAD.

**Packet mode** (`review.mode: packet`, default): generate `<reviews_dir>/<round-id>-request.md`
from `<plugin-root>/templates/review-request.md`. External reviewers typically browse the repo
directly (e.g. ChatGPT's GitHub connector), so record the pushed HEAD hash and prefer pointers
(file paths, §-anchors, commit hashes) over inlined diffs — inline only small load-bearing
snippets, or full diffs/pseudocode if the reviewer has no repo access. **Pin the packet HEAD to
the load-bearing implementation commit, not a later docs-only round-close commit** — otherwise
the reviewer reads a stale registry and raises false-positive findings. State every load-bearing
claim falsifiably and list the test ladder's known blind spots.

**PR mode** (`review.mode: pr`): open/locate the round's PR, then freeze a SHA-bound review cycle:
`uv run <plugin-root>/scripts/jw.py review freeze --pr <N> --round <round-id> .`. This stamps the
current PR head as cycle N (immutable target), posts the `@codex` request, and asks the macro
reviewer to bind its reply to that SHA. Check progress with `jw review status --pr <N>`; never
treat "a comment appeared" as "review done" — a review is `(reviewer, cycle, reviewed_sha)`.

## Step 6 — Report

Report in the user's configured language: shipped tasks (id — title), registry/roadmap state,
SSOT churn (+ quarantine flag if triggered), where the review packet is, and a suggested
commit message (`docs(round): close <round-id>`). Remind: paste the packet to the external
reviewer and ingest the reply with `/jahns-workflow:review <round-id>`. Do not commit unless
the project's conventions say rounds end in a commit and the user has authorized committing.
