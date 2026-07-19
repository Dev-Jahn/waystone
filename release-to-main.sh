#!/usr/bin/env bash
# release-to-main.sh — sync dev's shipping subset onto main without touching the caller's tree.
#
# Branch model (see README): dev = integration branch (the test suite and this tooling live
# here); main = release branch carrying the plugin runtime only. The marketplace pins main.
# The positive manifest below is the complete shipping surface; unlisted paths stay dev-only.
#
# Usage:  ./release-to-main.sh
#   Runs the dev test gate, then commits the projected tree onto main. Does NOT push — review the
#   commit, then `git push origin main` and bump the marketplace sha to it.
set -euo pipefail

SHIP_PATHS=(
  .claude-plugin
  .codex-plugin
  .github
  .gitignore
  LICENSE
  README.md
  assets
  bin
  hooks
  references
  scripts/cclog.py
  scripts/codexlog.py
  scripts/common.py
  scripts/dashboard.py
  scripts/delegate.py
  scripts/improve.py
  scripts/lanes.py
  scripts/merge.py
  scripts/overlay.py
  scripts/remote.py
  scripts/resume.py
  scripts/review.py
  scripts/roadmap.py
  scripts/round.py
  scripts/ssot.py
  scripts/tasks.py
  scripts/validate.py
  scripts/waystone.py
  skills
  templates
)

# Deliberate dev-only surface. Both manifests are expanded to tracked file paths below, so a new
# scripts/foo.py warns even though other explicitly listed scripts ship. This path check and the
# projected smoke cannot prove dependencies reached only through lazy imports; nested data below
# an already allowlisted directory is intentionally treated as manifested.
DEV_ONLY_PATHS=(
  .claude
  .waystone.yml
  dev_docs
  PROGRESS.md
  ROADMAP.md
  SSOT.md
  docs
  release-to-main.sh
  scripts/tests
  tasks.yaml
)

cd "$(git rev-parse --show-toplevel)"
repo_root=$(pwd -P)

tmp_base=${TMPDIR:-/tmp}
if ! tmp_base=$(cd "$tmp_base" && pwd -P); then
  echo "release: TMPDIR does not name an accessible directory: ${TMPDIR:-/tmp}" >&2
  exit 1
fi
# Guard every root Git may write under, not just this worktree: a temporary worktree placed
# inside the shared common dir or a sibling worktree would leave unrelated user state dirty if
# cleanup fails.
reject_tmp_inside() {
  local guarded=$1 resolved
  [ -n "$guarded" ] || return 0
  resolved=$(cd "$guarded" 2>/dev/null && pwd -P) || resolved=$guarded
  case "$tmp_base/" in
    "$resolved/"*)
      echo "release: TMPDIR must be outside the repository and its worktrees: $tmp_base (inside $resolved)" >&2
      exit 1
      ;;
  esac
}
reject_tmp_inside "$repo_root"
reject_tmp_inside "$(git rev-parse --path-format=absolute --git-common-dir)"
while IFS= read -r worktree_line; do
  case "$worktree_line" in
    "worktree "*) reject_tmp_inside "${worktree_line#worktree }" ;;
  esac
done <<< "$(git worktree list --porcelain)"

assert_main_not_checked_out() {
  local worktree_list main_worktree worktree_path line
  worktree_list=$(git worktree list --porcelain)
  main_worktree=""
  worktree_path=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) worktree_path=${line#worktree } ;;
      "branch refs/heads/main") main_worktree=$worktree_path; break ;;
    esac
  done <<< "$worktree_list"
  if [ -n "$main_worktree" ]; then
    echo "release: refs/heads/main is checked out at $main_worktree — aborting." >&2
    return 1
  fi
}

assert_main_not_checked_out

# Concurrency model: single user, single process (SSOT §3). The assert→update-ref window is not
# atomic against a concurrent checkout of main from another process; that scenario is out of the
# supported threat model. The CAS old-OID on update-ref protects against main *moving*, only.

if [ -n "$(git status --porcelain)" ]; then
  echo "release: working tree not clean — commit or stash first." >&2
  exit 1
fi

dev_oid=$(git rev-parse --verify 'dev^{commit}')
dev_sha=$(git rev-parse --short "$dev_oid")
main_oid=$(git rev-parse --verify 'refs/heads/main^{commit}')
tmpdir=$(mktemp -d "$tmp_base/waystone-release.XXXXXX")
test_worktree="$tmpdir/dev"
projected_worktree="$tmpdir/projected"
smoke_home="$tmpdir/home"
release_index="$tmpdir/index"
release_result_succeeded=0

cleanup() {
  local status=$?
  local cleanup_status=0
  trap - EXIT HUP INT TERM
  set +e
  if [ -e "$projected_worktree/.git" ]; then
    if ! git worktree remove --force "$projected_worktree"; then
      echo "release: failed to remove temporary worktree $projected_worktree; state kept at $tmpdir." >&2
      cleanup_status=1
    fi
  fi
  if [ -e "$test_worktree/.git" ]; then
    if ! git worktree remove --force "$test_worktree"; then
      echo "release: failed to remove temporary worktree $test_worktree; state kept at $tmpdir." >&2
      cleanup_status=1
    fi
  fi
  if [ "$cleanup_status" -eq 0 ] && ! rm -rf -- "$tmpdir"; then
    echo "release: failed to remove temporary state at $tmpdir." >&2
    cleanup_status=1
  fi
  if [ "$status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
    if [ "$release_result_succeeded" -eq 1 ]; then
      echo "release: cleanup warning only; the recorded release result remains successful." >&2
    else
      status=$cleanup_status
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Run the gate against the exact dev commit being projected, in an isolated worktree.
git worktree add --detach -q "$test_worktree" "$dev_oid"

# Warn for tracked files outside both manifests. Excluding the expanded pathspecs from the exact
# dev index is a NUL-delimited file-level check rather than a top-level path heuristic. Unknown
# paths are warning-only; failing to enumerate them at all is not — that would silently skip the
# coverage check.
unknown_pathspecs=(.)
for manifest_path in "${SHIP_PATHS[@]}" "${DEV_ONLY_PATHS[@]}"; do
  unknown_pathspecs+=(":(exclude)$manifest_path")
done
unknown_paths_list="$tmpdir/unknown-paths"
if ! git -C "$test_worktree" ls-files -z -- "${unknown_pathspecs[@]}" > "$unknown_paths_list"; then
  echo "release: could not enumerate tracked paths for manifest coverage — aborting." >&2
  exit 1
fi
while IFS= read -r -d '' path; do
  echo "release: warning: tracked path is outside release manifests: $path" >&2
done < "$unknown_paths_list"

echo "release: running the test suite on dev…"
if ! (cd "$test_worktree" && uv run scripts/tests/run_tests.py); then
  echo "release: tests failed on dev — aborting." >&2
  exit 1
fi

# Build the release tree in a temporary index from positive-manifest paths only.
GIT_INDEX_FILE="$release_index" git read-tree --empty
git ls-tree -r -z "$dev_oid" -- "${SHIP_PATHS[@]}" |
  GIT_INDEX_FILE="$release_index" git update-index -z --index-info
release_tree=$(GIT_INDEX_FILE="$release_index" git write-tree)
main_tree=$(git rev-parse "$main_oid^{tree}")

# Materialize exactly the projected tree, refresh its index, then execute the shipped front door.
# The smoke runs under an environment ALLOWLIST (env -i): no inherited variable — including uv
# re-injection channels such as UV_ENV_FILE or UV_WORKING_DIRECTORY — can make it borrow code or
# state from the caller's dev tree. The machine-level uv wheel cache and managed-python directory
# are passed through deliberately: they hold toolchain artifacts, not dev-tree code, and keep the
# smoke offline-capable.
git worktree add --detach -q "$projected_worktree" "$main_oid"
git -C "$projected_worktree" read-tree --reset -u "$release_tree"
git -C "$projected_worktree" update-index -q --refresh
mkdir -p "$smoke_home"
smoke_env=(
  PATH="$PATH"
  HOME="$smoke_home"
  TMPDIR="$tmp_base"
  WAYSTONE_HOME="$smoke_home/.waystone"
  CODEX_HOME="$smoke_home/.codex"
  PYTHONNOUSERSITE=1
)
if smoke_uv_cache=$(uv cache dir 2>/dev/null) && [ -n "$smoke_uv_cache" ]; then
  smoke_env+=(UV_CACHE_DIR="$smoke_uv_cache")
fi
if smoke_uv_python=$(uv python dir 2>/dev/null) && [ -n "$smoke_uv_python" ]; then
  smoke_env+=(UV_PYTHON_INSTALL_DIR="$smoke_uv_python")
fi
echo "release: running the projected release smoke…"
if ! (
  cd "$projected_worktree"
  env -i "${smoke_env[@]}" ./bin/waystone status >/dev/null
); then
  echo "release: projected release smoke failed — aborting." >&2
  exit 1
fi

if [ "$release_tree" = "$main_tree" ]; then
  # A same-value update-ref still compares the expected old OID atomically. Without it, main could
  # move after main_oid was read and this no-op path would report a false success.
  assert_main_not_checked_out
  git update-ref -m "release: verify no-op from dev@${dev_sha}" \
    refs/heads/main "$main_oid" "$main_oid"
  release_result_succeeded=1
  echo "release: main already in sync with dev@${dev_sha} — nothing to release."
  exit 0
fi

# commit-tree does not run commit hooks. It also evaluates config in the caller's branch context,
# so includeIf onbranch:main signing settings are not active while main is deliberately not checked
# out; only the active commit.gpgsign setting below is honored for this release commit.
commit_args=(commit-tree "$release_tree" -p "$main_oid" -m "release: sync from dev@${dev_sha}")
if git config --get-regexp '^commit\.gpgsign$' >/dev/null; then
  commit_gpgsign=$(git config --bool --get commit.gpgsign)
  if [ "$commit_gpgsign" = "true" ]; then
    commit_args+=(-S)
  fi
fi
release_commit=$(git "${commit_args[@]}")
assert_main_not_checked_out
git update-ref -m "release: sync from dev@${dev_sha}" \
  refs/heads/main "$release_commit" "$main_oid"
release_result_succeeded=1

echo "release: main @ $(git rev-parse --short "$release_commit") built from dev@${dev_sha}."
echo "next:    git push origin main   then bump the marketplace sha to this commit."
