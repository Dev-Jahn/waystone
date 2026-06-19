---
name: init
description: This skill should be used when the user runs "/jahns-workflow:init", asks to "initialize the workflow harness", "set up jahns-workflow", "adopt the workflow in this project", or "re-sync the workflow setup". One-click setup for new projects and non-destructive retrofit for projects already in progress.
argument-hint: "[ssot-path] (optional — skips SSOT detection)"
disable-model-invocation: true
---

# jahns-workflow: init

Set up (or repair) the jahns-workflow harness in the current project. Greenfield projects get
the full structure; in-progress projects are retrofitted **non-destructively**: existing docs,
codenames, and commit history are never rewritten — the convention applies from now on only.

Plugin root = two directories above this skill's base directory. Shared resources:
`<plugin-root>/references/conventions.md`, `<plugin-root>/templates/*`, `<plugin-root>/scripts/jw_*.py`
(run scripts with `uv run`).

## Step 0 — Preconditions and mode

1. Verify the project is a git repository (`git rev-parse --git-dir`). If not, ask the user whether to `git init` first; do not proceed without git.
2. If `.jahns-workflow.yml` already exists → **repair mode**: skip to Step 5 and re-run Steps 5–9 idempotently, reporting anything that drifted (missing dirs, stale generated views, missing CLAUDE.md stanza, unregistered project).

## Step 1 — Detect the existing structure

Scan before creating anything:

- **SSOT candidates**: argument if given; else root-level and `docs/` markdown whose name suggests design/theory/spec/SSOT, plus any root .md over ~200 lines that is not README/PROGRESS/CLAUDE/ROADMAP. Note size and headings of each candidate.
- **Existing homes**: PROGRESS-like log files, ADR directories (any naming), review/feedback files, docs layout, CLAUDE.md.
- Read `<plugin-root>/references/conventions.md` to have the target model in mind.

## Step 2 — Confirm the one decision that matters

Ask the user ONE question (AskUserQuestion): which file is the SSOT — listing detected
candidates with size/role, plus options "no SSOT yet — create a DESIGN.md skeleton" and
"this project has no single design doc" (then SSOT features are disabled: omit `ssot:` from
config; everything else still works). Map all other detected structures automatically and
report the mapping instead of asking.

**Adapt config to the repo, not the repo to the config**: if ADRs/reviews/progress already
live somewhere, point the config at the existing paths. Only create what is missing. Moving
existing files is allowed only when the user confirms it and history stays intact (`git mv`).

## Step 3 — Write the harness files

1. `.jahns-workflow.yml`:

```yaml
version: 1
project: <repo-name>
ssot: <confirmed path>          # omit if no SSOT
progress: PROGRESS.md           # or existing equivalent
adr_dir: docs/adr               # or existing
reviews_dir: docs/reviews
progress_archive_dir: docs/progress
generated_dir: docs/ssot
digest_max_lines: 150
oracles: []                     # sections with cheap executable checks; see audit skill
review:
  mode: packet                  # packet (paste to web reviewer) | pr (SHA-bound PR review cycles)
  reviewers: [codex, gpt-5.5-pro]
  require_ci: false             # if true, the merge gate blocks until CI passes
state:
  last_audit_commit: null
  last_round_commit: null
```

Ask the user which `review.mode` fits: `packet` (default — close a round, push, paste a
request packet to the web reviewer) or `pr` (open a PR per round, freeze a SHA-bound review
cycle, and let a deterministic gate guard the merge). PR mode suits repos that already work
through PRs with a `@codex` bot.

2. `tasks.yaml` — minimal valid registry (`version: 1`, `project:`, `milestones: []`, `tasks: []`),
   with a YAML comment documenting the optional task fields (`deps`, `milestone`, `round`,
   `anchor` — §-anchor of the SSOT section the task governs, used by audits — `severity`,
   `origin`, `branch`, `notes`, `ruling` — the user's decision on a `decision/...` task,
   `result` — a recorded measurement/outcome, `lane` — `{branch, base_sha, depends_on}` for
   parallel worktree lanes, verified by `jw lanes verify`).
3. Missing directories for adr/reviews/progress-archive; `docs/CONVENTIONS.md` as a verbatim copy of `<plugin-root>/references/conventions.md`; an ADR-0000 from `<plugin-root>/templates/adr.md` recording "adopted jahns-workflow" (so the numbering and format are established by example).
4. If no PROGRESS file exists, create one with a one-line header pointing at tasks.yaml/ROADMAP.

## Step 4 — Seed the task registry (brownfield only)

If a PROGRESS/TODO registry with open items exists, offer to convert open items into
`tasks.yaml` entries with proper new IDs and explanatory titles (old codenames go into
`notes:` for traceability, e.g. `notes: "was E9"`). Do not touch closed/historical items.

## Step 5 — Generate views

Run (always safe, idempotent):

```bash
uv run <plugin-root>/scripts/jw_ssot.py split .   # only if config has ssot:
uv run <plugin-root>/scripts/jw_ssot.py digest .
uv run <plugin-root>/scripts/jw_roadmap.py .
uv run <plugin-root>/scripts/jw_validate.py tasks.yaml
```

## Step 6 — CLAUDE.md stanza

Insert `<plugin-root>/templates/claude-md-stanza.md` into the project CLAUDE.md (create the
file if absent), substituting `{SSOT_PATH}`/`{GENERATED_DIR}`. The block is delimited by
`<!-- jahns-workflow:begin/end -->` markers — replace an existing block instead of duplicating
it. Do not touch anything outside the markers. If CLAUDE.md currently carries a running
status log (acting as a de-facto PROGRESS), propose moving that content into PROGRESS.md and
leaving a pointer — show the user the move before applying it.

## Step 7 — Reorganize agent memory

Check `~/.claude/projects/<dash-escaped-project-path>/memory/` — the directory name is the
absolute project path with `/` (and other separators) replaced by `-`, e.g.
`/home/u/work/proj` → `-home-u-work-proj`; when in doubt, glob `~/.claude/projects/*<repo-name>*/memory/`. For each memory file that
duplicates repo-derivable state (progress snapshots, task lists, design summaries): move any
non-derivable facts into the repo (PROGRESS or docs), then slim the memory to a pointer plus
those facts that genuinely belong in memory (environment gotchas, user preferences). Update
MEMORY.md index lines accordingly. Show a summary of what was slimmed. Never delete
environment/user-preference memories.

## Step 8 — Register the project

Add `{ "name": <project>, "path": <abs path> }` to `~/.claude/jahns-workflow/projects.json`
(create `{"projects": []}` if missing; skip if already registered). This feeds
`/jahns-workflow:status`.

## Step 9 — Report

Leave all changes **uncommitted** for user review. Report in the user's configured language:
what was created vs adapted, the config mapping, memory changes, and next steps (commit
suggestion `docs: adopt jahns-workflow harness`; start working; close rounds with
`/jahns-workflow:round`). Generated document content (PROGRESS, ADR-0000) is written in the
user's configured response language; `docs/CONVENTIONS.md` stays a verbatim copy.
