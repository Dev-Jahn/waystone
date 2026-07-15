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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import yaml  # noqa: E402

import roadmap  # noqa: E402
from common import (  # noqa: E402
    WorkflowError, find_project_root, hold_lock, project_lock_path, write_text_atomic,
)
from validate import validate  # noqa: E402


_PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_MOVE_PATH_RE = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)


def _edited_paths(payload: dict) -> list[Path]:
    """Return Claude file_path or Codex apply_patch paths without guessing other tool formats."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        return [Path(file_path)]

    command = tool_input.get("command")
    if payload.get("tool_name") != "apply_patch" or not isinstance(command, str):
        return []
    cwd = Path(payload.get("cwd") or ".")
    paths = []
    for raw in [*_PATCH_PATH_RE.findall(command), *_MOVE_PATH_RE.findall(command)]:
        path = Path(raw.strip())
        paths.append(path if path.is_absolute() else cwd / path)
    return paths


def _canonical_tasks_path(payload: dict) -> tuple[Path, Path] | None:
    for candidate in _edited_paths(payload):
        if candidate.name != "tasks.yaml":
            continue
        root = find_project_root(candidate.parent)
        if root is not None and (root / "tasks.yaml").resolve() == candidate.resolve():
            return candidate.resolve(), root
    return None


def _validate_and_regenerate(p: Path, root: Path) -> int:
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

    write_text_atomic(root / "ROADMAP.md", roadmap.render(root))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    matched = _canonical_tasks_path(payload)
    if matched is None:
        return 0
    p, root = matched
    try:
        with hold_lock(project_lock_path(root), timeout=3):
            return _validate_and_regenerate(p, root)
    except (WorkflowError, OSError) as e:
        print(f"[waystone] project lock unavailable ({e}); skipping ROADMAP regeneration",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
