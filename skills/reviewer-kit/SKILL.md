---
name: reviewer-kit
description: This skill should be used when the user runs "/jahns-workflow:reviewer-kit", asks to "set up the ChatGPT reviewer", "generate the reviewer project sources", "render the reviewer kit", or needs the ChatGPT Project setup for the external GPT review project. One-time setup of the web-reviewer's ChatGPT Project (instructions + optional Project Sources).
argument-hint: "[output-dir] (default: ./jahns-chatgpt-reviewer-kit); add --strict for the provenance protocol kit"
disable-model-invocation: true
---

# jahns-workflow: reviewer-kit

Render the reviewer setup for the external web reviewer (ChatGPT). One-time-per-project; carries NO
repository state. Two variants:

- **loose (default)** — for the `raw-zip` packet flow: the reviewer gets the repo zip (incl. `.git`)
  + the per-round brief and does a **domain** review. Short instructions, no fixed protocol.
- **strict (`--strict`)** — the SHA-pinned JW_* protocol + tamper-evident manifest, for
  provenance-gated PR review or the `strict-bundle` transport (reviewer reads a sandboxed bundle).

Plugin root = two directories above this skill's base directory.

## Step 1 — Render the kit

```bash
uv run <plugin-root>/scripts/jw.py reviewer kit --out <output-dir>            # loose (default)
uv run <plugin-root>/scripts/jw.py reviewer kit --strict --out <output-dir>   # strict protocol
```

Loose writes (from `<plugin-root>/templates/chatgpt-reviewer-loose/`):

- `REVIEWER_INSTRUCTIONS.md` — paste into the ChatGPT Project's instructions.
- `REVIEWER_CONTEXT.md` — optional Project Source (where to look; review priorities).

Strict writes (from `<plugin-root>/templates/chatgpt-reviewer/`): `PROJECT_INSTRUCTIONS.txt` +
the five `JW_*.md` Project Sources + `KIT_MANIFEST.yaml` (protocol version + per-source SHA-256).

## Step 2 — Wire up the ChatGPT Project (manual, by the user)

Tell the user, in their configured language:

**Loose (default):**
1. Create (or open) a ChatGPT Project for this repo's reviews.
2. Paste `REVIEWER_INSTRUCTIONS.md` into **Project settings → instructions**.
3. Optionally upload `REVIEWER_CONTEXT.md` and the repo's `docs/review-profile.md` (the domain lens,
   from `<plugin-root>/templates/review-profile.md`) as **Project Sources**.
4. Each review: attach the repo zip (incl. `.git`) + send the round's prompt. The reviewer reads
   `docs/reviews/<round>-request.md` and runs git itself.

**Strict (`--strict`):** paste `PROJECT_INSTRUCTIONS.txt` into the instructions, upload the five
`JW_*.md` as Project Sources, and each session attach one `*.review.zip` + the printed prompt.

## Step 3 — Updates

When the templates change, re-render and replace the relevant Project pieces in ChatGPT. For the
strict kit, the `KIT_MANIFEST.yaml` hashes tell you which Project Sources changed; the per-round
bundle's `MANIFEST.yaml` declares the bundle schema the reviewer must accept.

## Step 4 — Report

Report where the kit was written and the exact ChatGPT setup steps. Leave files uncommitted unless
the user is maintaining the kit inside a repo and asks to commit it.
