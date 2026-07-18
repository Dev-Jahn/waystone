# Review Request — [[ROUND_ID]]

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: [[PROJECT]]
- Branch: [[BRANCH]]
- Reviewer: [[REVIEWERS]]
- Reviewing: [[REVIEWING_SHA]]   (diff against [[DIFF_BASE]])

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

[[NARRATIVE]]

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: [[REPLY_MODEL]]
effort: high
review-target: [[REVIEW_TARGET]]
request-digest: [[REQUEST_DIGEST]]
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
