<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
  <img alt="waystone" src="assets/logo-light.png" width="440">
</picture>

### Agents forget. Projects shouldn't.

*Prevent intent drift and context loss in long-horizon development*

<p align="center">
<img alt="version" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2FDev-Jahn%2Fwaystone%2Fmain%2F.claude-plugin%2Fplugin.json&query=%24.version&prefix=v&label=version&style=flat-square">
<img alt="Claude Code plugin" src="https://img.shields.io/badge/Claude%20Code-plugin-8A5CF6?style=flat-square">
<img alt="Codex plugin" src="https://img.shields.io/badge/Codex-plugin-111111?style=flat-square">
<img alt="tests" src="https://img.shields.io/badge/tests-429-success?style=flat-square">
<img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
</p>

</div>

Waystone is a Claude Code and Codex plugin that gives a project a durable source of direction, a validated task list, bounded work cycles, independent review, and a way to learn from past agent sessions. It is built for research and software projects that span many sessions or agents — where context, decisions, and verification evidence would otherwise scatter across chats and memory files.

> **Status:** v0.9.0 is implemented — one shared project state across Claude Code and Codex, cross-process locking, and fully autonomous delegation behind harness-enforced acceptance gates. Upgrading from v0.8 migrates local state automatically and losslessly on first run. Next: user- and project-specific enforcement and larger-scale orchestration.

<br>

## Why waystone

- **Direction that survives sessions.** One durable project-direction document, plus a re-entry note written before compaction or exit, so a later session continues without reconstructing context.
- **Evidence, not "done".** Reviewer comments are treated as claims and verified against the code before they become tracked work.
- **Acceptance with evidence.** Delegated work runs in an isolated worktree, and nothing lands without a recorded criterion-by-criterion verdict backed by verification evidence — every decision stays auditable and reversible.
- **Cheap by default.** Most validation, rendering, bookkeeping, and log parsing are plain scripts that spend no model tokens.

<br>

## Install

Requirements: `git`, `bash`, and [`uv`](https://docs.astral.sh/uv/).

Claude Code:

```text
/plugin marketplace add Dev-Jahn/jahns-cc-marketplace
/plugin install waystone
```

Restart Claude Code afterward so the hooks load.

Codex:

```bash
codex plugin marketplace add Dev-Jahn/jahns-codex-marketplace
codex plugin add waystone@jahns-codex-marketplace
```

Restart Codex, then review and trust the installed hooks with `/hooks`. Codex intentionally does not
run untrusted plugin hooks until their exact hash is approved.

<details>
<summary>Local development</summary>

<br>

```bash
claude --plugin-dir ~/workspace/waystone
```

For Codex development, add a local marketplace containing the checkout and install its `waystone`
entry; the repository CI performs this same install smoke test.

</details>

<br>

## Quick start

For a new or half-formed project:

```text
Claude Code: /waystone:ideate "one-line project idea"   # optional
Claude Code: /waystone:init
Codex:      $waystone:ideate "one-line project idea"    # optional
Codex:      $waystone:init
```

`ideate` turns the idea into `SSOT.md`, a concise project-direction document. `init` then builds the working structure around it. For an existing project, run `/waystone:init` (Claude Code) or `$waystone:init` (Codex) alone — setup is non-destructive: Waystone adapts to existing files, leaves changes uncommitted for review, and applies its conventions only from that point forward.

A normal cycle:

```mermaid
flowchart LR
  T[choose or<br/>add a task] --> W[work directly<br/>or delegate]
  W --> R[round]
  R --> V[independent review<br/>of the pushed code]
  V --> F[review verifies<br/>findings]
  F --> T
  F -. periodically .-> M([improve])
  M -. recommendations .-> T
```

```text
Claude Code: /waystone:round    Codex: $waystone:round
cat > /tmp/review.md             # paste the external reviewer's reply, then Ctrl-D
Claude Code: /waystone:review   Codex: $waystone:review
```

Fix confirmed issues and start the next round. Run `/waystone:improve` or `$waystone:improve`
periodically to analyze past sessions and review results.

<br>

## Available commands

| Claude Code | Codex | Purpose |
|---|---|---|
| `/waystone:ideate` | `$waystone:ideate` | Turns a rough idea into `SSOT.md`, a concise project-direction document. No repository required. |
| `/waystone:init` | `$waystone:init` | Sets up a new project or adds Waystone to an existing one without rewriting its history. |
| `/waystone:round` | `$waystone:round` | Closes a bounded work cycle, updates progress, refreshes generated views, and creates a review request. |
| `/waystone:review` | `$waystone:review` | Preserves a reviewer reply exactly, verifies each issue, and turns confirmed issues into tasks. |
| `/waystone:delegate` | `$waystone:delegate` | Autonomously runs one task in an isolated worktree, verifies it, and resolves it with a recorded evidence-backed verdict. |
| `/waystone:status` | `$waystone:status` | Shows active, blocked, and pending work across registered local or remote projects. |
| `/waystone:improve` | `$waystone:improve` | Analyzes host session history and review evidence, then proposes evidence-backed workflow improvements. |

<details>
<summary>What an initialized project remembers, and how a session re-enters it</summary>

<br>

An initialized project keeps the main project direction in one document (when it has one), active work and dependencies in `tasks.yaml`, a generated visual roadmap in `ROADMAP.md`, recent work-cycle history in `PROGRESS.md`, decisions in `docs/adr/`, and review requests and feedback in `docs/reviews/`.

On session start or resume, the host receives a short operating summary, the project digest, active tasks, and the next useful action. Waystone stores a small re-entry note before compaction on both hosts and before Claude Code exits, so a later session can continue without reconstructing the whole context.

Most validation, rendering, bookkeeping, log parsing, and policy checks are plain scripts and spend no model tokens.

</details>

## Internal features

<details>
<summary>CLI surfaces the skills drive for you</summary>

<br>

| Command | Purpose |
|---|---|
| `waystone task ...` | Validated task-registry CLI — adds, updates, lists, and archives tasks without slurping the file. |
| `waystone paths` | Shows the resolved project-state, machine-state, and worktree-cache locations. |
| `waystone project` | Registers, unregisters, and lists projects through the machine-wide registry. |
| `waystone delegate verify` | Re-runs independent read-only verification of a delegation result in its preserved worktree. |
| `waystone delegate verdict` | Records the main session's evidence-backed apply or discard decision before resolution. |
| `waystone overlay` | Stores project-local adaptive checks and manages their observing/warning lifecycle; promotion to warning requires deterministic shadow replay. |
| `waystone check` | Evaluates active overlay rules against the current project state; warnings are visible but never block the host command. |
| `waystone improve evidence` | Deterministically joins review findings and delegation records by task ID into a local evidence log. |

</details>

<br>

## Rounds and independent review

A **round** is a bounded cycle of implementation, verification, push, and review. Closing one updates task status and progress, refreshes generated views, checks that the reviewed commit is pushed, and writes a short review request naming the important files, claims, evidence, and known weak points.

The project chooses one review mode during setup:

- **Packet mode** (default) — give the generated Markdown request to any capable external reviewer.
- **PR mode** — for pull-request workflows. Review, CI, issue resolution, and final approval are tied to the exact commit being merged, so an old review cannot approve a newer push.

Reviewer comments are treated as claims, not facts. The `review` skill checks them against the code before confirmed issues become tracked work.

<br>

## Delegate a task without losing control

The `delegate` skill autonomously closes one task whose success criteria are already recorded or can
be derived exactly from owner-authored project material. Waystone then:

1. selects an actionable task and, when necessary, records owner-derived criteria through `waystone task set <id> --accept-add` before the run;
2. fixes the current repository state as an immutable snapshot, including uncommitted work, then runs the configured implementer in a separate Git worktree;
3. computes the patch and changed-file list directly from Git while keeping the worker's own verification and risk report labeled as a claim;
4. always produces verification evidence — through `waystone delegate verify` when a verifier is bound, or focused commands in the normalized worktree otherwise;
5. records a criterion-by-criterion decision with `waystone delegate verdict`, then applies it or discards it with a recorded `--reason`;
6. reports the result, evidence, warnings, retries, and one pointer to the preserved record and undo information.

The main session owns the routine decision and records every acceptance with cited evidence. The user
audits that record. Worker claims never become facts merely because their report exists, and a missing
worker report never means verification was absent.

The autonomous loop keeps the existing safety rails: one nonterminal delegation per task, required
criteria and valid profile bindings, read-only independent verification, a bounded
`waystone delegate show --failure` diagnostic, at most two run attempts with retry context recorded by
`--note`, an atomic plain patch apply, and permanent records. It does not read the full runner log or
silently substitute a model, verification path, 3-way apply, or stash operation. Recorded override
flags require `--reason`; a main-session blocker override also has to cite direct checks that refute
each blocker.

Human input is reserved for nine cases: criteria cannot be derived without invention; the profile or
binding is unusable; an apply judgment still has an unrefuted blocker; two attempts are exhausted; a
verifier transport fails again after one retry; apply drift is not wholly caused by the current
session; the runner failure is deterministic; warning rules conflict; or you explicitly request a
review. When drift touches your uncommitted work, Waystone never commits or stashes it — it reports the
state and waits. In every other case the main session continues through verdict and apply or discard.

The current runner is Codex-backed, but Waystone stores bindings by responsibility (`implementer`, `verifier`) rather than baking model names into the workflow. Bindings live in the project's uncommitted `{project_root}/.waystone/profile.yml`. A verifier binding normally omits `execution`, allowing Waystone to derive the transport from the current host. Waystone refuses to guess a model when the profile is missing.

<br>

## Improve the workflow from real usage

The `improve` skill reads Claude Code logs from `$CLAUDE_CONFIG_DIR/projects` (or `~/.claude/projects`) and Codex rollouts from `$CODEX_HOME/sessions` (or `~/.codex/sessions`). By default it filters that history to the current project and combines it with the project's review and delegation records (joined deterministically by task ID via `waystone improve evidence`). `--user-wide` is an explicit cross-project mode for user-habit analysis. The skill looks for patterns such as:

- the main session doing large amounts of implementation directly;
- changes with little or no visible verification;
- repeated failed commands;
- very large tool outputs filling the main context;
- how work is delegated;
- recurring review issues and gaps in the available evidence.

Scripts produce repeatable facts first; the model only interprets them. Each recommendation states where it came from and whether it is directly observed or inferred. Project analysis and its accept/reject decisions stay under `{project_root}/.waystone/improve/`; opt-in `--user-wide` analysis stays under `~/.waystone/improve/`. Raw prompts and source files are not copied into the report, and decisions are remembered so later runs focus on new evidence.

For a small, predefined set of recommendations, Waystone can separately store a project-specific check in **observation mode** (`waystone overlay`): it records when the check would have fired but does not warn or block. Promoting it to a warning requires a deterministic replay over past evidence and another explicit command. `waystone check` evaluates the active rules against the current project state; warnings remain visible but never block until the enforcement arc lands.

<br>

## Roadmap

| Version | Main capability | Status |
|---|---|---|
| **v0.7 — Observe & Advise** | Organize projects, run review-centered work cycles, analyze past sessions, and make evidence-backed recommendations. | Implemented |
| **v0.8 — Delegate & Verify** | Run coding tasks through an isolated, reproducible delegation flow; verify results independently; begin project-specific observation and warning rules. | Implemented |
| **v0.9 — Unify & Automate** | Share one project state across Claude Code and Codex with cross-process locking; let the main session run delegation end-to-end behind harness-enforced acceptance gates; scope improve analysis to the project by default. | Implemented — current release |
| **Next — Adapt & Enforce** | Promote proven checks to enforceable guards with recorded waivers, and support larger parallel task groups. | Planned |

<details>
<summary>The intended Adapt & Enforce loop</summary>

<br>

1. The main session defines the task, boundaries, and success criteria.
2. Waystone assigns implementation, verification, or review responsibilities to configured models or external tools.
3. Repeatable runners prepare isolated environments and return structured evidence.
4. Independent review and actual remediation results become the quality signal.
5. `/waystone:improve` proposes user- or project-specific changes to the workflow.
6. Proposed checks are replayed against past evidence to estimate how often they would interrupt work.
7. Useful checks move gradually from observation to warning or enforcement, always with user consent and a recorded way to override them.
8. Large, sufficiently independent task groups can be fanned out while the main session remains the single owner of cross-task decisions and final approval.

Roles are defined independently of model names. Changing subscriptions or model generations should require changing role bindings, not redesigning the workflow.

</details>

<br>

## Principles

- **Quality before savings** — lower cost and smaller context matter only when the result stays correct and well verified.
- **Evidence over "done"** — changes, checks, review findings, and resolutions matter more than an agent's completion message.
- **Roles over model names** — users choose which model or tool fills each responsibility.
- **Scripts for repeatable steps; models for judgment** — automation handles bookkeeping and reproducible execution; models handle planning and trade-offs.
- **Gradual enforcement** — new rules begin as observations or suggestions and require evidence plus user consent before they can block work.
- **Local-first and non-destructive** — personal analysis stays local, and existing project history is preserved.

<br>

## Reference

<details>
<summary>Files added to a project</summary>

<br>

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
CLAUDE.md or AGENTS.md  a host-specific managed Waystone section
```

Uncommitted project state — default improve analysis, delegation records, model bindings, and adaptive-rule state — lives under `{project_root}/.waystone/`. The machine-wide registry, opt-in `--user-wide` analysis, and worktree cache live under `~/.waystone/` (or `$WAYSTONE_HOME`). Use `waystone paths` to show the resolved locations. See [references/conventions.md](references/conventions.md) for the full task, decision, storage, and review conventions.

</details>

## Recommended global CLAUDE.md or AGENTS.md

Pair the plugin with this global constitution:

```markdown
# Global Constitution

- Think before acting: state assumptions when they affect implementation.
- Prefer the simplest correct implementation.
- Do not use silent fallback(or any behavior inconsistent with function name) to make a task appear successful.
- Tests are means, not goals: implement tests only if they directly reduce the risk of failure.
- Main session owns task routing, hard decisions, and final acceptance.
- Verification evidence must be recorded before final reporting or round close.
- Don't use internal jargons and explain intuitively when reporting or asking for decision to user.
- Task state lives in `waystone task`, generated roadmap/progress, and workflow artifacts.
- Nontrivial implementation should go through `waystone delegate` unless explicitly justified.
```

<br>

## Development

`main` contains the distributable plugin runtime. Tests and development tooling live on `dev`.

```bash
git switch dev
uv run scripts/tests/run_tests.py
```

<br>

<div align="center">
<sub>License: MIT.</sub>
</div>
