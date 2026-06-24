# jahns-workflow Review Output Contract

Return the following sections in this exact order. Do not add a preamble or closing note outside them.

# Review Result

## Review identity

- Protocol: `jw-chatgpt-reviewer/v1`
- Bundle schema: `<manifest schema>`
- Project: `<project>`
- Round: `<round_id>`
- Base: `<full base_sha or none>`
- Reviewed HEAD: `<full head_sha>`
- Review mode: `packet | pr`
- Review cycle: `<integer or none>`
- Declared package completeness: `complete | partial`
- Verdict: `shipped | shipped-with-risk | not-shipped | intake-failed`
- Decision required: `true | false`
- Finding counts: `<b> blocker / <m> major / <k> minor`

## Executive summary

Two to six sentences. State what was reviewed, the highest-impact conclusion, and the main evidence or limitation. Do not repeat every finding.

## Scope and claim accounting

| Item | Result | Evidence |
|---|---|---|
| `<task, claim, or explicit question>` | `verified | contradicted | partially-verified | not-verified | answered` | `<paths, symbols, anchors, or checks>` |

Include every load-bearing claim and explicit question from `__review__/REQUEST.md`. Add material scope items discovered from the diff when needed.

## Findings

Use one subsection per confirmed finding, ordered by severity and then impact.

### JW-GPT-001 — `<imperative-free defect title>`

- Severity: `blocker | major | minor`
- Category: `correctness | security | data-integrity | concurrency | compatibility | performance | reliability | test-gap | documentation | workflow`
- Confidence: `high | medium`
- Task / SSOT: `<task IDs and section anchors, or none>`
- Location: `<path>` — `<symbol, heading, schema field, or stable locator>`
- Failure claim: `<precise behavior or invariant that fails>`
- Evidence: `<concrete execution path, contradiction, reproducer, or check result>`
- Impact: `<affected behavior and realistic consequence>`
- Required change: `<minimum required outcome; avoid gratuitous redesign>`
- Verification: `<focused test or inspection that would prove resolution>`

Continue with `JW-GPT-002`, etc. If there are no confirmed findings, write exactly:

`None.`

## Questions / decision points

List only unresolved matters that require user or SSOT interpretation. Each item must state why code evidence cannot decide it and what choices have different consequences. If none, write `None.`

## Residual risks and unverified areas

List package omissions, unavailable environments, checks not run, or bounded risks that remain after review. Distinguish these from confirmed findings. If none, write `None.`

## Checks performed

| Check | Result | Notes |
|---|---|---|
| `<command or static inspection>` | `pass | fail | not-run | not-applicable` | `<concise evidence>` |

Never imply that a check ran unless it actually ran in this review session. Pre-recorded implementer checks must be labeled `provided evidence`, not `performed`.

## Machine summary

Append exactly one neutral summary marker outside a code fence. `protocol` is the fixed constant
shown below. Copy the identity fields `project`, `round_id`, `review_mode`, `review_cycle`,
`base_sha` **verbatim from `__review__/MANIFEST.yaml`**, and set `reviewed_sha` from the manifest's
`head_sha` — ingest cross-checks all of these and rejects a reply whose identity does not match the
bundle:

<!-- jw-review-summary:v1
protocol: jw-chatgpt-reviewer/v1
reviewer: gpt-5.5-pro
project: <manifest.project>
round_id: <manifest.round_id>
review_mode: <manifest.review_mode>
review_cycle: <manifest.review_cycle — integer or none>
base_sha: <manifest.base_sha — full sha or none>
reviewed_sha: <manifest.head_sha>
verdict: <shipped|shipped-with-risk|not-shipped|intake-failed>
decision_required: <true|false>
blocker: <integer>
major: <integer>
minor: <integer>
-->

When `review_mode: pr` and the manifest supplies a review cycle, append this second marker immediately after the neutral marker, also outside a code fence. Its fields and verdict values are compatible with the existing PR merge gate. **`decision_required` here is a YAML LIST** (the merge gate requires a list — a boolean is rejected): `[]` when no decision is required, otherwise a list of the blocking decision IDs.

<!-- jw-review-result:v1
reviewer: gpt-5.5-pro
review_cycle: <integer>
reviewed_sha: <full head_sha>
verdict: <shipped|shipped-with-risk|not-shipped>
decision_required: []
-->

Do not emit `jw-review-result` for packet mode or for `intake-failed`.
