---
name: round
description: This skill should be used when the user runs "/jahns-workflow:round", says to "close the round", "wrap up this round", "finish the work cycle", or when an autonomous work round (implement → verify → push) reaches its end and the project CLAUDE.md mandates round closeout. Updates the task registry, PROGRESS, roadmap, SSOT views, and produces the external review bundle.
argument-hint: "[round-slug] e.g. lstream-seams"
---

# jahns-workflow: round

Close the current work round: bring the task registry up to date, record the round in
PROGRESS, refresh generated views, and build a self-contained external review bundle.

Requires an initialized project (`.jahns-workflow.yml`). If missing, stop and point the user
at `/jahns-workflow:init`. Plugin root = two directories above this skill's base directory.

## Step 1 — Determine the round id

`<today YYYY-MM-DD>-<slug>`: slug from the argument if given, else derive a short one from the
round's dominant theme. Check PROGRESS for an existing entry with the same id (extend it
rather than duplicating).

## Step 2 — Sync the task registry

First register any newly discovered work as new tasks (proper `<type>/<slug>` IDs + explanatory
titles; set `anchor:` to the governing SSOT §-anchor when known). Unresolved
questions for the user become `decision/...` tasks; when a `decision/...` is answered, record the
ruling in its `ruling:` field.

Then close the round in one atomic, deterministic step instead of hand-editing each field:

```bash
uv run <plugin-root>/scripts/jw.py round close . --round <round-id> \
    --done <comma-ids that fully passed> --touched <comma-ids worked but not done>
```

`round close` flips the `--done` tasks to `done`, stamps `round:` on every worked task, validates
the registry, regenerates `ROADMAP.md` (and SSOT views if configured), and advances
`state.last_round_commit`.
A `gate/...` task goes in `--done` only if the bar actually passed (link evidence in PROGRESS).
If `round close` reports the registry invalid, fix the reported issues before continuing.
If lanes were used this round, first verify them: `uv run <plugin-root>/scripts/jw.py lanes verify .`.

## Step 3 — PROGRESS entry + archive

Append an entry from `<plugin-root>/templates/progress-entry.md` (content in the user's
configured language). Then archive: move dated sections from months before the current one
into `docs/progress/<YYYY-MM>.md` (mechanical cut-paste, newest-first preserved), leaving
PROGRESS.md with the current month + the header pointers.

## Step 4 — Request review

**First, a hard push gate (both modes):** run `uv run <plugin-root>/scripts/jw.py remote verify .`.
A review must point at a pushed commit; if this exits non-zero, STOP and tell the user to push
the round's commits before a packet/cycle is created — do not emit a packet for an unpushed HEAD.

**Packet mode** (`review.mode: packet`, default): the reviewer no longer browses the repo (the
ChatGPT GitHub connector is unavailable) — it reads a self-contained **review bundle** zip.

1. Write `<reviews_dir>/<round-id>-request.md` from `<plugin-root>/templates/review-request.md`:
   state every load-bearing claim **falsifiably** under "Claims to attack" and list the test
   ladder's known blind spots. This is packaged verbatim as the bundle's `__review__/REQUEST.md`.
2. **The reviewed tree is the current HEAD.** So if your conventions end a round in a commit, commit
   the closeout (`docs(round): close <round-id>`) and push it FIRST — then `repo/tasks.yaml` /
   PROGRESS carry the round's final state and the manifest scope matches them. (Bundling before the
   closeout commit is allowed; the bundle is then internally consistent but marked `worktree_dirty`.)
3. Build the bundle:
   `uv run <plugin-root>/scripts/jw.py review bundle . --round <round-id>`. It reads the base
   watermark `round close` recorded, builds `repo/` **directly from git objects of HEAD** (exact
   tracked tree — no `.git`/caches/secrets; a symlink ships as a regular file holding its target string,
   recorded in `manifest.symlinks`, never a rebuildable link entry),
   adds `__review__/DIFF.patch` + `CHANGED_FILES.txt` + `COMMITS.txt` + a schema-validated
   `MANIFEST.yaml`, stamps the reviewed head into the sidecar, and writes the zip to
   `<reviews_dir>/bundles/` (untracked). HEAD must be pushed (the gate above).
4. Attach that zip to the web reviewer and paste the one-line prompt the command prints. (One-time:
   the ChatGPT Project must hold the reviewer kit — `/jahns-workflow:reviewer-kit`.)

**PR mode** (`review.mode: pr`): the `@codex` PR bot reviews on the PR, but the macro reviewer
(GPT) also lost repo browsing and needs the same bundle.

1. Open/locate the round's PR, then freeze a SHA-bound review cycle:
   `uv run <plugin-root>/scripts/jw.py review freeze --pr <N> --round <round-id> .`. This stamps the
   current PR head as cycle N (immutable target) and posts the `@codex` request.
2. Write `<reviews_dir>/<round-id>-request.md` (same falsifiable-claims template).
3. Build the macro-reviewer bundle: `uv run <plugin-root>/scripts/jw.py review bundle . --pr <N>
   --round <round-id>` (reviewed head = the frozen cycle SHA). Attach it + paste the one-line prompt;
   the reply ends with a `jw-review-result` marker the merge gate consumes.
4. Check progress with `jw review status --pr <N>`; never treat "a comment appeared" as "review
   done" — a review is `(reviewer, cycle, reviewed_sha)`.

## Step 5 — Report

Report in the user's configured language: shipped tasks (id — title), registry/roadmap state,
where the review bundle (`*.review.zip`) is, and a suggested commit message
(`docs(round): close <round-id>`). Do not commit unless the project's conventions say rounds end
in a commit and the user has authorized committing.

End with the **next-step reminder** (so the reply is preserved byte-exact, not re-typed by a model):

> Attach the `*.review.zip` bundle to the external reviewer and paste the one-line prompt the
> bundle command printed. To ingest the reply, save it **in a separate shell**: `cat > /tmp/review.md`
> → paste → `Ctrl-D`. Then run `/jahns-workflow:review <round-id>`, which copies `/tmp/review.md`
> verbatim into the reviews dir (no model retyping) and cross-checks it against the bundle.

## Step 6 — Refresh the re-entry pointer

**OVERWRITE** the project's persistent re-entry file so the next session — or a post-compaction
resume — picks up the live frontier without you re-explaining "where were we". Get its path:

```bash
uv run <plugin-root>/scripts/jw.py resume --start-here-path .
```

Then **Write** that file (overwrite — never append), **≤ ~35 lines / ~2.5KB**:

- first line: `# re-entry @ <round-id> / HEAD <short-sha>`
- then the live frontier: what just landed, the open decision / next probe and **why**, the active
  lane(s) — with detail **linked** to PROGRESS / topic files, not inlined.

This replaces the old habit of growing a "START HERE" blob inside the agent-memory `MEMORY.md`
(which accumulates unbounded). The SessionStart hook injects this file automatically each session;
it is reset every round, so keep it short and current. Authoritative state stays in
tasks.yaml / PROGRESS — this is only a pointer.
