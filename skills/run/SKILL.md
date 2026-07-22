---
name: run
description: Orchestrate the canonical one-task run lifecycle through WorkBrief, context transfer, execution, close, and report.
argument-hint: "<task-id>"
allowed-tools: ["Bash", "Read", "Write"]
---

# waystone: run

Thin orchestration only; the CLI and runtime store own authority.

1. Propose `explore`, `evaluate`, or `promote` from the request and current brief. Treat an
   ambiguous stage as an owner decision, not a silent downgrade.
2. Assemble a provenance-preserving WorkBrief with objective, why, current state, constraints,
   non-goals, open questions, evidence expectations, and context-transfer routing. Do not put
   Waystone bookkeeping in the worker prompt.
3. Start with the typed ingress:

```bash
waystone run start <task-id> --work-brief <file> [--owner-request <file>] [--stage <stage>]
```

4. If the run requests context, show it with `waystone run context show` and resume only after a
   typed response with `waystone run context provide`. Do not patch an existing attempt or reset
   its budget.
5. Close only with the frozen lineage and typed outcome:

```bash
waystone run close <run-id> --outcome <file>
```

Report the stage, evidence, OutcomeDelta, waiting context, and any refusal. Never turn a failed
promotion into explore success.
