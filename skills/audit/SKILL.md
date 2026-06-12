---
name: audit
description: This skill should be used when the user runs "/jahns-workflow:audit", asks to "audit the SSOT", "verify the design doc", "check the spec for errors", or after a bulk SSOT edit triggers the quarantine rule. Runs a scoped, tiered independent verification of the SSOT — verification stays cheap (seconds, not hours); it never runs production test suites or expensive builds.
argument-hint: "[--full] [--budget small|medium|large] (default: changed sections, medium)"
---

# jahns-workflow: audit

Adversarially verify the SSOT with **scoped, tiered** checks. Historical context for why:
spec FATALs tend to enter via single bulk edits, evade the spec's own test ladder, and get
caught only by *independent* re-derivation — but full from-scratch re-implementation does not
scale to long projects or expensive targets (hour-long suites, heavy builds). So: audit only
what changed, escalate depth only where it is cheap, and replace execution with analysis where
it is not. The cost cap is **wall-clock time, not hardware**.

Requires an initialized project with `ssot:` configured. Plugin root = two directories above
this skill's base directory. Use the `jahns-workflow:spec-auditor` agent for all fan-out;
default agent model follows the user's sub-agent policy.

## Step 1 — Scope

- Ensure generated views are fresh (`jw_ssot.py check`, regenerate if stale).
- Read `state.last_audit_commit` from `.jahns-workflow.yml`:
  - Set → scope = SSOT sections overlapping `git diff <watermark> HEAD -- <ssot>` hunks (map line ranges to sections via the INDEX line column).
  - Unset (first audit) → scope = sections referenced by `anchor:` of active/pending tasks, plus sections cited by accepted ADRs. If that resolves to nothing (no anchors set yet), list the section headings from the INDEX and ask the user to pick, or to run `--full`.
- `--full` → all sections.

If the scope is empty, report "nothing changed since last audit" and stop.

## Step 2 — Tiered verification

Run tiers concurrently per section (Agent tool fan-out, parallel where independent).
Budget: small = ~2 agents, medium = ~5, large = ~9.

- **T1 — consistency (always, all in-scope sections).** Contradictions within and across sections, undefined symbols/terms, broken cross-references, normative statements that conflict with an ADR, and **test-ladder blind spots**: "what systematic error would this section's own validation provably NOT detect?" (single-configuration tests, similarity-metric gates insensitive to small systematic biases, missing-state-coverage are the classic ones).
- **T2 — independent re-derivation (in-scope normative sections).** spec-auditor agents re-derive the section's logic/math from the SSOT text alone — **forbidden from reading the implementation** — and flag steps that do not follow, sign/order/ratio ambiguities, and places where two readings produce different results.
- **T3 — oracle execution (ONLY sections listed under `oracles:` in config).** Each oracle entry declares a section anchor, a command, and notes including its expected runtime. The constraint is **cost, not hardware**: a declared oracle may use whatever resources the project has at hand, as long as it is side-effect-free and finishes within its declared budget (seconds to at most ~1 minute). Agents may write a small throwaway reference implementation from the spec text and compare against the declared oracle. **Never** run the production test suite or anything resembling a full build; if a declared oracle runs far past its declared budget, kill it and report that instead of waiting. If no oracles are declared, skip T3 and note which in-scope sections *would* benefit from one.

## Step 3 — Adjudicate and register

Cross-check agent findings (a finding confirmed by one lens and refuted by another needs a
third look). For each confirmed finding: register a task (`fix/` for spec errors, `docs/` for
clarity gaps, `decision/` where the fix needs a user ruling) with `severity:` and
`origin: audit-<date>`.

## Step 4 — Record and report

- Write `<reviews_dir>/audit-<YYYY-MM-DD>.md` (content in the user's configured language; quoted spec text verbatim): scope, tiers run, findings with evidence, sections declared clean.
- Update `state.last_audit_commit` to current HEAD in `.jahns-workflow.yml`.
- Report in the user's configured language: findings by severity (blockers first), sections cleared, and oracle-coverage suggestions. Suggested commit: `docs(audit): SSOT audit <date>`.
