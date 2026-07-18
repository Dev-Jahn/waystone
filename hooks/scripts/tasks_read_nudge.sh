#!/usr/bin/env bash
# PreToolUse fast-path: only act when a Read targets a file named tasks.yaml.
set -uo pipefail

. "$(cd "$(dirname "$0")" && pwd)/verifier_guard.sh"
waystone_verifier_hook_guard && exit 0

input=$(cat)

printf '%s' "$input" | grep -qE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*tasks\.yaml"' || exit 0

printf '%s' "$input" | uv run --quiet "$(cd "$(dirname "$0")" && pwd)/tasks_read_nudge.py"
