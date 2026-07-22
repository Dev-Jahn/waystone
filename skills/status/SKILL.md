---
name: status
description: Show the objective-first Waystone status read model.
argument-hint: "[project-root] (optional)"
allowed-tools: ["Bash", "Read"]
---

# waystone: status

Run and relay the canonical read model:

```bash
waystone status
waystone status --project <project-root> --json
```

Read in this order: project brief status/current objective, active run stage and waiting context,
latest OutcomeDelta, owner rulings/promotion blockers, and advisory. Task, test, and finding counts
are audit-only. A direction advisory is non-blocking and may suggest `/waystone:ideate`; never
start realignment or create tasks automatically.
