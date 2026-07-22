---
name: init
description: Initialize a Waystone project around PROJECT_BRIEF.md and the canonical brief/run/review/status surfaces.
argument-hint: "[project-root] (optional)"
allowed-tools: ["Bash", "Read", "Write"]
---

# waystone: init

Initialize the project without inventing product commitments. Read an existing
`PROJECT_BRIEF.md` if present; otherwise create a provisional brief only from information the
user supplied. Preserve uncertainty as hypotheses and open questions.

The canonical project configuration is `.waystone.yml` with `brief: PROJECT_BRIEF.md`. The brief
must remain `status: provisional` until the owner supplies adoption evidence and runs:

```bash
waystone brief adopt <project-root> --evidence <owner-confirmation-file>
```

After the config and brief are ready, register the project through `waystone project register
<project-root>`. Do not edit the machine registry directly. Report the resulting canonical
surfaces and any unresolved questions; do not create generated views or retired workflow records.
