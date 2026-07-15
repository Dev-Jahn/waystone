#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unified front door for waystone scripts: `waystone <group> <args>`.

Groups:
  validate [tasks.yaml]              validate the task registry
  task     list|show|add|set|drop|archive ...  structured registry access (don't read/edit it raw)
  roadmap  [root]                    regenerate ROADMAP.md
  ssot     split|digest|check [root] SSOT generated views
  status   [--project N]             cross-project dashboard
  remote   verify|drift [root]       is HEAD pushed / how far behind
  review   freeze|status|ingest ...  SHA-bound review cycles (PR mode); ingest = byte-exact reply copy
  approve  --pr N --sha X            SHA-bound human approval
  round    merge --pr N ...          deterministic merge guard
  improve  trace|reviews|evidence|audit|decide ...  project logs + reviews + task-id evidence / decisions
  delegate run|status|show|verify|verdict|apply|discard ...  worktree runner + evidence-gated verdict
  overlay  add|list|show|promote|demote|suspend|retire|replay ...  project-local adaptive warn deltas
  check    [--root DIR]               evaluate active overlay deltas at an explicit boundary (never blocks)
  paths    [--root DIR] [--json]      show resolved machine and project storage paths
  project  register|unregister|list ...  manage the cross-project registry

Existing hook/skill call sites that invoke sibling scripts directly keep working; this is an
additive convenience front door (GPT review: consolidate under one `waystone` CLI).
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def _run_module_main(modname: str, argv: list[str]) -> int:
    """Invoke a sibling module's main() that reads sys.argv (legacy scripts)."""
    sys.argv = [modname, *argv]
    ns = runpy.run_path(str(HERE / f"{modname}.py"), run_name="__waystone_dispatch__")
    return int(ns["main"]() or 0)


def _resolved(path: Path) -> str:
    return str(path.expanduser().resolve())


def _paths_main(argv: list[str]) -> int:
    root_arg = None
    as_json = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            if i + 1 >= len(argv):
                print("waystone paths: --root requires a path", file=sys.stderr)
                return 1
            root_arg = argv[i + 1]
            i += 2
        elif arg == "--json":
            as_json = True
            i += 1
        else:
            print(f"waystone paths: unexpected argument {arg!r}", file=sys.stderr)
            return 1

    start = Path(root_arg).expanduser() if root_arg is not None else Path.cwd()
    root = common.find_project_root(start)
    if root_arg is not None and root is None:
        print(f"waystone paths: no waystone project found from {start.resolve()}", file=sys.stderr)
        return 1

    paths = {
        "machine_root": _resolved(common.machine_dir()),
        "worktrees_cache": _resolved(common.worktrees_cache_dir()),
        "registry": _resolved(common.registry_path()),
    }
    if root is not None:
        import delegate
        import overlay

        paths.update({
            "project_root": _resolved(root),
            "project_state": _resolved(common.project_state_path(root)),
            "resume": _resolved(common.resume_path(root)),
            "start_here": _resolved(common.start_here_path(root)),
            "delegations": _resolved(delegate._delegations_dir(root)),
            "overlay": _resolved(overlay._overlay_dir(root)),
            "exposure": _resolved(overlay._exposure_dir(root)),
            "profile": _resolved(delegate._profile_path(root)),
            "project_improve": _resolved(common.project_state_path(root) / "improve"),
        })

    if as_json:
        print(json.dumps(paths, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for name, path in paths.items():
            print(f"{name}: {path}")
    return 0


def _load_registry(path: Path) -> dict:
    if not path.is_file():
        return {"projects": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise common.WorkflowError(f"registry unreadable/unparseable: {path} ({type(e).__name__})")
    if not isinstance(registry, dict):
        raise common.WorkflowError(f"registry has wrong shape (expected a JSON object): {path}")
    projects = registry.get("projects", [])
    if not isinstance(projects, list):
        raise common.WorkflowError(f"registry has wrong shape (`projects` must be a list): {path}")
    registry["projects"] = projects
    return registry


def _write_registry(path: Path, registry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise


def _entry_path(entry: object) -> Path | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return None
    return Path(entry["path"]).expanduser().resolve()


def _project_main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("register", "unregister", "list"):
        print("waystone project: expected register <path>, unregister <path>, or list", file=sys.stderr)
        return 1
    command, rest = argv[0], argv[1:]
    if command == "list":
        if rest:
            print("waystone project list: takes no arguments", file=sys.stderr)
            return 1
    elif len(rest) != 1:
        print(f"waystone project {command}: expected one path", file=sys.stderr)
        return 1

    path = common.registry_path()
    try:
        registry = _load_registry(path)
        projects = registry["projects"]
        if command == "list":
            if not projects:
                print("no projects registered")
                return 0
            for entry in projects:
                if not isinstance(entry, dict):
                    print("?\t(invalid entry)")
                    continue
                location = entry.get("path") or entry.get("repo") or "(no path or repo)"
                print(f"{entry.get('name', '?')}\t{location}")
            return 0

        requested = Path(rest[0]).expanduser().resolve()
        if command == "register":
            root = common.find_project_root(requested)
            if root is None:
                raise common.WorkflowError(f"no waystone project found from {requested}")
            try:
                project_data = common.load_tasks(root)
            except (OSError, ValueError, yaml.YAMLError) as e:
                raise common.WorkflowError(f"cannot read {root / common.TASKS_NAME}: {e}") from e
            name = project_data.get("project")
            if not isinstance(name, str) or not name.strip():
                raise common.WorkflowError(f"{root / common.TASKS_NAME} has no non-empty project name")
            root = root.resolve()
            if any(_entry_path(entry) == root for entry in projects):
                print(f"already registered: {name}\t{root}")
                return 0
            projects.append({"name": name, "path": str(root)})
            _write_registry(path, registry)
            print(f"registered: {name}\t{root}")
            return 0

        before = len(projects)
        registry["projects"] = [entry for entry in projects if _entry_path(entry) != requested]
        if len(registry["projects"]) == before:
            raise common.WorkflowError(f"project path is not registered: {requested}")
        _write_registry(path, registry)
        print(f"unregistered: {requested}")
        return 0
    except common.WorkflowError as e:
        print(f"waystone project {command}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone project {command}: cannot update registry {path}: {e}", file=sys.stderr)
        return 2


def _migrate_command_project(argv: list[str]) -> None:
    """Run lazy Phase 2 for the project addressed by this CLI invocation, including explicit paths."""
    candidates = [Path.cwd()]
    group, rest = argv[0], argv[1:]
    if group == "improve" and "--user-wide" in rest:
        return
    if "--root" in rest:
        index = rest.index("--root")
        if index + 1 < len(rest):
            candidates.append(Path(rest[index + 1]).expanduser())
    positional_root_groups = {
        "validate", "task", "roadmap", "ssot", "remote", "review", "approve",
        "round", "lanes", "resume", "project",
    }
    if group in positional_root_groups:
        for index, arg in enumerate(rest):
            if arg.startswith("-") or (index > 0 and rest[index - 1].startswith("-")):
                continue
            candidate = Path(arg).expanduser()
            try:
                if candidate.exists():
                    candidates.append(candidate.parent if candidate.is_file() else candidate)
            except OSError:
                continue
    seen: set[Path] = set()
    for candidate in candidates:
        root = common.find_project_root(candidate)
        if root is None or root in seen:
            continue
        seen.add(root)
        with common.hold_lock(common.project_lock_path(root)):
            common.migrate_project_state(root)


def _module_handles_phase2(argv: list[str]) -> bool:
    """Whitelist modules whose direct CLI entry point performs its own one-time lazy migration."""
    if not argv:
        return False
    if argv[0] in {"task", "review", "delegate", "overlay", "check", "roadmap"}:
        return True
    return argv[0] == "round" and len(argv) > 1 and argv[1] == "close"


def main(argv: list[str]) -> int:
    group = argv[0] if argv else None
    try:
        # Lock acquisition order is registry -> project -> record. Project registry verbs keep the
        # registry lock across Phase 1, their optional Phase 2 migration, and the registry RMW so a
        # single CLI entry acquires this lock exactly once.
        if group == "project":
            with common.hold_lock(common.registry_lock_path()):
                common.migrate_home_data()
                _migrate_command_project(argv)
                return _project_main(argv[1:])
        with common.hold_lock(common.registry_lock_path()):
            common.migrate_home_data()
        if argv and not _module_handles_phase2(argv):
            _migrate_command_project(argv)
    except common.WorkflowError as e:
        print(f"waystone migration: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone migration: filesystem failure: {e}", file=sys.stderr)
        return 2
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    group, rest = argv[0], argv[1:]

    # new-style modules expose main(argv)
    if group == "task":
        import tasks
        return tasks.main(rest)
    if group == "review":
        import review
        return review.main(rest)
    if group == "improve":
        import improve
        return improve.main(rest)
    if group == "delegate":
        import delegate
        return delegate.main(rest)
    if group == "overlay":
        import overlay
        return overlay.main(rest)
    if group == "check":
        import overlay
        return overlay.main(["check", *rest])
    if group == "paths":
        return _paths_main(rest)
    if group == "remote":
        import importlib
        mod = importlib.import_module("remote")
        return mod.main(rest)
    if group == "approve":
        import merge
        return merge.main(["approve", *rest])
    if group == "round":
        if rest and rest[0] == "merge":
            import merge
            return merge.main(["merge", *rest[1:]])
        if rest and rest[0] == "close":
            return _run_module_main("round", rest)
        print("waystone round: expected 'close' or 'merge'", file=sys.stderr)
        return 1
    if group == "lanes":
        return _run_module_main("lanes", rest)
    if group == "resume":
        return _run_module_main("resume", rest)

    # legacy modules with main() reading sys.argv
    legacy = {"validate": "validate", "roadmap": "roadmap",
              "ssot": "ssot", "status": "dashboard"}
    if group in legacy:
        return _run_module_main(legacy[group], rest)

    print(f"waystone: unknown group {group!r}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
