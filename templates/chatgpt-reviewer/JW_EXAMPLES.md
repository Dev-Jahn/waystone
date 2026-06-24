# jahns-workflow Review Examples

These examples clarify classification. They do not replace repository evidence.

## Confirmed major finding

### JW-GPT-001 — Retry path commits the same event twice

- Severity: `major`
- Category: `data-integrity`
- Confidence: `high`
- Task / SSOT: `feat/event-retry`, `§4.2 Exactly-once publication`
- Location: `src/publisher.py` — `Publisher.flush`
- Failure claim: A timeout after the remote write but before the local acknowledgement causes the retry branch to publish the same event a second time.
- Evidence: `flush` retries every timeout with the same payload, but the request has no idempotency key and the local sequence is advanced only after a successful response. The provided timeout test mocks failure before the remote write, so it cannot cover this branch.
- Impact: A normal network ambiguity can create duplicate externally visible events, violating the SSOT's exactly-once contract.
- Required change: Make publication idempotent across ambiguous retries or revise the contract through a decision/ADR.
- Verification: Add a test whose first attempt performs the remote write and then times out; assert that retry produces one externally visible event.

Why this is a finding: it has a reachable path, a contract, code evidence, impact, and a focused verification method.

## Decision point, not a finding

- `DESIGN.md §7` permits both fail-open and fail-closed behavior for an unavailable policy service, while the implementation chooses fail-open. The repository contains no accepted ADR or `decision/...` ruling selecting one behavior. This belongs under `Questions / decision points`, with `decision_required: true`; it is not yet a confirmed implementation defect.

## Residual risk, not a finding

- The archive omits the generated database client and the environment lacks the generator. The schema call sites were inspected statically, but generated compatibility could not be executed. Record this under `Residual risks and unverified areas`; do not claim a generator defect without evidence.

## Suppressed review noise

Do not report “rename this helper,” “split this function,” or “add more comments” unless the current form causes a concrete correctness, operability, or contract failure. Do not report that a branch “could race” without identifying shared state, interleaving, and impact.
