# Reviewer Context

Optional Project Source. This repository uses jahns-workflow to manage its development, but the
workflow is **not** the review target unless the request explicitly asks for a workflow review.

Where to look:

- `docs/reviews/<round-id>-request.md` — the per-round review brief. Start here.
- `docs/review-profile.md` — this project's standing domain review priorities, if present.
- `DESIGN.md` or the project's SSOT — the design / theory / specification source of truth.
- `tasks.yaml`, `PROGRESS.md` — task registry and progress log; useful for evidence pointers.
- `docs/adr/` — accepted design decisions, if present.

Review priority:

1. Major correctness or domain errors.
2. Mismatches between the implementation and the design / theory.
3. Test blind spots — places where existing evidence passes while the claim is actually false.
4. Practical failure modes under realistic use (real shapes, devices, data, scale).

Suppress: style-only comments, naming, optional refactors without a failure mechanism, and
workflow/harness complaints unrelated to the requested change.
