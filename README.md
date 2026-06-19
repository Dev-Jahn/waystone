# jahns-workflow

A Claude Code plugin that harnesses SSOT-anchored agentic development: one global naming
convention across all projects, a machine-validated task registry, zero-token roadmap/digest
generation, round-based progress discipline, external-review ingestion, and scoped SSOT audits.

Generalizes a research-workflow battle-tested on theory-heavy repos, but applies to any
programming project (web, systems, ML) that anchors work on one design document.

## What it enforces (from adoption onward — never retroactively)

- **Task IDs** `<type>/<kebab-slug>` (`feat|fix|perf|gate|spike|decision|docs|chore`) registered in `tasks.yaml` with explanatory titles before first use. Bare codenames (`P0`, `E3`, `Q1`) are rejected by a validator hook.
- **Severities** `blocker > major > minor` on review/audit findings.
- **One home per fact**: SSOT (design), `tasks.yaml` (registry), `ROADMAP.md` (generated view), `PROGRESS.md` (log, auto-archived monthly), ADRs (decisions), reviews dir (external feedback, verbatim).
- **SSOT discipline**: §-anchor citations only; generated section split + INDEX + session-injected DIGEST; "binding but falsifiable" discrepancy rule; bulk-edit quarantine (>100 changed lines → audit before dependent work).

Full convention: [references/conventions.md](references/conventions.md).

## Components

| | |
|---|---|
| `/jahns-workflow:init` | One-click setup; non-destructive retrofit for in-progress projects (incl. agent-memory cleanup, project registration) |
| `/jahns-workflow:round` | Close a work round: sync registry → PROGRESS entry + archive → refresh views → review-request packet |
| `/jahns-workflow:review` | Ingest an external review reply: preserve verbatim → verify each finding → register as tasks |
| `/jahns-workflow:audit` | Scoped, tiered SSOT audit (consistency / independent re-derivation / cheap-oracle checks). Cost cap is wall-clock, not hardware — never runs production suites or expensive builds |
| `/jahns-workflow:status` | Cross-project terminal dashboard (branches, rounds, active/blocked tasks). Registry entries can be local (`path`) or remote (`repo: owner/name`, fetched via `gh api` — for projects not cloned on this machine) |
| `spec-auditor` agent | Independent verifier fanned out by the audit skill |
| SessionStart hook | Injects digest + active tasks on startup/resume/clear/**compact** (capped ~8KB; no-ops in ~30ms in non-initialized projects) |
| PostToolUse hook | On `tasks.yaml` edits only: schema validation (exit-2 feedback) + deterministic `ROADMAP.md` regeneration. ~13ms no-op otherwise |

All rendering/validation is plain Python (`scripts/jw_*.py`, run via `uv`) — zero LLM tokens.
A unified front door `scripts/jw.py` dispatches `jw <group>` (validate/roadmap/ssot/status/remote/review/approve/round).

## Review profiles (v0.2.0)

Set `review.mode` in `.jahns-workflow.yml`:

- **`packet`** (default) — close a round, push, paste a request packet to a web reviewer.
- **`pr`** — SHA-bound review cycles on a GitHub PR, with a deterministic merge gate. A review
  is identified by `(reviewer, cycle, reviewed_sha)`, stored as machine-readable markers in PR
  comments (GitHub is the canonical store, never inferred from filenames). A new push makes a
  cycle stale; a merge is blocked by `jw round merge` unless — at the *current* head — the cycle
  is fresh, CI is ok (if `require_ci`), a fresh Codex review + resolved findings + a macro-reviewer
  result are bound to the head, there are zero open blockers/decisions, and a human approval
  (`jw approve --pr N --sha <head>`) is bound to the head. The gate is computed, never judged.

Both modes share a hard push gate (`jw remote verify`): no review is requested against an
unpushed HEAD. Deterministic core is tested under `scripts/tests/` (`uv run scripts/tests/run_tests.py`).

## Requirements

- `git`, `bash`, [`uv`](https://docs.astral.sh/uv/) on PATH (scripts use PEP 723 inline deps; first run downloads `pyyaml` once).

## Install

```bash
# from a marketplace that lists it
/plugin install jahns-workflow

# or for local development
claude --plugin-dir ~/workspace/jahns-workflow
```

Then in each project: `/jahns-workflow:init`. **Restart Claude Code after install** so hooks load.

## Files a project gains

```
.jahns-workflow.yml      # config: SSOT path, dir mapping, oracles, audit watermark
tasks.yaml               # THE codename registry (validated on every edit)
ROADMAP.md               # generated Mermaid dependency graph + task table (GitHub renders it)
PROGRESS.md              # round log (older months auto-archived to docs/progress/)
docs/CONVENTIONS.md      # verbatim copy of the global convention
docs/ssot/               # generated: sections/ split, INDEX.md, DIGEST.md
docs/adr/  docs/reviews/ # decisions; review request/feedback/audit records
CLAUDE.md                # gains a marker-delimited workflow stanza
```

## Token-overhead design

Skills load only on invocation (descriptions ~60 words each); hooks are command-type
(no LLM); the only recurring context cost is the SessionStart injection (digest capped at
150 lines, task summary at 8 lines per state) — which replaces re-reading a 60–150KB SSOT
after every compaction.
