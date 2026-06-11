# Review request — round {round-id}

> Paste this whole packet to the external reviewer (e.g. web ChatGPT).
> Ingest the reply with `/jahns-workflow:review {round-id}`.

## Scope

- Repo: {owner/name}, branch {branch}, **HEAD {pushed commit hash}** — review the code at this exact commit
- Commits this round: {first}..{last} ({n} commits, {diffstat})
- Round goal: {one paragraph}
- Tasks shipped: {id — title, one per line}
- SSOT sections touched: {§-anchors, or "none"}

## What changed

{Concise narrative with pointers the reviewer can follow directly in the repo: file paths,
SSOT §-anchors, commit hashes. Reviewers with repo access (e.g. a GitHub connector) should
read the actual code at the HEAD above rather than trusting this summary. Inline only small
load-bearing snippets where reading them in place saves a lookup; if the reviewer has NO repo
access, inline the key diffs/pseudocode instead.}

## Claims to verify (please attack these)

{Numbered list of the round's load-bearing claims, each stated falsifiably, e.g.
"1. The chunked path is numerically equivalent to the full path within fp32 tolerance for
nonzero initial state (gate/chunk-equivalence passed with max rel err 3e-7)."}

## Known weak spots

{Where the implementer is least confident. Blind spots of the current test ladder.}

## Questions

{Specific questions for the reviewer, numbered.}
