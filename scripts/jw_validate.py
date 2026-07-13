#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Validate a jahns-workflow tasks.yaml against the global naming convention and schema.

Usage: jw_validate.py [path/to/tasks.yaml]   (default: ./tasks.yaml or nearest project root)
Exit codes: 0 valid, 1 cannot read/parse, 2 schema violations (details on stderr).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from jw_common import (  # noqa: E402
    MILESTONE_ID_RE, MILESTONE_STATUSES, ROUND_RE, SEVERITIES,
    TASK_ID_RE, TASK_STATUSES, TASK_TYPES, find_project_root,
)


def validate(data: object) -> list[str]:
    """Return a list of human-readable violations (empty list = valid)."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["top level must be a mapping with keys: version, project, tasks"]
    if data.get("version") != 1:
        errs.append("version: must be 1")
    if not isinstance(data.get("project"), str) or not data.get("project", "").strip():
        errs.append("project: required non-empty string")

    milestones = data.get("milestones", [])
    if not isinstance(milestones, list):
        errs.append("milestones: must be a list")
        milestones = []
    ms_ids: set[str] = set()
    for i, ms in enumerate(milestones):
        loc = f"milestones[{i}]"
        if not isinstance(ms, dict):
            errs.append(f"{loc}: must be a mapping")
            continue
        mid = ms.get("id")
        if not isinstance(mid, str) or not MILESTONE_ID_RE.match(mid):
            errs.append(f"{loc}.id: must match M<number> (e.g. M1), got {mid!r}")
        elif mid in ms_ids:
            errs.append(f"{loc}.id: duplicate milestone id {mid!r}")
        else:
            ms_ids.add(mid)
        title = ms.get("title")
        if not isinstance(title, str) or len(title.strip()) < 6:
            errs.append(f"{loc}.title: required, ≥6 chars — a phrase an outsider can understand")
        if ms.get("status", "pending") not in MILESTONE_STATUSES:
            errs.append(f"{loc}.status: must be one of {MILESTONE_STATUSES}")

    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return errs + ["tasks: must be a list"]
    ids: set[str] = set()
    for i, t in enumerate(tasks):
        loc = f"tasks[{i}]"
        if not isinstance(t, dict):
            errs.append(f"{loc}: must be a mapping")
            continue
        tid = t.get("id")
        loc = f"tasks[{i}] ({tid!r})"
        if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
            errs.append(
                f"{loc}.id: must match <type>/<kebab-slug> with type in {TASK_TYPES} and a "
                f"3–48 char kebab-case slug (no leading/trailing dash). "
                f"Bare codenames like 'P0', 'E3' are banned."
            )
            tid = None
        elif tid in ids:
            errs.append(f"{loc}.id: duplicate task id")
        else:
            ids.add(tid)

        title = t.get("title")
        if not isinstance(title, str) or len(title.strip()) < 10:
            errs.append(f"{loc}.title: required, ≥10 chars")
        elif " " not in title.strip():
            errs.append(f"{loc}.title: must be an explanatory phrase (contains a space), not a codeword")
        elif tid and title.strip().lower() == tid.lower():
            errs.append(f"{loc}.title: must explain the task, not repeat the id")

        if t.get("status", "pending") not in TASK_STATUSES:
            errs.append(f"{loc}.status: must be one of {TASK_STATUSES}")
        ms_ref = t.get("milestone")
        if ms_ref is not None and ms_ref not in ms_ids:
            errs.append(f"{loc}.milestone: unknown milestone {ms_ref!r}")
        rnd = t.get("round")
        if rnd is not None and (not isinstance(rnd, str) or not ROUND_RE.match(rnd)):
            errs.append(f"{loc}.round: must match YYYY-MM-DD-<slug>, got {rnd!r}")
        sev = t.get("severity")
        if sev is not None and sev not in SEVERITIES:
            errs.append(f"{loc}.severity: must be one of {SEVERITIES}")
        deps = t.get("deps", [])
        if not isinstance(deps, list):
            errs.append(f"{loc}.deps: must be a list of task ids")
        else:
            seen_deps: set[str] = set()
            for d in deps:
                if not isinstance(d, str) or not TASK_ID_RE.match(d):
                    errs.append(f"{loc}.deps: each dep must be a <type>/<slug> task id, got {d!r}")
                elif d in seen_deps:
                    errs.append(f"{loc}.deps: duplicate dep {d!r}")
                else:
                    seen_deps.add(d)
        for field in ("anchor", "origin", "branch", "notes", "ruling", "result"):
            v = t.get(field)
            if v is not None and not isinstance(v, str):
                errs.append(f"{loc}.{field}: must be a string")
        acc = t.get("accept")
        if acc is not None:
            if not isinstance(acc, list):
                errs.append(f"{loc}.accept: must be a list of free-text acceptance criteria (strings)")
            else:
                for a in acc:
                    if not isinstance(a, str):
                        errs.append(f"{loc}.accept: each criterion must be a string, got {a!r}")
        lane = t.get("lane")
        if lane is not None:
            if not isinstance(lane, dict):
                errs.append(f"{loc}.lane: must be a mapping with branch/base_sha")
            else:
                if not isinstance(lane.get("branch"), str):
                    errs.append(f"{loc}.lane.branch: required string")
                if not isinstance(lane.get("base_sha"), str):
                    errs.append(f"{loc}.lane.base_sha: required string (sha the lane was cut from)")
                if "depends_on" in lane:
                    dep_on = lane["depends_on"]
                    if not isinstance(dep_on, list):
                        errs.append(f"{loc}.lane.depends_on: must be a list")
                    else:
                        for d in dep_on:
                            if not isinstance(d, str) or not TASK_ID_RE.match(d):
                                errs.append(f"{loc}.lane.depends_on: each must be a task id, got {d!r}")

    # Dependency references and cycles (only over well-formed ids). A non-list `deps` is already
    # flagged above; normalize it to [] here so graph construction can't raise on it.
    graph = {
        t["id"]: [d for d in (t.get("deps") if isinstance(t.get("deps"), list) else []) if isinstance(d, str)]
        for t in tasks
        if isinstance(t, dict) and isinstance(t.get("id"), str) and t["id"] in ids
    }
    for tid, deps in graph.items():
        for d in deps:
            if d == tid:
                errs.append(f"task {tid!r}: depends on itself")
            elif d not in ids:
                errs.append(f"task {tid!r}: unknown dep {d!r}")

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        for d in graph[node]:
            if d not in graph:
                continue
            if color[d] == GRAY:
                cycle = stack[stack.index(d):] + [d] if d in stack else [node, d]
                errs.append("dependency cycle: " + " -> ".join(cycle))
            elif color[d] == WHITE:
                dfs(d, stack + [d])
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            dfs(node, [node])

    return errs


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        root = find_project_root(Path.cwd())
        path = (root / "tasks.yaml") if root else Path("tasks.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"jw_validate: cannot read {path}: {e}", file=sys.stderr)
        return 1
    errs = validate(data)
    if errs:
        print(f"tasks.yaml: {len(errs)} violation(s):", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 2
    n = len(data.get("tasks", [])) if isinstance(data, dict) else 0
    print(f"tasks.yaml: OK ({n} tasks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
