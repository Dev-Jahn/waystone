# Waystone 0.13 conventions

## 1. Canonical project frame

`PROJECT_BRIEF.md` is the public project frame and `.waystone.yml` uses `brief:`. It contains
commitments, prototype boundary, non-goals, hypotheses, and open questions. Provisional framing is
valid input for exploration but is never hard acceptance. Owner adoption is recorded only through
`waystone brief adopt --evidence`.

## 2. Canonical CLI

The public groups are `brief`, `run`, `review`, and `status`. `task` and `improve` remain selected
work/advisory utilities. `delegate`, `round`, `ssot`, `lanes`, and `dashboard` are retired groups;
there is no alias or read fallback that converts their failure into canonical success.

## 3. Runs and context

Each run freezes one lifecycle stage: `explore`, `evaluate`, or `promote`. WorkBrief carries semantic
context and item-level provenance; it does not carry harness bookkeeping. A context request is a
no-change waiting state and resumes through a new response/brief/spec/attempt. Stage cannot be
changed after failure to reclassify a result.

In v1, staged execution supports external Codex workers and evaluators only. Although profiles can
declare `in-session`, `subagent`, and `external` execution categories, in-session and subagent
carriers are not implemented. Context-transfer-cost-based routing is also not implemented; the
category declarations are not a claim that all three execution paths are supported.

## 4. Review and progress

Review feedback is immutable claim evidence. Validation establishes validity/failure mechanism;
disposition establishes impact, relevance, and selected action. Only selected dispositions
materialize tasks. Severity alone is not priority or a blocker.

Completed runs publish exactly one typed OutcomeDelta. Status reads objective, stage, waiting
context, last delta, advisory, and only then audit counts. `no-objective-delta` is honest output,
not omitted progress.

## 5. Hooks and agents

SessionStart reads the status read model and injects an objective/stage/delta capsule. Re-entry
snapshots contain objective, stage, waiting context, and last delta. The tasks hook protects the
selected-work registry; it does not claim that task count is progress authority. Boundary and
verifier hooks retain only their focused execution-safety contracts.

## 6. Forbidden automatic promotions

- hypothesis → requirement
- confirmed finding → task
- probe → permanent test
- coordinator summary → owner authority

Uncertainty and provenance must survive every surface transition.
