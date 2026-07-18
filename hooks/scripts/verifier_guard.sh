#!/usr/bin/env bash
# Shared guard for every manifest hook entrypoint. Independent verifier sessions must be hermetic
# from all Waystone host automation, including read redirects and non-blocking lifecycle hooks.

waystone_verifier_hook_guard() {
  [ "${WAYSTONE_VERIFIER_SESSION:-}" = "1" ]
}
