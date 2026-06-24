# jahns-workflow Review Playbook

## Phase 0 — Establish identity and completeness

1. Complete the intake protocol from `JW_INSTRUCTION.md`.
2. Record project, round, base SHA, reviewed HEAD, review mode, and declared omissions. Any `repo/` path listed in `manifest.symlinks` is a symlink whose file content is its link **target string**, not file data — treat it as such.
3. Read `__review__/CHANGED_FILES.txt`, `__review__/DIFF.patch`, and `__review__/CHECKS.yaml` when present.
4. If the archive is partial, identify which conclusions cannot be made. Do not silently treat a partial package as a full repository.

## Phase 1 — Build the change map

From the manifest, request, diff, and task registry, map:

- shipped/touched task IDs;
- acceptance claims and explicit questions;
- changed files, symbols, schemas, migrations, interfaces, and configuration;
- SSOT anchors and ADRs touched;
- direct callers, consumers, serializers, persistence boundaries, and tests likely affected;
- generated files that should agree with canonical inputs.

Use the diff to find the changed surface. Use the full packaged files to understand behavior. A diff-only review is insufficient for stateful, cross-file, or contract changes.

## Phase 2 — Verify workflow and design claims

For each in-scope task or claim:

1. Locate its registry entry and governing SSOT/ADR material.
2. State the invariant or acceptance condition in testable terms.
3. Trace the implementation path that is meant to satisfy it.
4. Inspect the relevant tests and test blind spots.
5. Determine whether evidence supports, contradicts, or cannot establish the claim.

Treat `PROGRESS.md`, request prose, test summaries, and comments as claims. Passing tests are supporting evidence, not a substitute for checking whether the tests exercise the claimed behavior.

## Phase 3 — Inspect implementation risk

Apply only categories relevant to the changed surface:

- correctness and invariant preservation;
- boundary, empty, null, overflow, precision, ordering, and error paths;
- state transitions, retries, idempotency, cleanup, cancellation, and partial failure;
- concurrency, races, locking, atomicity, and stale reads;
- input validation, authorization, trust boundaries, injection, secret handling, and unsafe deserialization;
- API/schema/storage compatibility, migrations, rollback, and mixed-version behavior;
- resource lifetime, leaks, unbounded work, algorithmic regressions, and performance claims;
- observability and failure diagnosability when correctness depends on operations;
- tests that can pass while the implementation remains wrong.

Do not manufacture a checklist finding. Report only reachable, evidenced failures.

## Phase 4 — Expand just enough beyond the diff

Inspect unchanged code when needed to verify:

- callers and callees of changed symbols;
- interface implementations and consumers;
- shared types, schemas, codecs, and migrations;
- configuration defaults and environment branches;
- error conversion and cleanup paths;
- tests or fixtures whose assumptions changed;
- duplicated logic likely to diverge.

Do not turn the round review into an unrelated whole-repository audit. Out-of-scope defects may be reported only when the round directly exposes or materially worsens them; label the scope connection.

## Phase 5 — Execute evidence checks

You may run cheap, local, non-destructive checks when the archive and environment permit it. Prefer the narrowest command that tests the claim.

- Record the exact command, result, and relevant output.
- Do not claim a command ran when it did not.
- Do not require network access, production credentials, external services, destructive migrations, or long suites merely to complete the review.
- When dependencies or environment are unavailable, perform static analysis and list the unexecuted check under residual risk.
- Treat pre-recorded `CHECKS.yaml` entries as implementer evidence. Re-run only when useful and feasible.

## Phase 6 — Form findings

A confirmed finding requires all of the following:

1. **Location** — file path plus symbol, section anchor, schema field, or other stable locator. Snapshot line numbers are optional secondary aids.
2. **Failure claim** — the specific invariant or behavior that fails.
3. **Evidence** — a trace, conflicting contract, reproducible input, command result, or concrete code path.
4. **Impact** — what user, data, system, task, or downstream work is affected.
5. **Required change** — the minimum outcome needed, not an unnecessarily broad redesign.
6. **Verification** — a focused way to prove the correction.
7. **Confidence** — `high` or `medium`. Low-confidence concerns belong under questions or residual risks.

Deduplicate findings by root cause. If one defect appears at several call sites, report one finding and list the affected sites.

## Phase 7 — Decide the verdict

Use these verdicts:

- `shipped` — no confirmed blocker, major, or unresolved decision; no material unverified risk.
- `shipped-with-risk` — no blocker or major and no required decision, but minor findings or explicitly bounded residual risk remain.
- `not-shipped` — at least one blocker or major exists, or a required decision prevents a reliable acceptance judgment.
- `intake-failed` — the review target could not be established or inspected reliably.

Minor findings alone do not force `not-shipped`. A count of zero findings does not force `shipped` when material scope could not be verified.

## Noise filter

Do not report:

- formatting, naming, or style preferences with no concrete defect;
- broad refactors that are merely aesthetically preferable;
- hypothetical security/performance concerns without a reachable path;
- duplicate symptoms of one root cause;
- failures outside the reviewed range that the round neither causes nor materially exposes;
- claims based only on prior chat memory or a prior archive.
