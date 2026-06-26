# External Review Brief — {round-id}

> The reviewer has the full repo as a zip **including `.git`** — they can run git directly
> (`git log`, `git diff`, `git show`, even CPU tests). This brief is a map to cut their ramp-up
> time, **not** a substitute for reading the code. This is a **domain / code review**, not a
> jahns-workflow harness audit.

## Target
- Project / Branch: {project} / {branch}
- Reviewed HEAD: {head full sha}  ← `git rev-parse HEAD` must match this; if not, stop and report it
- Diff base: {base full sha, or "(root)" for the first round}

Run first:

```bash
git rev-parse HEAD
git log --oneline --decorate --graph -n 30
git diff --stat {base}..HEAD
git diff --name-status --find-renames {base}..HEAD
```

## What changed and why

{Not a file list — the intent. What problem this round attacked, what structure you chose, and
why you believe it is right. 5–10 sentences the reviewer can step into.}

## Read these first

1. `{path}` — {why it is load-bearing}
2. `{path}` — {why}

## Claims to attack

{Numbered, falsifiable. What you assert is true and want the reviewer to try to break — the
math / model / kernel / physics invariant, the thing a passing test could still get wrong.}

## Evidence already produced (mine — inspect, don't trust)

| Claim | Command / artifact | My reading | Where it lives |
|---|---|---|---|
| {claim} | `{command}` | {your interpretation} | `{path/to/log or PROGRESS §}` |

## Known weak spots

{Where you are least confident — the proof gap, the GPU-only race, the training dynamics you are
unsure you read right, the boundary condition you hand-waved.}

## Domain lens

{See `docs/review-profile.md` for this project's standing review priorities. This round
especially: <the angle that matters most this round>.}

## Out of scope

{What not to audit this round — including the jahns-workflow harness unless explicitly asked.}

## Response I want

Major / critical issues only (skip style, naming, optional refactors). Return:

1. Verdict: `ok-to-use` or `hold`
2. Major findings — each with a concrete failure mechanism and where you confirmed it
3. Domain questions / decision points
4. Checks you actually ran
5. Residual risks from unavailable GPU / data / environment
