#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""PostToolUse hook body: validate an edited tasks.yaml; regenerate ROADMAP.md on success.

Exit 0 + silence when valid (ROADMAP refreshed deterministically, zero tokens).
Exit 2 + violations on stderr when invalid (fed back to Claude to fix immediately).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import yaml  # noqa: E402

import roadmap  # noqa: E402
from common import find_project_root  # noqa: E402
from validate import validate  # noqa: E402


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    p = Path(file_path)
    if p.name != "tasks.yaml":
        return 0
    root = find_project_root(p.parent)
    if root is None or (root / "tasks.yaml") != p.resolve():
        return 0

    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"[waystone] tasks.yaml is not parseable YAML: {e}", file=sys.stderr)
        return 2

    errs = validate(data)
    if errs:
        print(f"[waystone] tasks.yaml violates the workflow convention ({len(errs)} issue(s)) — fix now:",
              file=sys.stderr)
        for e in errs[:20]:
            print(f"  - {e}", file=sys.stderr)
        return 2

    (root / "ROADMAP.md").write_text(roadmap.render(root), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
