# Waystone 0.13 reference conventions

The project frame is `PROJECT_BRIEF.md`; it is provisional until owner evidence passes
`waystone brief adopt`. Runtime authorities are the typed store, Git facts, content-addressed
artifacts, finding chains, and OutcomeDelta ledger—not a generated global document.

Use `waystone brief`, `waystone run`, `waystone review`, and `waystone status` for public work. A run
freezes one stage and one semantic WorkBrief. A worker proposes; independent verification and owner
gates decide. Review claim, validation, and disposition remain separate.

In v1, staged execution supports external Codex workers and evaluators only. Profiles may declare
`in-session`, `subagent`, and `external`, but in-session and subagent carriers and
context-transfer-cost-based routing are not implemented. These declarations are not a claim that
all three execution paths are supported.

Task registry entries represent selected work only. Findings, probes, and summaries do not become
tasks or permanent tests automatically. Status counts are audit context; objective progress comes
only from evidence-bound OutcomeDelta records.

The following transitions are forbidden without their typed authority gate: hypothesis to
requirement, confirmed finding to task, probe to permanent test, and coordinator summary to owner
authority. Preserve uncertainty rather than converting it into requirements.
