#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""SessionStart hook: inject the canonical objective-first status read model."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from common import git_branch_info  # noqa: E402
from waystone.project.context import resolve_project_context  # noqa: E402
from waystone.runs.engine import ReadOnlyStoreUnavailable, open_read_only_store  # noqa: E402
from waystone.runs.observe import project_status_json, project_status_projection  # noqa: E402

MAX_CHARS = 8000


def _status(root: Path) -> dict[str, object]:
    context = resolve_project_context(root)
    try:
        with open_read_only_store(context.canonical_root) as store:
            projection = project_status_projection(context.canonical_root, store)
    except ReadOnlyStoreUnavailable:
        projection = project_status_projection(context.canonical_root)
    payload = project_status_json(projection)
    # Keep the re-entry capsule explicit: objective, lifecycle stage, waiting context, and last delta.
    _last_delta = payload["outcome"]["last_delta"]
    del _last_delta
    payload["checkout"] = {
        "project_id": context.project_id,
        "checkout_identity": context.checkout_identity,
        "branch": git_branch_info(root)["branch"],
    }
    return payload


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    try:
        payload = _status(root)
        text = "[waystone] status read model:\n" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2)
    except Exception as error:  # noqa: BLE001 — hook must remain optional and honest
        text = f"[waystone] status unavailable: {type(error).__name__}: {error}"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 1].rstrip() + "…"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
