#!/usr/bin/env bash
# Stop fast-path: run the non-blocking boundary check only for explicitly enabled projects.
set -uo pipefail

. "$(cd "$(dirname "$0")" && pwd)/verifier_guard.sh"
waystone_verifier_hook_guard && exit 0

if [ -n "${PLUGIN_ROOT:-}" ]; then
  export WAYSTONE_HOST=codex
fi

input=$(cat)

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/.waystone.yml" ]; then
      printf '%s' "$dir"
      return 0
    fi
    dir=$(dirname "$dir")
  done
  return 1
}

cwd=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
root=$(find_root "${cwd:-$PWD}") || root=$(find_root "$PWD") || exit 0
[ -f "$root/.waystone/boundary-hooks-enabled" ] || exit 0

uv run --quiet "$(cd "$(dirname "$0")" && pwd)/../../scripts/waystone.py" check --root "$root"
exit 0
