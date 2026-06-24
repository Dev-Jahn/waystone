# Review Request — round {round-id}

> This file is packaged verbatim into the review bundle as `__review__/REQUEST.md` by
> `jw review bundle`. It is a map + a set of falsifiable claims — never a substitute for the
> reviewer inspecting the packaged `repo/` tree and `__review__/DIFF.patch`.

## Identity
- Project: {project}
- Round: {round-id}
- Base: {base full sha, or "(root)" for the first round}
- Reviewed HEAD: {the committed HEAD being bundled — commit the round closeout first so this tree carries the final task statuses + PROGRESS}

## Round objective

{One paragraph: what this round set out to achieve.}

## Tasks and acceptance claims

{One line per shipped task, each a falsifiable acceptance claim + an evidence pointer, e.g.
`- feat/stream-carry — chunked path is fp32-equivalent to the full path for nonzero initial
state (gate/chunk-equivalence: max rel err 3e-7) — see scripts/tests/test_carry.py`}

## Changed surface

{Paths, symbols, interfaces, schemas, migrations, and SSOT §-anchors the round touched. The
reviewer uses `__review__/DIFF.patch` for the exact change set; this orients it.}

## Claims to attack

{Numbered, falsifiable. The reviewer is asked to try to break these, e.g.
"1. The retry path is idempotent across an ambiguous timeout (commit after remote write)."}

## Known weak spots

{Where the implementer is least confident; blind spots of the current test ladder.}

## Questions

{Specific questions for the reviewer, numbered.}

## Provided check evidence

{Commands already run this round, as `command — result — evidence/log pointer`. The reviewer
treats these as implementer evidence (provided, not performed) and may re-run cheap ones.}

## Explicitly out of scope

{Areas the round did not change and does not ask the reviewer to audit.}
