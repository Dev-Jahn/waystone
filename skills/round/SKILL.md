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

First register any newly discovered work as new tasks (proper `<type>/<slug>` IDs + explanatory
titles; set `anchor:` to the governing SSOT §-anchor when known — audits scope by it). Unresolved
questions for the user become `decision/...` tasks; when a `decision/...` is answered, record the
ruling in its `ruling:` field.

Then close the round in one atomic, deterministic step instead of hand-editing each field:

```bash
uv run <plugin-root>/scripts/jw.py round close . --round <round-id> \
    --done <comma-ids that fully passed> --touched <comma-ids worked but not done>
```

`round close` flips the `--done` tasks to `done`, stamps `round:` on every worked task, validates
the registry, regenerates `ROADMAP.md` (and SSOT views if configured), advances
`state.last_round_commit`, and prints the SSOT churn (flagging the >100-line bulk-edit quarantine).
A `gate/...` task goes in `--done` only if the bar actually passed (link evidence in PROGRESS).
If `round close` reports the registry invalid, fix the reported issues before continuing.
If lanes were used this round, first verify them: `uv run <plugin-root>/scripts/jw.py lanes verify .`.

## Step 3 — SSOT quarantine check

`round close` already regenerated the SSOT views and printed the churn since the previous round.
If it reported a **bulk edit (>100 lines)**, apply the quarantine rule: state prominently in the
report and in PROGRESS that `/jahns-workflow:audit` must run on the changed sections before
dependent work consumes them. (Churn measurement, view regen, and the watermark advance are all
handled by `round close` — no manual `git diff`/watermark edit.)

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
