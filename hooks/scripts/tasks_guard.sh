#!/usr/bin/env bash
# PostToolUse fast-path: only act when the edited file is a tasks.yaml.
set -uo pipefail

. "$(cd "$(dirname "$0")" && pwd)/verifier_guard.sh"
waystone_verifier_hook_guard && exit 0

if [ -n "${PLUGIN_ROOT:-}" ]; then
  export WAYSTONE_HOST=codex
fi

input=$(cat)

printf '%s' "$input" | grep -qE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*tasks\.yaml"|\*\*\* (Add|Update|Delete) File: [^"]*tasks\.yaml|\*\*\* Move to: [^"]*tasks\.yaml' || exit 0

printf '%s' "$input" | uv run --quiet "$(cd "$(dirname "$0")" && pwd)/tasks_guard.py"
