# Waystone

Waystone is an intent control plane for agent-assisted development. It preserves the difference
between committed direction, working hypotheses, open questions, staged execution evidence, and
accepted objective progress.

## Canonical surfaces

- `PROJECT_BRIEF.md` is the project-frame authority; `.waystone.yml` uses `brief:`.
- `waystone brief check|show|adopt` checks, reads, or owner-adopts the frame.
- `waystone run start|context|close` runs one typed WorkBrief through an `explore`, `evaluate`, or
  `promote` stage.
- `waystone review ingest|validate|disposition|materialize` keeps review claims separate from
  validation, disposition, and selected work.
- `waystone status` projects objective, active stage, waiting context, OutcomeDelta, advisory, and
  audit counts in that order.

`ideate` is one skill with two modes: framing when no brief exists and realignment when one exists.
Its output is always provisional. Adoption requires the typed `waystone brief adopt` gate.

## Authority boundaries

The worker receives semantic context and provenance, not harness bookkeeping. A worker result is a
proposal. Independent evidence is required for evaluate/promote claims. Review findings are claims,
not automatic tasks or progress. Only a confirmed validation and an explicit disposition may
materialize selected work.

The following transitions are never automatic: hypothesis → requirement, confirmed finding → task,
probe → permanent test, and coordinator summary → owner authority.

## Installation

Install the plugin for the host you use, then invoke the matching skill (`/waystone:init` or
`$waystone:init`). The CLI launcher is available as `bin/waystone` and `bin/waystone-codex`.

## Verification

```bash
uv run scripts/tests/run_tests.py
```

The suite intentionally contains the preserved trust kernel and focused 0.13 control-plane tests;
retired delegate/round/SSOT compatibility tests are not part of the suite.
