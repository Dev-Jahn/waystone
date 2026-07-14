#!/usr/bin/env bash
# SessionStart fast-path: only spin up Python in waystone-initialized projects.
set -uo pipefail

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
