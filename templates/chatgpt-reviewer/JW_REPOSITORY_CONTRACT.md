# jahns-workflow Repository Contract

This document defines how an external reviewer interprets repositories managed by `jahns-workflow`. It describes workflow semantics, not current repository state.

## 1. Canonical records

| Record | Conventional path | Review meaning | Authority notes |
|---|---|---|---|
| Workflow configuration | `.jahns-workflow.yml` | Resolves actual paths and review mode | Read first; it overrides conventional path assumptions |
| Design SSOT | normally `DESIGN.md`, otherwise `ssot:` | Binding design/theory contract, but falsifiable by evidence | Cite by section anchor or heading, not mutable line number |
| Task registry | `tasks.yaml` | Canonical task IDs, titles, dependencies, status, round, severity, origin, and SSOT anchors | Current task state comes from here |
| Roadmap | `ROADMAP.md` | Generated projection of `tasks.yaml` | Never treat it as an independent source of truth |
| Progress log | `PROGRESS.md` | Round history, checks claimed, decisions pending, and next-step narrative | Historical evidence and pointers; not canonical task status |
| Decisions | configured ADR directory | Accepted design decisions and SSOT amendments | Check ADR status and affected anchors |
| External reviews | configured reviews directory | Request, verbatim feedback, and triage history | A prior reviewer statement is not automatically a real defect |
| Generated SSOT views | configured generated directory | Index, digest, and split views for navigation | Read-only derivatives; canonical text remains the SSOT |
| Tests/checks | repository-specific | Executable evidence about behavior | A passing check proves only the behavior it actually covers |

If a configured path and a conventional path disagree, use the configured path and disclose the discrepancy only when it affects review reliability.

## 2. One-home-per-fact model

- Design requirements belong in the SSOT and accepted ADRs.
- Task identity and current status belong in `tasks.yaml`.
- A readable plan view belongs in generated `ROADMAP.md`.
- Round history and evidence pointers belong in `PROGRESS.md`.
- External feedback belongs in the reviews directory.

Duplicated summaries are navigation aids. Resolve contradictions against the canonical home above.

## 3. Task semantics

Task IDs use `<type>/<kebab-slug>`, where the type is normally one of:

`feat`, `fix`, `perf`, `gate`, `spike`, `decision`, `docs`, `chore`.

Typical lifecycle:

`pending -> active -> done`, with `blocked` and `dropped` as side states.

Interpretation rules:

- `done` is a workflow claim. Confirm the relevant acceptance evidence when the task is in review scope.
- A `gate/...` task is complete only when the stated bar passed and evidence is available.
- A `decision/...` task represents an unresolved or recorded ruling. Do not convert an interpretation question into an implementation defect without checking its `ruling:` and related ADR.
- `deps` describe declared dependencies, but code may contain undeclared coupling. Review the actual dependency surface.
- `anchor` points to the governing SSOT section and should guide design checks.
- `origin` can connect a task to an earlier review; it does not prove that the earlier finding was correct.

## 4. Round semantics

A round is an autonomous work cycle, normally named `YYYY-MM-DD-<slug>`, that may ship several tasks before external review.

For review purposes:

- The round request defines intended scope.
- `base_sha..head_sha` defines the changed commit range.
- The packaged HEAD defines the reviewed implementation tree.
- A later commit is not covered merely because it belongs to the same branch or round name.
- A docs-only closeout commit and a load-bearing implementation commit must not be conflated. Trust the manifest's explicit review target and comparison range.

## 5. SSOT interpretation

The SSOT is binding but falsifiable.

When implementation and SSOT disagree, classify the situation before prescribing a fix:

1. **Implementation defect** — the SSOT is clear and internally coherent, and code violates it.
2. **SSOT defect** — independent evidence shows the documented rule is wrong or contradictory.
3. **Decision required** — two defensible readings or an unresolved trade-off exist.
4. **Stale derivative** — generated views or summaries are out of date while the canonical record is clear.

Do not silently demand compliance with an evidently wrong specification, and do not silently excuse divergence from a valid one. Put unresolved cases under `Questions / decision points` and set `decision_required: true` when they block a verdict.

## 6. Severity semantics

Use the repository's shared scale:

- `blocker` — unsafe for downstream work to consume; causes severe correctness, security, data-integrity, or release-gate failure in realistic conditions.
- `major` — confirmed functional defect or material hazard that should be resolved within the current milestone or before shipping the affected capability.
- `minor` — confirmed limited defect, misleading contract, or narrowly scoped maintainability/test gap; not a style preference.

Severity describes impact and urgency, not confidence. Low-confidence concerns are not findings.

## 7. Review-mode metadata

- `packet` mode uses an attached, SHA-pinned review bundle and later ingests the reply verbatim.
- `pr` mode additionally binds a review cycle to a PR head/base and may require a `jw-review-result` marker.

The current bundle manifest determines which mode and marker rules apply.
