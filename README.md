# jahns-workflow

A Claude Code plugin for SSOT-anchored development: one task-naming convention across projects, a
validated task registry, generated roadmaps, round-based progress, and an external review loop. Built
for design-doc-driven work (theory-heavy research repos, but any codebase anchored on one spec).

## What it enforces (from adoption onward, never retroactively)

- **Task IDs** `<type>/<kebab-slug>` (`feat|fix|perf|gate|spike|decision|docs|chore`), registered in `tasks.yaml` with a real title before first use. Bare codenames (`P0`, `E3`) are rejected by a hook.
- **Severities** `blocker > major > minor` on review findings.
- **One home per fact**: SSOT (design), `tasks.yaml` (registry), `ROADMAP.md` (generated), `PROGRESS.md` (log), ADRs (decisions), reviews dir (feedback).
- **SSOT discipline**: cite by §-anchor, never line number; read the generated section split + INDEX + injected DIGEST, not the whole file. Evidence that contradicts the SSOT opens a `decision` task and an ADR — not a silent edit.

Full convention: [references/conventions.md](references/conventions.md).

## Components

| | |
|---|---|
| `/jahns-workflow:init` | Set up or retrofit a project — non-destructive; existing history and docs are never rewritten. |
| `/jahns-workflow:round` | Close a round: sync the registry, append PROGRESS, regenerate views, write the review request. |
| `/jahns-workflow:review` | Ingest a reviewer's reply verbatim, verify each finding, register the real ones as tasks. |
| `/jahns-workflow:status` | Cross-project dashboard (branches, rounds, active/blocked tasks); projects can be local or remote. |
| `/jahns-workflow:improve` | Advisory report over your Claude Code history + review evidence: provenance-labeled recommendations you accept or reject (recorded, never auto-applied). |
| `jw task` CLI | Read and mutate the registry without opening the whole file: `list`/`show`, `add`/`set`/`drop` (validated, comment-preserving), `archive` (move old done/dropped tasks to `tasks.archive.yaml`). |
| SessionStart hook | Injects the digest + active tasks on start/resume/compact. No-op outside an initialized project. |
| PreToolUse hook | Redirects a raw read of `tasks.yaml` to the `jw task` CLI; `cat` stays as an escape hatch. |
| PostToolUse hook | Validates `tasks.yaml` on every edit and regenerates `ROADMAP.md`. |

Rendering and validation are plain Python (`scripts/jw_*.py`, run with `uv`) — no LLM tokens. One
front door, `jw <group>`, dispatches them
(`validate/task/roadmap/ssot/status/remote/review/approve/round/lanes/resume`).

## Rounds

`jw round close . --round <id> --done <ids> --touched <ids>` runs the closeout in one step: flips task
status and stamps the round (comment-preserving), validates, regenerates ROADMAP and the SSOT views,
and advances the `last_round_commit` watermark.

Between sessions, the PreCompact/SessionEnd hooks write a re-entry pointer (HEAD, branch, active round,
next-actionable tasks) and SessionStart injects it back, so a fresh or post-compaction session picks up
the frontier without re-explaining. `round close` and `review` also overwrite a short "where am I" note,
reset each round so it can't grow without bound. `jw lanes verify .` checks each task's `lane:` manifest
— that the branch contains its recorded `base_sha` — for parallel worktree lanes.

## Review

Two modes, set by `review.mode` in `.jahns-workflow.yml`. Both share a push gate: no review is requested
against an unpushed HEAD.

**`packet`** (default) — round close writes one markdown request
(`<reviews_dir>/<round-id>-request.md`): what changed and why, files to read first, claims to attack,
evidence pointers, weak spots, the domain lens. The reviewer reads the repo over git (connector, zip, or
clone — the plugin doesn't care how) and reviews for domain validity, not workflow conformance.
`jw review ingest` copies the reply byte-exact — the user saves it to `/tmp/review.md` in a separate
shell, so the model never re-types it — and appends a triage skeleton; the model verifies each finding
against the code before registering it. Per-project review priorities can live in `docs/review-profile.md`.

**`pr`** — SHA-bound review cycles on a GitHub PR with a computed merge gate. A review is identified by
`(reviewer, cycle, reviewed_sha)` and stored as markers in PR comments (GitHub is the source of truth,
never inferred from filenames). `jw review freeze` stamps the PR head as a cycle and posts the `@codex`
request. `jw round merge` passes only when, at the current head: the cycle is fresh, CI is green (if
`require_ci`), a fresh Codex review + resolved findings + a macro-reviewer result are bound to the head,
there are no open blockers or decisions, and a human approval (`jw approve --pr N --sha <head>`) is bound
to the head. The trust policy (reviewers, approvers, CI requirement) is read from the PR base SHA, so a
branch can't relax its own merge rules. The gate is computed, never judged.

## Requirements

`git`, `bash`, and [`uv`](https://docs.astral.sh/uv/) on PATH. Scripts use PEP 723 inline deps (the first
run fetches `pyyaml` once).

## Install

```bash
/plugin marketplace add Dev-Jahn/jahns-cc-marketplace
/plugin install jahns-workflow                   # from a marketplace listing it
claude --plugin-dir ~/workspace/jahns-workflow   # or for local dev
```

Then run `/jahns-workflow:init` in each project. Restart Claude Code after install so the hooks load.

## What a project gains

```
.jahns-workflow.yml      config: SSOT path, directory mapping, review mode
tasks.yaml               the task registry (validated on every edit)
tasks.archive.yaml       old done/dropped tasks, moved out by `jw task archive`
ROADMAP.md               generated Mermaid graph + task table
PROGRESS.md              round log (older months auto-archived under docs/progress/)
docs/CONVENTIONS.md      verbatim copy of the global convention
docs/ssot/               generated section split, INDEX.md, DIGEST.md
docs/adr/  docs/reviews/  decisions; review requests and feedback
CLAUDE.md                gains a marker-delimited workflow stanza
```

The regression suite lives on the `dev` branch (`uv run scripts/tests/run_tests.py`); `main` ships the
runtime only.

## Token cost

Skills load only when invoked. Hooks are plain commands, no LLM. The one recurring cost is the
SessionStart injection (digest capped at 150 lines, task summary at 8 per state), which replaces
re-reading a large SSOT after every compaction.
