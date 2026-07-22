---
name: improve
description: Produce evidence-grounded workflow-improvement advice without enforcing it.
argument-hint: "[--source DIR] [--user-wide] (optional)"
---

# waystone: improve

Collect deterministic history and review evidence with the existing `waystone improve` commands,
then present provenance-labeled recommendations. Recommendations are advisory: record explicit
accept/reject decisions, but do not materialize overlays or policy changes automatically.

For realignment evidence, include objective progress, repeated `no-objective-delta` outcomes,
review-remediation activity, and coverage caveats. Do not treat task/test/finding counts as
progress, do not turn hypotheses into requirements, and do not dispatch retired workflows.
