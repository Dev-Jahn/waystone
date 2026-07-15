#!/usr/bin/env bash
# release-to-main.sh — sync dev's shipping subset onto main, excluding dev-only tooling.
#
# Branch model (see README): dev = integration branch (the test suite and this tooling live
# here); main = release branch carrying the plugin runtime only. The marketplace pins main.
# This rebuilds main's tree as `dev's tree − EXCLUDES` in one release commit. It is itself
# dev-only (listed in EXCLUDES) and never lands on main.
#
# Usage:  ./release-to-main.sh
#   Runs the dev test gate, then commits the synced tree onto main. Does NOT push — review the
#   commit, then `git push origin main` and bump the marketplace sha to it.
set -euo pipefail

# paths that exist only on dev and must never appear in a main release:
EXCLUDES=(scripts/tests release-to-main.sh)
# waystone's own project artifacts (this repo dogfoods waystone): tracked on dev, never shipped.
EXCLUDES+=(
  SSOT.md .waystone.yml tasks.yaml tasks.archive.yaml ROADMAP.md PROGRESS.md AGENTS.md
  docs/CONVENTIONS.md docs/ssot docs/adr docs/reviews docs/waystone-policy.yaml
)

cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "release: working tree not clean — commit or stash first." >&2
  exit 1
fi

start=$(git symbolic-ref --short HEAD)
restore() { git checkout -q "$start"; }

# 1. release gate — the suite must pass on dev before anything reaches main
git checkout -q dev
echo "release: running the test suite on dev…"
if ! uv run scripts/tests/run_tests.py; then
  echo "release: tests failed on dev — aborting." >&2
  restore
  exit 1
fi
dev_sha=$(git rev-parse --short dev)

# 2. main's next tree = dev's tree − EXCLUDES, committed on top of main
git checkout -q main
git read-tree -u --reset dev
for p in "${EXCLUDES[@]}"; do
  git rm -r -q --cached --ignore-unmatch -- "$p"
  rm -rf -- "$p"
done

if git diff --cached --quiet; then
  echo "release: main already in sync with dev@${dev_sha} — nothing to release."
  restore
  exit 0
fi

git commit -q -m "release: sync from dev@${dev_sha}"
echo "release: main @ $(git rev-parse --short main) built from dev@${dev_sha} (minus ${EXCLUDES[*]})."
echo "next:    git push origin main   then bump the marketplace sha to this commit."
restore
