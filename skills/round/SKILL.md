---
name: round
description: This skill should be used when the user runs "/waystone:round" in Claude Code or "$waystone:round" in Codex, says to "close the round", "wrap up this round", "finish the work cycle", or when an autonomous work round (implement → verify → push) reaches its end and the project's host instruction file mandates round closeout. Updates the task registry, PROGRESS, roadmap, SSOT views, and writes the round's markdown review request.
argument-hint: "[round-slug] e.g. lstream-seams"
---

# waystone: round

## Host contract

- Claude Code: invoke `/waystone:round`; assign `$CLAUDE_PLUGIN_ROOT` to
  `WAYSTONE_PLUGIN_ROOT`, then run command examples with `waystone` from `PATH`.
- Codex: invoke `$waystone:round`; from this skill's directory walk up two parents, assign that
  absolute path to `WAYSTONE_PLUGIN_ROOT`, then run command examples with
  `$WAYSTONE_PLUGIN_ROOT/bin/waystone-codex`.
- Resolve plugin resources from `$WAYSTONE_PLUGIN_ROOT`. Ask required choices through the host's native
  user-interaction mechanism; never require a specifically named question tool.

Close the current work round: bring the task registry up to date, record the round in
PROGRESS, refresh generated views, and write the round's external review request (one markdown
file; the reviewer reads the repo over git).

Requires an initialized project (`.waystone.yml`). If missing, stop and point the user
at `/waystone:init` in Claude Code or `$waystone:init` in Codex.

## Step 1 — Determine the round id

`<today YYYY-MM-DD>-<slug>`: slug from the argument if given, else derive a short one from the
round's dominant theme. Check PROGRESS for an existing entry with the same id (extend it
rather than duplicating).

## Step 2 — Sync the task registry

First register any newly discovered work as new tasks via the CLI — `waystone task add
<type>/<slug> . --title "..." [--severity ...] [--deps a,b]` (proper IDs + explanatory
titles; set `anchor:` to the governing SSOT §-anchor when known) — rather than hand-editing the
registry. Unresolved questions for the user become `decision/...` tasks; when a `decision/...` is
answered, record the ruling with `waystone task set <id> ruling "..."`.

Before handing off nontrivial work, resolve the profile with `waystone paths --root <project-root>`
and follow the selected role's `execution`/`backend`. Use `waystone delegate run <task-id>` only for
an `implementer` bound to `external-runner`. For `clean-subagent`, `forked-subagent`,
`deterministic-workflow`, or `main-session`, use the host's native execution mechanism instead and
preserve the role attribution. When exact path scope is derivable, record it first with repeated
`waystone task set <task-id> --scope-add "<repo-relative-prefix>"` calls. Whichever route ran, include
the task in this round's `--done` or `--touched` set and record its role/execution/backend and result
in PROGRESS.

Then close the round in one atomic, deterministic step instead of hand-editing each field:

```bash
waystone round close . --round <round-id> \
    --done <comma-ids that fully passed> --touched <comma-ids worked but not done> \
    --route-note <role>,<execution>,<backend>
```

Repeat `--route-note` once for each host-guided role actually used in the round. Do not record an
external-runner here; delegation exposure already records it. The close command validates every
note against the current profile and stores it in the immutable round exposure. If no host-guided
route was used, omit the flag; downstream role attribution remains unknown rather than guessed.

`round close` flips the `--done` tasks to `done`, stamps `round:` on every worked task, validates
the registry, regenerates `ROADMAP.md` (and SSOT views if configured), and advances
`state.last_round_commit`.
A `gate/...` task goes in `--done` only if the bar actually passed (link evidence in PROGRESS).
If `round close` reports the registry invalid, fix the reported issues before continuing.
If lanes were used this round, first verify them: `waystone lanes verify .`.

Relay adaptive-rule results with tri-state wording: **fired**, **did not fire (evaluable)**, or
**unevaluable (<coverage reason>)**. Never call an unevaluable rule a non-fire. Keep a
`waystone warn conflict` line labeled as a policy conflict whose effective stage was resolved
least-restrictively; do not relabel it as a rule fire.

Then keep the registry small: `waystone task archive .` relocates old
done/dropped tasks into `tasks.archive.yaml` once the registry crosses a size threshold (it keeps
the most-recent few for decision context, and never archives a task a live one still depends on).
It is a safe no-op below the threshold, so run it every round.

## Step 3 — PROGRESS entry + archive

Append an entry from `$WAYSTONE_PLUGIN_ROOT/templates/progress-entry.md` (content in the user's
configured language). Then archive: move dated sections from months before the current one
into `docs/progress/<YYYY-MM>.md` (mechanical cut-paste, newest-first preserved), leaving
PROGRESS.md with the current month + the header pointers.

## Step 4 — Request review

Review-request generation is an unconditional part of round closeout. Never ask whether to create
it and never end the round without either passing its publication gate or reporting that gate's
failure.

Write only the model-authored narrative to `/tmp/<round-id>-review-narrative.md`. It must contain
these six `##` sections exactly once and in this order: `What changed and why`, `Read these first`,
`Claims to attack`, `Evidence already produced (mine — inspect, don't trust)`, `Known weak spots`,
and `Domain lens`. Point to existing logs/PROGRESS evidence rather than copying it. Do not add
project, branch, reviewer, Reviewing/diff-base, or response fields; the renderer owns every protocol
surface and rejects lookalikes.

**Packet mode** (`review.mode: packet`, default): while `HEAD` still equals the immutable exposure
written by `round close`, render and bind the request, then commit all round-close outputs and the
two generated review artifacts together. The model never opens or edits the rendered request.

```bash
waystone review prepare --round <round-id> \
  --narrative /tmp/<round-id>-review-narrative.md .
git add -- <round-close outputs> \
  <reviews_dir>/<round-id>-request.md <reviews_dir>/<round-id>-request.binding*.json
git commit -m "docs(round): close and publish <round-id>"
git push
waystone remote verify . --round <round-id>
```

`review prepare` derives the exact target/base, project, branch, and resolved reviewer from the
round exposure, renders the plugin template, and refuses a stale `HEAD`, a mismatched immutable
sidecar, an invalid narrative, or an unresolved template token. `remote verify --round` then proves
the request and matching binding are committed unchanged in the pushed publication commit. If a
command fails, stop without reporting a review-ready packet.

After the gate passes, give the user the verified upstream ref, publication SHA, and repo-relative
request path, followed only by a short instruction to review that request at that remote commit.

**PR mode** (`review.mode: pr`): a request cannot be committed into the SHA it names. Commit and
push the round-close outputs first, record a host-local exposure at that fixed commit, render the
host-local request, and let `freeze` post that rendered document as the PR-comment carrier:

```bash
git add -- <round-close outputs>
git commit -m "docs(round): close <round-id>"
git push
waystone remote verify .
waystone round reclose . --round <round-id>
waystone review prepare --round <round-id> \
  --narrative /tmp/<round-id>-review-narrative.md .
waystone review freeze --pr <N> --round <round-id> .
waystone review status --pr <N> .
```

`round reclose` changes only host-local immutable exposure evidence and preserves the original round
diff base. It refuses while tracked closeout changes are uncommitted; untracked files are
deliberately outside that check — commit every closeout artifact before reclosing. `freeze` refuses unless the prepared request and binding target the exact current PR head
and configured reviewer set. Never treat comment presence as review completion; the formal identity
remains `(reviewer, cycle, reviewed_sha)`.

## Step 5 — Report

Report in the user's configured language: shipped tasks (id — title), registry/roadmap state, and
the packet mode's verified remote locator
(`<upstream>@<publication-sha>:<reviews_dir>/<round-id>-request.md`) or the PR mode's frozen cycle,
PR number, and reviewed SHA. Do not describe a local-only path as review-ready.

End with the **next-step reminder** (so the reply is preserved byte-exact, not re-typed by a model):

> For packet mode, give the reviewer the verified remote round-request locator; PR mode already
> carries the rendered request in its frozen comment. To ingest the reply, save it **in a separate shell**:
> `cat > /tmp/review.md` → paste → `Ctrl-D`. Then run `/waystone:review <round-id>` in Claude
> Code or `$waystone:review <round-id>` in Codex; it copies `/tmp/review.md` verbatim into the
> reviews dir (no model retyping) and triages it.

## Step 6 — Refresh the re-entry pointer

**OVERWRITE** the project's persistent re-entry file so the next session — or a post-compaction
resume — picks up the live frontier without you re-explaining "where were we". Get its path:

```bash
waystone resume --start-here-path .
```

Then overwrite that file — never append — with **≤ ~35 lines / ~2.5KB**:

- first line: `# re-entry @ <round-id> / HEAD <short-sha>`
- then the live frontier: what just landed, the open decision / next probe and **why**, the active
  lane(s) — with detail **linked** to PROGRESS / topic files, not inlined.

This replaces the old habit of growing a "START HERE" blob inside the agent-memory `MEMORY.md`
(which accumulates unbounded). The SessionStart hook injects this file automatically each session;
it is reset every round, so keep it short and current. Authoritative state stays in
tasks.yaml / PROGRESS — this is only a pointer.
