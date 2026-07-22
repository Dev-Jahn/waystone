---
name: review
description: Preserve review feedback and process claims through validation, disposition, and selected-work materialization.
argument-hint: "<run-id>"
allowed-tools: ["Bash", "Read", "Write"]
---

# waystone: review

Reviews are evidence sensors, not task or progress authorities. Preserve the feedback byte-exactly
and use the canonical commands:

```bash
waystone review ingest <run-id> --file <feedback-file>
waystone review validate <finding-id> --file <validation-file>
waystone review disposition <finding-id> --file <disposition-file>
waystone review materialize <finding-id>
```

Verify claims against the actual run and code. Keep claim, validity/failure mechanism, and
impact/relevance/disposition as separate immutable records. Only a confirmed validation plus a
disposition explicitly selecting `fix-now` or `fix-before-promotion` may materialize selected work
in `tasks.yaml`; severity alone never creates a task or blocker. Do not automatically require a
new review cycle, permanent regression test, ADR, or remediation for every real finding.
