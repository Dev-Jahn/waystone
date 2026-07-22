---
name: ideate
description: Frame a new project or realign an existing PROJECT_BRIEF.md while preserving uncertainty.
argument-hint: "[one-line idea] (optional)"
allowed-tools: ["Bash", "Read", "Write"]
---

# waystone: ideate

Use one surface with two explicit modes:

- No `PROJECT_BRIEF.md`: framing. Ask one meaningful question at a time, clarify commitments,
  hypotheses, non-goals, and open questions, then write a complete but provisional brief.
- Existing `PROJECT_BRIEF.md`: realignment. Inspect the objective-first status read model, recent
  OutcomeDelta history, repeated no-objective-delta activity, and current brief before asking
  bounded questions. Update the brief only when the answer changes direction, scope, or non-goals.

There is no fixed question count. Stop when remaining questions are implementation detail. Every
result is `status: provisional`; never infer owner adoption from conversation completion. Preserve
uncertainty instead of converting hypotheses or open questions into requirements. The owner must
use the typed gate `waystone brief adopt` to commit the brief.

Never create a second state machine or treat coordinator summaries as authority. Report the
provisional frame, provenance, and remaining open questions.
