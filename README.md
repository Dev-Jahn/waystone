# waystone

Waystone is a Claude Code plugin for keeping long-running research and software projects coherent.

It gives a project a durable source of direction, a validated task list, bounded work cycles,
independent review, and a way to learn from past Claude Code sessions. It is designed for projects
that span many sessions or agents, where context, decisions, and verification evidence would otherwise
become scattered across chats and memory files.

> **Status:** v0.8.2 is implemented. v0.9 is the planned step toward user- and project-specific
enforcement and larger-scale multi-agent orchestration.

<!-- IMAGE PLACEHOLDER: docs/images/overview.png
Suggested diagram: Idea → Project setup → Tasks → Work or delegate → Round close → Independent review → Improvement.
-->

## Install

Requirements: `git`, `bash`, and [`uv`](https://docs.astral.sh/uv/).

```bash
/plugin marketplace add Dev-Jahn/jahns-cc-marketplace
/plugin install waystone
```

Restart Claude Code after installation so the hooks load.

For local development:

```bash
claude --plugin-dir ~/workspace/waystone
```

## Quick start

For a new or half-formed project:

```text
/waystone:ideate "one-line project idea"
/waystone:init
```

`ideate` turns the idea into `SSOT.md`, a concise project-direction document. `init` then
builds the working structure around it.

For an existing project, start with:

```text
/waystone:init
```

Setup is non-destructive: Waystone adapts to existing files, leaves changes uncommitted for review,
and applies its conventions only from that point forward.

A normal cycle is:

```text
choose or add a task
        ↓
work directly or /waystone:delegate
        ↓
/waystone:round
        ↓
independent review of the pushed code
        ↓
save the reply to /tmp/review.md
        ↓
/waystone:review
        ↓
fix confirmed issues and start the next round
```

Run `/waystone:improve` periodically to analyze past sessions and review results.

## What it does today

| Command | Purpose |
|---|---|
| `/waystone:ideate` | Turns a rough idea into `SSOT.md`, a concise project-direction document. No repository is required. |
| `/waystone:init` | Sets up a new project or adds Waystone to an existing one without rewriting its history. |
| `waystone task ...` | Adds, updates, lists, and archives tasks through a validated command-line interface. |
| `/waystone:delegate` | Runs one task in an isolated worktree, optionally verifies the result independently, then asks you to apply or discard it. |
| `/waystone:round` | Closes a bounded work cycle, updates progress, refreshes generated views, and creates a review request. |
| `/waystone:review` | Preserves a reviewer reply exactly, verifies each issue, and turns confirmed issues into tasks. |
| `/waystone:status` | Shows active, blocked, and pending work across registered local or remote projects. |
| `/waystone:improve` | Analyzes Claude Code history and review evidence, then proposes evidence-backed workflow improvements. |

An initialized project keeps:

- the main project direction in one document when the project has one;
- active work and dependencies in `tasks.yaml`;
- a generated visual roadmap in `ROADMAP.md`;
- recent work-cycle history in `PROGRESS.md`;
- decisions in `docs/adr/`;
- review requests and feedback in `docs/reviews/`.

On session start or resume, Claude Code receives a short operating summary, the project digest,
active tasks, and the next useful action. Before compaction or exit, Waystone stores a small re-entry
note so a later session can continue without reconstructing the whole context.

Most validation, rendering, bookkeeping, log parsing, and policy checks are plain scripts and do not
spend model tokens.

## Rounds and independent review

A **round** is a bounded cycle of implementation, verification, push, and review. Closing one updates
task status and progress, refreshes generated views, checks that the reviewed commit is pushed, and
writes a short review request naming the important files, claims, evidence, and known weak points.

The project chooses one review mode during setup:

- **Packet mode** — the default. Give the generated Markdown request to any capable external reviewer.
- **PR mode** — for pull-request workflows. Review, CI, issue resolution, and final approval are tied
  to the exact commit being merged, so an old review cannot approve a newer push.

Reviewer comments are treated as claims, not facts. `/waystone:review` checks them against the code
before confirmed issues become tracked work.

<!-- IMAGE PLACEHOLDER: docs/images/review-loop.png
Suggested diagram: Work round → pushed commit → independent review → verify findings → next tasks or decisions.
-->

## Delegate a task without losing control

`/waystone:delegate` is for a task with explicit success criteria. Waystone then:

1. fixes the current repository state as an immutable snapshot, including uncommitted work;
2. creates a separate Git worktree and prepares the environment from the project's lockfiles or
   configured setup command;
3. sends the bounded task to the configured external implementer;
4. computes the resulting patch and changed-file list directly from Git;
5. keeps the worker's own verification and risk report clearly labeled as a claim;
6. optionally runs a separate read-only verifier;
7. asks you to apply or discard the patch.

The worker and verifier never own final approval. The main session and the user retain that decision.

The current v0.8 runner is Codex-backed, but Waystone stores bindings by responsibility
(`implementer`, `verifier`) rather than baking model names into the workflow. Bindings live in
`~/.claude/waystone/profile.yml`; Waystone refuses to guess a model when the profile is missing.

<!-- IMAGE PLACEHOLDER: docs/images/delegation-flow.png
Suggested diagram: Task + success criteria → fixed snapshot → isolated worktree → implementer → patch → verifier → user apply/discard.
-->

## Improve the workflow from real usage

`/waystone:improve` reads Claude Code logs from `$CLAUDE_CONFIG_DIR/projects`, or
`~/.claude/projects` when that variable is not set. Extra log directories and project filters are
supported.

It combines session history with review and delegation records, then looks for patterns such as:

- the main session doing large amounts of implementation directly;
- changes with little or no visible verification;
- repeated failed commands;
- very large tool outputs filling the main context;
- how work is delegated;
- recurring review issues and gaps in the available evidence.

Scripts produce repeatable facts first; the model only interprets those facts. Each recommendation
states where it came from and whether it is directly observed or inferred.

The analysis stays under `~/.claude/waystone/` by default. Raw prompts and source files are not copied
into the report. Accept and reject decisions are remembered so later runs focus on new evidence.

For a small, predefined set of recommendations, v0.8 can separately store a project-specific check in
**observation mode**. Observation records when the check would have fired but does not warn or block.
Promoting it to a warning requires a deterministic replay over past evidence and another explicit
command. v0.8 warnings remain non-blocking.

## Roadmap to v0.9

| Version | Main capability |
|---|---|
| **v0.7 — Observe & Advise** | Organize projects, run review-centered work cycles, analyze past sessions, and make evidence-backed recommendations. **Implemented.** |
| **v0.8 — Delegate & Verify** | Run coding tasks through an isolated, reproducible delegation flow; verify results independently; and begin project-specific observation and warning rules. **Implemented; current release.** |
| **v0.9 — Adapt & Enforce** | Separate user-wide and project-specific rules, promote proven checks to enforceable guards with recorded waivers, and support larger parallel task groups. **Planned.** |

By v0.9, the complete loop is intended to work like this:

1. The main session defines the task, boundaries, and success criteria.
2. Waystone assigns implementation, verification, or review responsibilities to configured models or
   external tools.
3. Repeatable runners prepare isolated environments and return structured evidence.
4. Independent review and actual remediation results become the quality signal.
5. `/waystone:improve` proposes user- or project-specific changes to the workflow.
6. Proposed checks are replayed against past evidence to estimate how often they would interrupt work.
7. Useful checks move gradually from observation to warning or enforcement, always with user consent
   and a recorded way to override them.
8. Large, sufficiently independent task groups can be fanned out while the main session remains the
   single owner of cross-task decisions and final approval.

Roles are defined independently of model names. Changing subscriptions or model generations should
require changing role bindings, not redesigning the workflow.

<!-- IMAGE PLACEHOLDER: docs/images/adaptive-workflow.png
Suggested diagram: Plan → Delegate → Prepare → Execute → Verify → Review → Learn → Adjust, with Main retaining final approval.
-->

## Files added to a project

```text
.waystone.yml           project paths and review settings
tasks.yaml              active task registry
tasks.archive.yaml      older completed or dropped tasks
ROADMAP.md              generated dependency graph and task table
PROGRESS.md             recent work-cycle history
docs/CONVENTIONS.md     shared task and review conventions
docs/ssot/              generated design-document index, sections, and digest
docs/adr/               recorded decisions
docs/reviews/           review requests and feedback
CLAUDE.md               a managed Waystone section
```

Personal analysis, delegation records, worktrees, model bindings, and adaptive-rule state live under
`~/.claude/waystone/` rather than in the project repository.

See [references/conventions.md](references/conventions.md) for the full task, decision, and review
conventions.

## Principles

- **Quality before savings:** lower cost and smaller context matter only when the result stays correct
  and well verified.
- **Evidence over "done":** changes, checks, review findings, and resolutions matter more than an
  agent's completion message.
- **Roles over model names:** users choose which model or tool fills each responsibility.
- **Scripts for repeatable steps; models for judgment:** automation handles bookkeeping and
  reproducible execution, while models handle planning and trade-offs.
- **Gradual enforcement:** new rules begin as observations or suggestions and require evidence plus
  user consent before they can block work.
- **Local-first and non-destructive:** personal analysis stays local, and existing project history is
  preserved.

## Recommended global CLAUDE.md to use with plugin

```markdown
# Global Constitution

- Think before acting: state assumptions when they affect implementation.
- Prefer the simplest correct implementation.
- Do not use silent fallback(or any behavior inconsistent with function name) to make a task appear successful.
- Tests are means, not goals: implement tests only if they directly reduce the risk of failure.
- Main session owns task routing, hard decisions, and final acceptance.
- Verification evidence must be recorded before final reporting or round close.
- Don't use internal jargons and explain intuitively when reporting or asking for decision to user.
- Task state lives in `jw task`, generated roadmap/progress, and workflow artifacts.
- Nontrivial implementation should go through `jw delegate` unless explicitly justified.
```

## Development

`main` contains the distributable plugin runtime. Tests and development tooling live on `dev`.

```bash
git switch dev
uv run scripts/tests/run_tests.py
```

License: MIT.
