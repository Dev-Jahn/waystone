---
name: round
description: This skill should be used when the user runs "/waystone:round", says to "close the round", "wrap up this round", "finish the work cycle", or when an autonomous work round (implement → verify → push) reaches its end and the project CLAUDE.md mandates round closeout. Updates the task registry, PROGRESS, roadmap, SSOT views, and writes the round's markdown review request.
argument-hint: "[round-slug] e.g. lstream-seams"
---

# waystone: round

Close the current work round: bring the task registry up to date, record the round in
PROGRESS, refresh generated views, and write the round's external review request (one markdown
file; the reviewer reads the repo over git).

Requires an initialized project (`.waystone.yml`). If missing, stop and point the user
at `/waystone:init`. Plugin root = two directories above this skill's base directory.

## Step 1 — Determine the round id

`<today YYYY-MM-DD>-<slug>`: slug from the argument if given, else derive a short one from the
round's dominant theme. Check PROGRESS for an existing entry with the same id (extend it
rather than duplicating).

## Step 2 — Sync the task registry

First register any newly discovered work as new tasks via the CLI — `uv run <plugin-root>/scripts/waystone.py
task add <type>/<slug> . --title "..." [--severity ...] [--deps a,b]` (proper IDs + explanatory
titles; set `anchor:` to the governing SSOT §-anchor when known) — rather than hand-editing the
registry. Unresolved questions for the user become `decision/...` tasks; when a `decision/...` is
answered, record the ruling with `waystone task set <id> ruling "..."`.

An implementation task can be delegated to an external runner with `waystone delegate run <task-id>` — it
runs in an isolated worktree cut from a snapshot of your current tree and comes back as a reviewable
patch you `apply` or `discard` (the guided flow arrives in a later milestone).

Then close the round in one atomic, deterministic step instead of hand-editing each field:

```bash
uv run <plugin-root>/scripts/waystone.py round close . --round <round-id> \
    --done <comma-ids that fully passed> --touched <comma-ids worked but not done>
```

`round close` flips the `--done` tasks to `done`, stamps `round:` on every worked task, validates
the registry, regenerates `ROADMAP.md` (and SSOT views if configured), and advances
`state.last_round_commit`.
A `gate/...` task goes in `--done` only if the bar actually passed (link evidence in PROGRESS).
If `round close` reports the registry invalid, fix the reported issues before continuing.
If lanes were used this round, first verify them: `uv run <plugin-root>/scripts/waystone.py lanes verify .`.

Then keep the registry small: `uv run <plugin-root>/scripts/waystone.py task archive .` relocates old
done/dropped tasks into `tasks.archive.yaml` once the registry crosses a size threshold (it keeps
the most-recent few for decision context, and never archives a task a live one still depends on).
It is a safe no-op below the threshold, so run it every round.

## Step 3 — PROGRESS entry + archive

Append an entry from `<plugin-root>/templates/progress-entry.md` (content in the user's
configured language). Then archive: move dated sections from months before the current one
into `docs/progress/<YYYY-MM>.md` (mechanical cut-paste, newest-first preserved), leaving
PROGRESS.md with the current month + the header pointers.

## Step 4 — Request review

**Push gate first (both modes):** run `uv run <plugin-root>/scripts/waystone.py remote verify .`. A review
must point at a pushed commit; if it exits non-zero, STOP and have the user push the round's commits.
If your conventions end a round in a commit, commit the closeout (`docs(round): close <round-id>`)
and push it FIRST so `tasks.yaml` / PROGRESS carry the round's final state.

Write `<reviews_dir>/<round-id>-request.md` from `<plugin-root>/templates/review-request.md`: what
changed and *why*, the files to read first, falsifiable "claims to attack", evidence pointers (to
where logs/PROGRESS already live — do **not** copy them), known weak spots, and the domain lens. Fill
`Reviewing` with `git rev-parse HEAD` and the diff base with the **`review diff base`** value
`waystone round close` printed in Step 2 (the previous round's tip, or `(root)` for the first round — the
live `state.last_round_commit` is no longer it, having just advanced to this round's tip). The
reviewer reaches the repo over git, so the request is the only artifact you author — no zip, no bundle.

**Packet mode** (`review.mode: packet`, default): give the user the request file and a one-line
prompt, e.g.:

> `docs/reviews/<round-id>-request.md`를 읽고, 거기 적힌 claim이 코드/테스트로 성립하는지 repo를
> 직접 확인하며 major 위주로 도메인 리뷰해줘.

If a repo-local `docs/review-profile.md` exists (the project's standing domain lens), the reviewer
reads it too — the brief points there.

**PR mode** (`review.mode: pr`): also freeze a SHA-bound review cycle and post the `@codex` request:
`uv run <plugin-root>/scripts/waystone.py review freeze --pr <N> --round <round-id> .` (stamps the current
PR head as cycle N + posts the request). The macro reviewer reads the PR + the request file. Check
progress with `waystone review status --pr <N>`; never treat "a comment appeared" as "review done" — a
review is `(reviewer, cycle, reviewed_sha)`.

## Step 5 — Report

Report in the user's configured language: shipped tasks (id — title), registry/roadmap state, where
the review request (`<reviews_dir>/<round-id>-request.md`) is, and a suggested commit message
(`docs(round): close <round-id>`). Do not commit unless the project's conventions say rounds end in
a commit and the user has authorized committing.

End with the **next-step reminder** (so the reply is preserved byte-exact, not re-typed by a model):

> Give the reviewer the round request (`<reviews_dir>/<round-id>-request.md`) and the prompt; the
> reviewer reads the repo over git. To ingest the reply, save it **in a separate shell**:
> `cat > /tmp/review.md` → paste → `Ctrl-D`. Then run `/waystone:review <round-id>`, which
> copies `/tmp/review.md` verbatim into the reviews dir (no model retyping) and triages it.

## Step 6 — Refresh the re-entry pointer

**OVERWRITE** the project's persistent re-entry file so the next session — or a post-compaction
resume — picks up the live frontier without you re-explaining "where were we". Get its path:

```bash
uv run <plugin-root>/scripts/waystone.py resume --start-here-path .
```

Then **Write** that file (overwrite — never append), **≤ ~35 lines / ~2.5KB**:

- first line: `# re-entry @ <round-id> / HEAD <short-sha>`
- then the live frontier: what just landed, the open decision / next probe and **why**, the active
  lane(s) — with detail **linked** to PROGRESS / topic files, not inlined.

This replaces the old habit of growing a "START HERE" blob inside the agent-memory `MEMORY.md`
(which accumulates unbounded). The SessionStart hook injects this file automatically each session;
it is reset every round, so keep it short and current. Authoritative state stays in
tasks.yaml / PROGRESS — this is only a pointer.
