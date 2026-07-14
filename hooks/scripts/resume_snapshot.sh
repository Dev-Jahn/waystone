#!/usr/bin/env bash
# PreCompact / SessionEnd fast-path: snapshot a re-entry pointer only in initialized projects.
set -uo pipefail

input=$(cat)

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/.jahns-workflow.yml" ]; then
      printf '%s' "$dir"; return 0
    fi
    dir=$(dirname "$dir")
  done
  return 1
}

cwd=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
root=$(find_root "${cwd:-$PWD}") || root=$(find_root "$PWD") || exit 0

uv run --quiet "$(cd "$(dirname "$0")" && pwd)/../../scripts/resume.py" "$root" >/dev/null 2>&1 || true
exit 0
