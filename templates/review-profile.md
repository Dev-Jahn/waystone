# Review Profile — {project}

Standing priorities for every external review of this repo. The per-round brief points the
reviewer here, so you write the project's domain lens once instead of repeating it each round.

**Setup:** copy this file into the repo as `docs/review-profile.md` and tune it to the domain.
Delete the example block at the bottom once you have written your own.

## Focus on

- {the domain-critical correctness axis that matters most here}
- {a second axis — an invariant, a regime of validity, a numerical/aliasing/synchronization concern}
- whether reported metrics / tests actually support the claim (a passing test ≠ a correct claim)

## Suppress

- style-only comments, naming, and optional refactors with no failure mechanism
- waystone harness / workflow complaints unless the request asks for a workflow review
- minor docs issues unless they mislead the implementation or the experiments

## Environment notes

{What the reviewer can and cannot reproduce — e.g. GPU-scale training, proprietary data — so
provided logs are treated as evidence to inspect, not as proof.}

<!-- Examples by project type — keep the one that fits, then delete this block:

steno       → model / architecture invariants; loss / objective correctness; data-pipeline
              assumptions; pretraining dynamics and failure interpretation; numerical stability;
              whether reported metrics actually support the claim.

research-cc  → mathematical correctness and proof obligations; CUDA memory model, synchronization,
              aliasing, and lifetime; warp / block / grid assumptions; numerical range and precision;
              CPU/GPU semantic equivalence; whether a test can pass while the kernel is wrong on
              realistic shapes or devices.

newton      → physical assumptions and regimes of validity; dimensional / unit consistency;
              conservation laws and invariants; boundary / initial conditions; singular and limiting
              cases; whether the implementation preserves the intended physical model.
-->
