---
name: init
description: This skill should be used when the user runs "/waystone:init" in Claude Code or "$waystone:init" in Codex, asks to "initialize the workflow harness", "set up waystone", "adopt the workflow in this project", or "re-sync the workflow setup". One-click setup for new projects and non-destructive retrofit for projects already in progress.
argument-hint: "[ssot-path] (optional — skips SSOT detection)"
disable-model-invocation: true
---

# waystone: init

## Host contract

- Claude Code: invoke `/waystone:init`; assign `$CLAUDE_PLUGIN_ROOT` to
  `WAYSTONE_PLUGIN_ROOT`, then run command examples with `waystone` from `PATH`.
- Codex: invoke `$waystone:init`; from this skill's directory walk up two parents, assign that
  absolute path to `WAYSTONE_PLUGIN_ROOT`, then run command examples with
  `$WAYSTONE_PLUGIN_ROOT/bin/waystone-codex`.
- Resolve plugin resources from `$WAYSTONE_PLUGIN_ROOT`. Ask required choices through the host's native
  user-interaction mechanism; never require a specifically named question tool.

Set up (or repair) the waystone harness in the current project. Greenfield projects get
the full structure; in-progress projects are retrofitted **non-destructively**: existing docs,
codenames, and commit history are never rewritten — the convention applies from now on only.

Shared resources: `$WAYSTONE_PLUGIN_ROOT/references/conventions.md` and
`$WAYSTONE_PLUGIN_ROOT/templates/*`.

## Step 0 — Preconditions and mode

1. Verify the project is a git repository (`git rev-parse --git-dir`). If not, ask the user whether to `git init` first; do not proceed without git.
2. If `.waystone.yml` or legacy `.jahns-workflow.yml` already exists → **repair mode**: skip to Step 5 and re-run Steps 5–9 idempotently, reporting anything that drifted (missing dirs, stale generated views, missing host instruction stanza, unregistered project). The first `waystone` command renames the legacy config before reading it.

## Step 1 — Detect the existing structure

Scan before creating anything:

- **SSOT candidates**: argument if given; else root-level and `docs/` markdown whose name suggests design/theory/spec/SSOT, plus any root .md over ~200 lines that is not README/PROGRESS/CLAUDE/AGENTS/ROADMAP. Note size and headings of each candidate.
- **Existing homes**: PROGRESS-like log files, ADR directories (any naming), review/feedback files,
  docs layout, and the current host instruction file (`CLAUDE.md` or `AGENTS.md`).
- Read `$WAYSTONE_PLUGIN_ROOT/references/conventions.md` to have the target model in mind.

## Step 2 — Confirm SSOT and consent defaults

Ask the user which file is the SSOT — listing detected
candidates with size/role, plus options "no SSOT yet — create an SSOT.md skeleton" and
"this project has no single design doc" (then SSOT features are disabled: omit `ssot:` from
config; everything else still works). Map all other detected structures automatically and
report the mapping instead of asking.

Then ask two separate host-native consent questions:

1. Start level: `warn-allowed` (default; preserve warning behavior) or `observe-only`
   (warnings may be promoted only through the existing replay gate).
2. Delegation worktree/runner: enabled (default) or disabled. This controls
   `delegation.enabled`; disabled means `waystone delegate run` fails loud.

Do not infer either choice from repository contents. After writing `.waystone.yml` in Step 3,
record both choices in the standard local consent log:

```bash
waystone consent record init.start-level <observe-only|warn-allowed> \
  --context start_level=<observe-only|warn-allowed> --root <project-root>
waystone consent record init.delegation <enabled|disabled> \
  --context enabled=<true|false> --root <project-root>
```

**Adapt config to the repo, not the repo to the config**: if ADRs/reviews/progress already
live somewhere, point the config at the existing paths. Only create what is missing. Moving
existing files is allowed only when the user confirms it and history stays intact (`git mv`).

## Step 3 — Write the harness files

1. `.waystone.yml`:

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
review:
  mode: packet                  # packet (hand the change to a web reviewer) | pr (SHA-bound PR review cycles)
  reviewers: [role:reviewer]
  require_ci: false             # if true, the merge gate blocks until CI passes
  # operators: []               # PR mode: extra GitHub logins trusted to post review markers (owner always is)
  # approvers: []               # PR mode: extra GitHub logins trusted to post the final approval
policy:
  start_level: warn-allowed     # observe-only | warn-allowed (use the recorded Step 2 choice)
delegation:
  enabled: true                 # use the recorded Step 2 choice
  env_prep: null                # null = lockfile auto-detection
state:
  last_round_commit: null
```

Ask the user which `review.mode` fits: `packet` (default — close a round, push, then hand a web
reviewer the round's request markdown; the reviewer reads the repo over git and reviews for domain
validity) or `pr` (open a PR per round, freeze a SHA-bound review cycle, and let a deterministic gate
guard the merge; suits repos that already work through PRs with a `@codex` bot).

2. `tasks.yaml` — minimal valid registry (`version: 1`, `project:`, `milestones: []`, `tasks: []`),
   with a YAML comment documenting the optional task fields (`deps`, `milestone`, `round`,
   `anchor` — §-anchor of the SSOT section the task governs — `severity`,
   `origin`, `branch`, `notes`, `ruling` — the user's decision on a `decision/...` task,
   `result` — a recorded measurement/outcome, `scope` — repo-relative path-prefix list maintained
   with `waystone task set <id> --scope-add`, `lane` — `{branch, base_sha, depends_on}` for
   parallel worktree lanes, verified by `waystone lanes verify`).
3. Missing directories for adr/reviews/progress-archive; `docs/CONVENTIONS.md` as a verbatim copy of `$WAYSTONE_PLUGIN_ROOT/references/conventions.md`; an ADR-0000 from `$WAYSTONE_PLUGIN_ROOT/templates/adr.md` recording "adopted waystone" (so the numbering and format are established by example).
4. If no PROGRESS file exists, create one with a one-line header pointing at tasks.yaml/ROADMAP.

## Step 4 — Seed the task registry (brownfield only)

If a PROGRESS/TODO registry with open items exists, offer to convert open items into
`tasks.yaml` entries with proper new IDs and explanatory titles (old codenames go into
`notes:` for traceability, e.g. `notes: "was E9"`). Do not touch closed/historical items.

## Step 5 — Generate views

Run (always safe, idempotent):

```bash
waystone ssot split .   # only if config has ssot:
waystone ssot digest .
waystone roadmap .
waystone validate tasks.yaml
```

## Step 6 — Host instruction stanza

For Claude Code, insert `$WAYSTONE_PLUGIN_ROOT/templates/claude-md-stanza.md` into project `CLAUDE.md`.
For Codex, insert `$WAYSTONE_PLUGIN_ROOT/templates/agents-md-stanza.md` into project `AGENTS.md`. Create the
selected file if absent and substitute `{SSOT_PATH}`/`{GENERATED_DIR}`. Recognize and replace both the legacy
`<!-- jahns-workflow:begin -->` … `<!-- jahns-workflow:end -->` block and the current
`<!-- waystone:begin -->` … `<!-- waystone:end -->` block (the template's begin marker may carry an
annotation). Always write the new `waystone` markers, never the legacy markers, and never duplicate
the managed block. Do not touch anything outside the markers. If the selected host instruction file
currently carries a running status log (acting as a de-facto PROGRESS), propose moving that content
into PROGRESS.md and leaving a pointer — show the user the move before applying it.

## Step 7 — Reorganize Claude Code agent memory

In Codex, skip this step; do not reorganize Codex memory. In Claude Code, check
`~/.claude/projects/<dash-escaped-project-path>/memory/` — the directory name is the
absolute project path with `/` (and other separators) replaced by `-`, e.g.
`/home/u/work/proj` → `-home-u-work-proj`; when in doubt, glob `~/.claude/projects/*<repo-name>*/memory/`. For each memory file that
duplicates repo-derivable state (progress snapshots, task lists, design summaries): move any
non-derivable facts into the repo (PROGRESS or docs), then slim the memory to a pointer plus
those facts that genuinely belong in memory (environment gotchas, user preferences). Update
MEMORY.md index lines accordingly. Show a summary of what was slimmed. Never delete
environment/user-preference memories.

## Step 8 — Register the project

Register the project through the CLI (idempotent; do not edit `projects.json` directly):

```bash
waystone project register <project-root>
```

This feeds `/waystone:status` in Claude Code or `$waystone:status` in Codex.

## Step 8.5 — Optionally install managed agents or hooks

Ask one host-native question offering the managed project agent, the project boundary hooks, both,
or neither. This is optional and must not change the result of initialization when declined. For
each offered surface, display this preview **before** asking for or recording consent:

- target path (`.claude/agents/waystone-operator.md` for agents or `.claude/settings.json` for hooks);
- effect (which managed agent or boundary hook becomes available);
- rollback (delete that exact installed file; no other project file is changed by rollback).

Only after the user has seen the target path, effect, and rollback, record consent and install:

```bash
waystone consent record install.agents accept --context kind=agents --root <project-root>
waystone install agents --root <project-root>

waystone consent record install.hooks accept --context kind=hooks --root <project-root>
waystone install hooks --root <project-root>
```

The install commands refuse to overwrite an existing target. Every installed managed file is left
uncommitted together with the rest of init output; do not use consent or installation as permission
to commit it.

## Step 9 — Report

Leave all changes **uncommitted** for user review. Report in the user's configured language:
what was created vs adapted, the config mapping, memory changes, any managed agent/hook consent and
uncommitted install, and next steps (commit
suggestion `docs: adopt waystone harness`; start working; close rounds with
`/waystone:round` in Claude Code or `$waystone:round` in Codex). Generated document content (PROGRESS, ADR-0000) is written in the
user's configured response language; `docs/CONVENTIONS.md` stays a verbatim copy.
