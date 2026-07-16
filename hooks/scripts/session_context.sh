#!/usr/bin/env bash
# SessionStart fast-path: only spin up Python in waystone-initialized projects.
set -uo pipefail

. "$(cd "$(dirname "$0")" && pwd)/verifier_guard.sh"
waystone_verifier_hook_guard && exit 0

# Codex injects PLUGIN_ROOT (and a CLAUDE_PLUGIN_ROOT compatibility alias); Claude injects only
# CLAUDE_PLUGIN_ROOT. Select the host before Python imports the host-local data helpers.
if [ -n "${PLUGIN_ROOT:-}" ]; then
  export WAYSTONE_HOST=codex
fi

input=$(cat)

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/.waystone.yml" ] || [ -f "$dir/.jahns-workflow.yml" ]; then
      printf '%s' "$dir"
      return 0
    fi
    dir=$(dirname "$dir")
  done
  return 1
}

cwd=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
root=$(find_root "${cwd:-$PWD}") || root=$(find_root "$PWD") || exit 0

printf '%s' "$input" | uv run --quiet "$(cd "$(dirname "$0")" && pwd)/session_context.py" "$root"
