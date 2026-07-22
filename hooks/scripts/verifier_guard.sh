#!/usr/bin/env bash
# Shared guard for every manifest hook entrypoint. Independent verifier sessions remain hermetic
# from all Waystone host automation, including read redirects and lifecycle hooks.

waystone_verifier_hook_guard() {
  [ "${WAYSTONE_VERIFIER_SESSION:-}" = "1" ]
}
