---
name: status
description: This skill should be used when the user runs "/waystone:status", asks "what's the status across my projects", "show the project dashboard", "which tasks are active/blocked", or wants a cross-project overview of branches, rounds, and task progress.
argument-hint: "[project-name] (optional filter)"
allowed-tools: ["Bash", "Read"]
---

# waystone: status

Show the cross-project dashboard. Zero-LLM rendering: run the script, relay its output.

Plugin root = two directories above this skill's base directory.

```bash
uv run <plugin-root>/scripts/dashboard.py            # all registered projects
uv run <plugin-root>/scripts/dashboard.py --project <name>
```

Relay the output verbatim in a code block (it is pre-formatted). Add at most 1–3 sentences
in the user's configured language only when something needs flagging: blocked tasks whose
dependencies are all done (stale `blocked` status), projects with `✗ path missing`, or
pending `decision/...` tasks awaiting the user. Otherwise add nothing.

Projects appear here after `/waystone:init` registers them. Projects without a local
clone on this machine can be tracked remotely: add `{ "name": "...", "repo": "owner/name" }`
to `~/.claude/waystone/projects.json` and the dashboard fetches their `tasks.yaml` via
`gh api` (default branch). Each project's visual dependency graph is its `ROADMAP.md`
(rendered by GitHub as Mermaid).
