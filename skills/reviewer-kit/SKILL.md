---
name: reviewer-kit
description: This skill should be used when the user runs "/jahns-workflow:reviewer-kit", asks to "set up the ChatGPT reviewer", "generate the reviewer project sources", "render the reviewer kit", or needs the static protocol files for the external GPT review project. One-time setup of the web-reviewer's ChatGPT Project (instructions + Project Sources).
argument-hint: "[output-dir] (default: ./jahns-chatgpt-reviewer-kit)"
disable-model-invocation: true
---

# jahns-workflow: reviewer-kit

Render the **static reviewer protocol** for the external web reviewer (ChatGPT GPT-5.5 Pro). This
is a one-time-per-protocol-version setup, separate from per-round review bundles. The kit carries
NO repository state — only the rules a reviewer follows for every `jahns-workflow` project.

Plugin root = two directories above this skill's base directory.

## Step 1 — Render the kit

```bash
uv run <plugin-root>/scripts/jw.py reviewer kit --out <output-dir>
```

This writes (from `<plugin-root>/templates/chatgpt-reviewer/`):

- `PROJECT_INSTRUCTIONS.txt` — paste into the ChatGPT Project's instructions (the control plane).
- `JW_INSTRUCTION.md`, `JW_REPOSITORY_CONTRACT.md`, `JW_REVIEW_PLAYBOOK.md`,
  `JW_OUTPUT_CONTRACT.md`, `JW_EXAMPLES.md` — upload as Project Sources (the static protocol).
- `KIT_MANIFEST.yaml` — protocol version, compatible bundle schema, and a SHA-256 of each source.

## Step 2 — Wire up the ChatGPT Project (manual, by the user)

Tell the user, in their configured language, to:

1. Create (or open) a ChatGPT Project for this repo's reviews.
2. Paste `PROJECT_INSTRUCTIONS.txt` into **Project settings → instructions**.
3. Upload the five `JW_*.md` files as **Project Sources**.
4. Each review session: attach one `*.review.zip` (from `/jahns-workflow:round` → `jw review
   bundle`) and send the one-line prompt the bundle command prints. Nothing else is pasted.

## Step 3 — Updates

The kit is versioned by the plugin. When the protocol changes (the `KIT_MANIFEST.yaml` hashes
differ from what the Project holds), re-render and replace only the changed Project Sources in
ChatGPT. The per-round bundle's `MANIFEST.yaml` declares the bundle schema the reviewer must accept.

## Step 4 — Report

Report where the kit was written and the exact ChatGPT setup steps. Leave files uncommitted unless
the user is maintaining the kit inside a repo and asks to commit it.
