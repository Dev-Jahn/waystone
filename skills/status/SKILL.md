---
name: status
description: This skill should be used when the user runs "/waystone:status" in Claude Code or "$waystone:status" in Codex, asks "what's the status across my projects", "show the project dashboard", "which tasks are active/blocked", or wants a cross-project overview of branches, rounds, and task progress.
argument-hint: "[project-name] (optional filter)"
allowed-tools: ["Bash", "Read"]
---

# waystone: status

## Host contract

- Claude Code: invoke `/waystone:status`; assign `$CLAUDE_PLUGIN_ROOT` to
  `WAYSTONE_PLUGIN_ROOT`, then run command examples with `waystone` from `PATH`.
- Codex: invoke `$waystone:status`; from this skill's directory walk up two parents, assign that
  absolute path to `WAYSTONE_PLUGIN_ROOT`, then run command examples with
  `$WAYSTONE_PLUGIN_ROOT/bin/waystone-codex`.
- Resolve plugin resources from `$WAYSTONE_PLUGIN_ROOT`. Ask required choices through the host's native
  user-interaction mechanism; never require a specifically named question tool.

Show the cross-project dashboard. Zero-LLM rendering: run the script, relay its output.

```bash
waystone status            # all registered projects
waystone status --project <name>
```

Relay the output verbatim in a code block (it is pre-formatted). Add at most 1–3 sentences
in the user's configured language only when something needs flagging: blocked tasks whose
dependencies are all done (stale `blocked` status), projects with `✗ path missing`, or
pending `decision/...` tasks awaiting the user. Otherwise add nothing.

Projects appear here after `/waystone:init` in Claude Code or `$waystone:init` in Codex registers
them with `waystone project register <project-root>`. Use `waystone project list` to inspect the
machine registry and `waystone paths` to see its resolved location; do not edit `projects.json`
directly. Each project's visual dependency graph is its `ROADMAP.md` (rendered by GitHub as
Mermaid).
