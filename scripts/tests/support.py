#!/usr/bin/env python3
"""Small shared helpers for the focused 0.13 contract suite."""
from __future__ import annotations

import json as _json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import yaml  # noqa: E402


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def init_repo(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("0", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "c0")


def _run_with_home(home: Path, fn):
    old = os.environ.get("WAYSTONE_HOME")
    try:
        os.environ["WAYSTONE_HOME"] = str(home)
        return fn()
    finally:
        if old is None:
            os.environ.pop("WAYSTONE_HOME", None)
        else:
            os.environ["WAYSTONE_HOME"] = old


def json_bytes(value: object) -> bytes:
    return _json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
