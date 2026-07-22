# Promoted contracts (0.13 C2)

This is the canonical public contract for the 0.13 surface cutover. The durable project frame is
`PROJECT_BRIEF.md`; the public commands are `brief`, `run`, `review`, and `status`.

| ID | Contract |
|---|---|
| PC-01 | An initialized project has one canonical `ProjectContext`; missing or ambiguous identity is a typed refusal. |
| PC-02 | Project registration writes an opaque `project_id` and preserves the canonical project path and name. |
| PC-03 | `PROJECT_BRIEF.md` remains provisional until `waystone brief adopt`; adoption is an explicit typed gate. |
| PC-04 | WorkBriefs carry semantic objective, stage, constraints, open questions, and provenance; bookkeeping is not worker context. |
| PC-05 | Run progression is stage-scoped: propose, start, provide context, close, and report. No new state machine is introduced at the surface. |
| PC-06 | Review is a typed chain of claim, validation, disposition, and materialization; a finding does not automatically become a task. |
| PC-07 | OutcomeDelta is the progress authority. Canceled runs are not recorded as successful ledger progress. |
| PC-08 | Status is an objective-first read model exposing stage, waiting context, last delta, and typed unknown/degraded state. |
| PC-09 | Session and resume hooks consume the status read model and remain optional, bounded, and honest about unavailable state. |
| PC-10 | Legacy `delegate`, `round`, `ssot`, `lanes`, and `dashboard` groups have no aliases or read fallbacks. |
| PC-11 | Generated `docs/ssot/*` views and retired delegate/round templates are deleted; `SSOT.md` migration remains a separate owner concern. |
| PC-12 | Uncertainty is preserved: hypotheses, confirmed findings, probes, and coordinator summaries do not silently gain stronger authority. |

## Retired contracts

The flat review adapter, round-close task-count gate, delegation verdict schemas, delegate fan-out
workflow, generated SSOT view, and automatic finding-to-task promotion are historical only. They
cannot make a canonical command succeed through compatibility aliases or legacy read fallback.

The trust kernel contracts remain covered by their focused tests; this document records only the
0.13 surface boundary and its authority transitions.
