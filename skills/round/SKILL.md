---
name: round
description: This skill should be used when the user runs "/jahns-workflow:round", says to "close the round", "wrap up this round", "finish the work cycle", or when an autonomous work round (implement → verify → push) reaches its end and the project CLAUDE.md mandates round closeout. Updates the task registry, PROGRESS, roadmap, SSOT views, and prepares the external review packet (a domain brief + a repo zip by default).
argument-hint: "[round-slug] e.g. lstream-seams"
---

# jahns-workflow: round

Close the current work round: bring the task registry up to date, record the round in
PROGRESS, refresh generated views, and prepare the external review packet (by default a domain
review brief the user pairs with a `.git`-inclusive repo zip).

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

## Step 4 — Prepare external review

**First, a hard push gate (all modes):** run `uv run <plugin-root>/scripts/jw.py remote verify .`.
A review must point at a pushed commit; if this exits non-zero, STOP and tell the user to push
the round's commits before a packet/cycle is created — do not emit a packet for an unpushed HEAD.

**The reviewed tree is the current HEAD.** If your conventions end a round in a commit, commit the
closeout (`docs(round): close <round-id>`) and push it FIRST so `tasks.yaml` / PROGRESS carry the
round's final state — the brief's "Reviewed HEAD" and the reviewer's `git rev-parse HEAD` must agree.

**Packet mode — `raw-zip` (`review.mode: packet`, the default; `review.packet_transport: raw-zip`).**
The reviewer is a *domain* reviewer who gets the whole repo (incl. `.git`) and runs git directly —
not a workflow auditor reading a sandboxed bundle.

1. Confirm the working tree is clean and HEAD is the pushed round commit (`git rev-parse HEAD`
   should equal `git rev-parse @{upstream}` — the Step-4 push gate already fetched). The brief's
   `Reviewed HEAD` is this SHA; do not let HEAD move after you stamp it (the zip below must be of
   this exact HEAD, so don't `git pull` between stamping and zipping).
2. Write `<reviews_dir>/<round-id>-request.md` from `<plugin-root>/templates/review-request.md`: a
   **briefing**, not a protocol — what changed and *why*, the files to read first, falsifiable
   "claims to attack", evidence pointers (to where logs/PROGRESS already live — do **not** copy
   them into a new dir), known weak spots, and the domain lens. Fill `Reviewed HEAD` / `Diff base`
   from the HEAD above and the round's `base_sha` (in `<reviews_dir>/<round-id>-bundle.yaml`, or
   `(root)` for the first round). Keep the harness out of scope unless the user asks.
3. Tell the user to zip the repo root **including `.git`** and drag-drop it to the reviewer. Suggested
   command (tune the excludes per repo — keep `.git`):
   ```bash
   zip -y -r "../$(basename "$PWD")@$(git rev-parse --short=12 HEAD).zip" . \
     -x './.venv/*' './node_modules/*' './__pycache__/*' './.pytest_cache/*' './.mypy_cache/*' \
        './wandb/*' './runs/*' './checkpoints/*' './data/*' \
        '*.env' './.env*' '*.pem' '*.key' '*id_rsa*' '*.tfstate*' './.aws/*' './.ssh/*'
   ```
   (`-y` stores symlinks as links rather than following them; `.git` is kept on purpose so the
   reviewer can `git log`/`git diff`/`git show`.)
   **⚠ Secrets:** this ships the **whole worktree** (incl. untracked files) **and full `.git`
   history** to the external reviewer — the excludes above are size/secret hygiene, not a guarantee.
   Any secret ever committed stays in history even if later removed from HEAD. Tell the user to skim
   `unzip -l <zip>` before upload, and if history was ever cleaned (or they can't risk it) to use
   `strict-bundle` instead — it ships only the tracked HEAD tree, never `.git` or untracked files.
4. One-time per project: the ChatGPT Project holds the **loose** reviewer kit
   (`/jahns-workflow:reviewer-kit` → paste `REVIEWER_INSTRUCTIONS.md`). Optionally add a repo-local
   `docs/review-profile.md` (from `<plugin-root>/templates/review-profile.md`) for the domain lens.
5. Give the user the attach-and-prompt line, e.g.:
   > 첨부한 repo zip을 풀고 `.git`으로 HEAD/diff를 직접 확인한 뒤
   > `docs/reviews/<round-id>-request.md`를 읽고 major 위주로 도메인 리뷰해줘.

**Packet mode — `strict-bundle` (only when `review.packet_transport: strict-bundle`).** Build the
SHA-pinned self-contained bundle instead (provenance-gated review, or when the reviewer cannot run
git): `uv run <plugin-root>/scripts/jw.py review bundle . --round <round-id>` builds `repo/`
directly from git objects of HEAD (exact tracked tree — no `.git`/caches/secrets; symlinks stored as
their target string), adds `__review__/DIFF.patch` + `CHANGED_FILES.txt` + `COMMITS.txt` + a
schema-validated `MANIFEST.yaml`, stamps the reviewed head into the sidecar, and writes the zip to
`<reviews_dir>/bundles/`. Attach it and paste the printed prompt. The ChatGPT Project then needs the
**strict** kit (`/jahns-workflow:reviewer-kit` rendered with `--strict`).

**PR mode** (`review.mode: pr`): the `@codex` PR bot reviews on the PR, but the macro reviewer
(GPT) also lost repo browsing and needs the strict bundle — so its ChatGPT Project must hold the
**strict** kit (`/jahns-workflow:reviewer-kit --strict`); the loose kit emits no `jw-review-result`
marker and the merge gate would never see the macro result.

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
where the review brief (`<reviews_dir>/<round-id>-request.md`) is — and, in `strict-bundle`/PR
mode, where the `*.review.zip` is — plus a suggested commit message (`docs(round): close
<round-id>`). Do not commit unless the project's conventions say rounds end in a commit and the
user has authorized committing.

End with the **next-step reminder** (so the reply is preserved byte-exact, not re-typed by a model):

> Attach the review artifact to the external reviewer and paste the prompt. In the default
> `raw-zip` flow that is the repo zip (incl. `.git`) + the round brief; in `strict-bundle`/PR mode
> it is the `*.review.zip`. To ingest the reply, save it **in a separate shell**:
> `cat > /tmp/review.md` → paste → `Ctrl-D`. Then run `/jahns-workflow:review <round-id>`, which
> copies `/tmp/review.md` verbatim into the reviews dir (no model retyping) and triages it.

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
