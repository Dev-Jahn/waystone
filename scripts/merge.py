#!/usr/bin/env python3
"""Retained pure promotion-readiness helpers; public acceptance is owned by ``run close``."""
from __future__ import annotations

import sys


def tasks_gate_counts(data: dict) -> dict:
    rows = data.get("tasks", []) if isinstance(data, dict) else []
    tasks = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return {
        "open_blockers": [row.get("id") for row in tasks
                          if row.get("severity") == "blocker"
                          and row.get("status") not in ("done", "dropped")],
        "open_decisions": [row.get("id") for row in tasks
                           if str(row.get("id", "")).startswith("decision/")
                           and row.get("status") not in ("done", "dropped")],
    }


def merge_gate(facts: dict) -> tuple[bool, list[str]]:
    failures = []
    if not facts.get("promotion_assurance_ready", False):
        failures.append("promotion assurance is not ready")
    for key, label in (("open_blockers", "open blocker tasks"),
                       ("open_decisions", "unresolved owner rulings")):
        values = facts.get(key) or []
        if values:
            failures.append(f"{label}: {', '.join(values)}")
    return not failures, failures


def main(argv: list[str]) -> int:
    print("waystone merge: public merge workflow is retired; use waystone run close", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
