#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unified front door for waystone scripts: `waystone <group> <args>`.

Groups:
  validate [tasks.yaml]              validate the task registry
  task     list|show|add|set|drop|archive ...  structured registry access (`set --scope-add` for boundaries)
  roadmap  [root]                    regenerate ROADMAP.md
  ssot     split|digest|check [root] SSOT generated views
  status   [--project N]             cross-project dashboard
  remote   verify|drift [root]       is HEAD pushed / how far behind; verify --round gates packets
  review   freeze|status|pending|prepare|ingest|triage ...  review publication, pending, ingest, and triage
  approve  --pr N --sha X            SHA-bound human approval
  round    merge --pr N ...          deterministic merge guard
  improve  trace|reviews|evidence|audit|metrics|decide ...  project evidence, metrics, and decisions
  delegate run|status|show|verify|verdict|apply|discard ...  worktree runner + evidence-gated verdict
  run      start|resume|status|watch|cancel|actions ...  one-task run engine (opt-in)
  overlay  add|...|promote-user|override|materialize|compose ...  four-layer adaptive policy
  consent  record <surface> <choice> ...  append a standard project-local consent event
  install  agents|hooks|statusline [--consent-recorded] ...  consent-gated managed surfaces
  statusline                            one-line current-project status (read-only, no model)
  check    [--root DIR]               evaluate active overlay deltas at an explicit boundary (never blocks)
  paths    [--root DIR] [--json]      show resolved machine and project storage paths
  project  register|unregister|alias|list ...  manage the cross-project registry

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

HERE = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(HERE))
import common  # noqa: E402

_STATUS_RESET = "\033[0m"
_STATUS_BOLD_CYAN = "\033[1;38;5;81m"
_STATUS_GREEN = "\033[38;5;114m"
_STATUS_YELLOW = "\033[38;5;221m"
_STATUS_MAGENTA = "\033[38;5;176m"
_STATUS_RED = "\033[38;5;210m"
_STATUS_DIM = "\033[38;5;245m"


def _statusline_project_root(start: Path) -> Path | None:
    """Find only the current config without inspecting unsupported legacy state."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / common.CONFIG_NAME).is_file():
            return candidate
    return None


def _statusline_error(message: str) -> str:
    return (f"{_STATUS_BOLD_CYAN}◆ waystone{_STATUS_RESET} "
            f"{_STATUS_DIM}│{_STATUS_RESET} {_STATUS_RED}⚠ {message}{_STATUS_RESET}")


def _statusline_render(data: dict, pending_count: int) -> str:
    tasks = data["tasks"]
    done = sum(task.get("status") == "done" for task in tasks)
    blocked = sum(task.get("status") == "blocked" for task in tasks)
    active_rounds = sorted({
        task["round"] for task in tasks
        if task.get("status") == "active" and task.get("round")
    })
    current_round = (active_rounds[-1] if active_rounds else
                     max((task.get("round") or "" for task in tasks), default="") or "—")
    separator = f" {_STATUS_DIM}│{_STATUS_RESET} "
    return separator.join((
        f"{_STATUS_BOLD_CYAN}◆ waystone{_STATUS_RESET}",
        f"{_STATUS_GREEN}✓ tasks {done}/{len(tasks)}{_STATUS_RESET}",
        f"{_STATUS_YELLOW}◷ round {current_round}{_STATUS_RESET}",
        f"{_STATUS_MAGENTA}◇ reviews {pending_count}{_STATUS_RESET}",
        f"{_STATUS_RED}⛔ blockers {blocked}{_STATUS_RESET}",
    ))


def _statusline_main(argv: list[str]) -> int:
    """Render without legacy checks, locks, state writes, subprocesses, or model calls."""
    try:
        root = _statusline_project_root(Path.cwd())
    except (OSError, RuntimeError):
        return 0
    if root is None:
        return 0
    if argv:
        print(_statusline_error("invalid statusline arguments"))
        return 0

    try:
        cfg = common.load_yaml(root / common.CONFIG_NAME)
        if not isinstance(cfg, dict):
            raise ValueError("config must be a mapping")
        common.normalize_config(cfg)
    except Exception:  # noqa: BLE001 — a prompt status line must degrade to one honest line
        print(_statusline_error("config unreadable"))
        return 0
    try:
        # Display surface: parse and count only — the full registry validation pass is a
        # deliberate NON-feature here (per-render validate was explicitly rejected; real
        # commands validate loudly).
        data = common.load_yaml(root / common.TASKS_NAME)
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("task registry is invalid")
    except Exception:  # noqa: BLE001 — a prompt status line must degrade to one honest line
        print(_statusline_error("registry unreadable"))
        return 0
    try:
        import contextlib
        import io
        import review

        # Path.glob swallows scandir permission errors, which would silently render an
        # unreadable reviews directory as "reviews 0" — probe readability explicitly first.
        reviews_dir = root / cfg["reviews_dir"]
        if reviews_dir.exists():
            next(iter(reviews_dir.iterdir()), None)
        # Reuse the single pending-review derivation. Diagnostics are suppressed because this
        # display owns stdout and must never damage the surrounding prompt line.
        with contextlib.redirect_stderr(io.StringIO()):
            pending_count = len(review.pending_reviews(root))
    except Exception:  # noqa: BLE001 — damaged review artifacts remain visible, but non-fatal
        print(_statusline_error("reviews unreadable"))
        return 0

    print(_statusline_render(data, pending_count))
    return 0


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


def _command_root(value: str | None) -> Path:
    start = Path(value).expanduser() if value is not None else Path.cwd()
    root = common.find_project_root(start)
    if root is None:
        raise common.WorkflowError(f"no waystone project found from {start.resolve()}")
    return root


def _consent_main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] != "record":
        print("waystone consent: expected record <surface> <choice> [--context key=value] [--root DIR]",
              file=sys.stderr)
        return 1
    surface, choice = argv[1:3]
    context: dict[str, str] = {}
    root_value = None
    rest = argv[3:]
    i = 0
    try:
        while i < len(rest):
            if rest[i] in ("--context", "--root") and i + 1 < len(rest):
                value = rest[i + 1]
                if rest[i] == "--root":
                    if root_value is not None:
                        raise common.WorkflowError("--root may be passed only once")
                    root_value = value
                else:
                    key, separator, item = value.partition("=")
                    if not separator or not key or key in context:
                        raise common.WorkflowError(
                            "--context requires a unique non-empty key=value")
                    context[key] = item
                i += 2
            else:
                raise common.WorkflowError(f"unexpected argument {rest[i]!r}")
        root = _command_root(root_value)
        with common.hold_project_lock(root):
            if surface == "materialize":
                import overlay

                delta_id = context.get("origin_delta_id") or context.get("rule_id")
                if not isinstance(delta_id, str) or not delta_id:
                    raise common.WorkflowError(
                        "materialize consent requires --context origin_delta_id=<delta-id>")
                context = overlay.materialize_consent_context(root, delta_id)
            elif surface in ("install.agents", "install.hooks", "install.statusline"):
                kind = surface.partition(".")[2]
                if context and context != {"kind": kind}:
                    raise common.WorkflowError(
                        f"{surface} consent context is derived; pass only --context kind={kind}")
                context, _source, _target, _payload = _install_candidate(root, kind)
            row = common.record_consent(root, surface, choice, context)
    except common.WorkflowError as e:
        print(f"waystone consent record: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone consent record: cannot append consent — {e}", file=sys.stderr)
        return 2
    print(f"recorded consent {row['surface']}={row['choice']} -> {common.consent_path(root)}")
    return 0


_INSTALL_TARGETS = {
    "agents": ("waystone-operator-agent.md", Path(".claude/agents/waystone-operator.md")),
    "hooks": (None, Path(".waystone/boundary-hooks-enabled")),
}
_STATUSLINE_KIND = "statusline"
# The installed command guards the launcher boundary itself: if `uv run` cannot even start
# (cold cache, read-only env), the prompt gets an empty line instead of rc!=0 + stderr noise.
# The honest degradation tokens are printed by the Python dispatcher once it runs.
_STATUSLINE_VALUE = {"type": "command", "command": "waystone statusline 2>/dev/null || true"}


def _install_candidate(root: Path, kind: str) -> tuple[dict, Path | None, Path, bytes]:
    if kind == _STATUSLINE_KIND:
        target = Path.home() / ".claude" / "settings.json"
        candidate = {
            "kind": kind,
            "target_path": str(target.resolve()),
            "stage": "install",
            "command": _STATUSLINE_VALUE["command"],
        }
        context = {**candidate, "candidate_hash": common.canonical_payload_hash(candidate)}
        return context, None, target, b""
    template_name, relative_target = _INSTALL_TARGETS[kind]
    target = root / relative_target
    if os.path.lexists(target):
        raise common.WorkflowError(f"refusing to overwrite existing managed install target {target}")
    source = HERE.parent / "templates" / template_name if template_name is not None else None
    payload = b""
    if source is not None:
        try:
            payload = source.read_bytes()
        except OSError as e:
            raise common.WorkflowError(f"cannot read managed install template {source}: {e}") from e
    candidate = {
        "kind": kind, "target_path": str(target.resolve()), "stage": "install",
    }
    if source is not None:
        candidate["template_hash"] = common.content_hash(payload)
    context = {**candidate, "candidate_hash": common.canonical_payload_hash(candidate)}
    return context, source, target, payload


def _legacy_boundary_hook_settings(root: Path) -> Path | None:
    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        return None
    try:
        document = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    hooks = document.get("hooks")
    stop_groups = hooks.get("Stop") if isinstance(hooks, dict) else None
    if not isinstance(stop_groups, list):
        return None
    for group in stop_groups:
        commands = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(commands, list):
            continue
        for hook in commands:
            if (isinstance(hook, dict) and isinstance(hook.get("command"), str)
                    and hook["command"].strip() == "waystone check"):
                return settings
    return None


def _install_statusline(target: Path) -> str:
    if target.exists():
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise common.WorkflowError(
                f"cannot read Claude settings {target}: {type(e).__name__}") from e
        if not isinstance(document, dict):
            raise common.WorkflowError(f"Claude settings must be a JSON object: {target}")
    else:
        document = {}

    if "statusLine" in document:
        if document["statusLine"] == _STATUSLINE_VALUE:
            return f"statusLine already runs `waystone statusline` in {target}; no changes made"
        return (
            f"existing statusLine preserved in {target}; Waystone did not modify it. "
            "To embed project status in a script that parses Claude's `cwd`, add "
            "`project_status=$(cd \"$cwd\" && waystone statusline)` and append its non-empty "
            "output to your rendered line."
        )

    document["statusLine"] = dict(_STATUSLINE_VALUE)
    common.write_text_atomic(
        target, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return f"installed statusLine: {target} (remove its statusLine field to roll back)"


def _install_main(argv: list[str]) -> int:
    if not argv or argv[0] not in (*_INSTALL_TARGETS, _STATUSLINE_KIND):
        print("waystone install: expected agents|hooks|statusline "
              "[--consent-recorded] [--root DIR]",
              file=sys.stderr)
        return 1
    kind = argv[0]
    root_value = None
    consent_recorded = False
    rest = argv[1:]
    i = 0
    try:
        while i < len(rest):
            if rest[i] == "--root" and i + 1 < len(rest):
                if root_value is not None:
                    raise common.WorkflowError("--root may be passed only once")
                root_value = rest[i + 1]
                i += 2
            elif rest[i] == "--consent-recorded":
                if consent_recorded:
                    raise common.WorkflowError("--consent-recorded may be passed only once")
                consent_recorded = True
                i += 1
            else:
                raise common.WorkflowError(f"unexpected argument {rest[i]!r}")
        root = _command_root(root_value)
        surface = f"install.{kind}"
        with common.hold_project_lock(root):
            context, _source, target, payload = _install_candidate(root, kind)
            if consent_recorded:
                common.record_consent(root, surface, "accept", context)
            if not common.has_accepted_consent(root, surface, context):
                raise common.WorkflowError(
                    f"consent is required; record `{surface}` acceptance or pass --consent-recorded")
            if kind == _STATUSLINE_KIND:
                install_message = _install_statusline(target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open("xb") as stream:
                        stream.write(payload)
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
    except common.WorkflowError as e:
        print(f"waystone install {kind}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone install {kind}: cannot install managed surface — {e}", file=sys.stderr)
        return 2
    if kind == _STATUSLINE_KIND:
        print(install_message)
        return 0
    legacy_settings = _legacy_boundary_hook_settings(root) if kind == "hooks" else None
    if legacy_settings is not None:
        print("legacy waystone Stop hook detected in "
              f"{legacy_settings}; remove that hook from .claude/settings.json after review "
              "(Waystone did not modify the file)")
    if kind == "hooks":
        print(f"enabled hooks: {target} (project-local state)")
    else:
        print(f"installed {kind}: {target} (left uncommitted)")
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
    for entry in projects:
        if isinstance(entry, dict) and "aliases" in entry:
            aliases = entry["aliases"]
            if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                raise common.WorkflowError(
                    f"registry entry `aliases` must be a list of paths: {path}")
    common.validate_registry_path_uniqueness(projects, path)
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


def _entry_aliases(entry: object) -> list[Path]:
    if not isinstance(entry, dict):
        return []
    aliases = entry.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
        raise common.WorkflowError("registry entry `aliases` must be a list of paths")
    return [Path(alias).expanduser().resolve() for alias in aliases]


def _project_main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("register", "unregister", "alias", "list"):
        print(
            "waystone project: expected register <path>, unregister <path>, "
            "alias <path> --root <root>, or list",
            file=sys.stderr,
        )
        return 1
    command, rest = argv[0], argv[1:]
    if command == "list":
        if rest:
            print("waystone project list: takes no arguments", file=sys.stderr)
            return 1
    elif command == "alias":
        if len(rest) != 3 or rest[1] != "--root":
            print(
                "waystone project alias: expected <path> --root <root>", file=sys.stderr)
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
        if command == "alias":
            root = common.find_project_root(Path(rest[2]).expanduser())
            if root is None:
                raise common.WorkflowError(f"no waystone project found from {rest[2]}")
            root = root.resolve()
            target = next((entry for entry in projects if _entry_path(entry) == root), None)
            if target is None:
                raise common.WorkflowError(
                    f"canonical project root is not registered: {root}; register it first")
            if requested == root:
                raise common.WorkflowError(f"alias is already the canonical project path: {root}")
            for entry in projects:
                if _entry_path(entry) == requested:
                    raise common.WorkflowError(
                        f"alias path is another project's canonical path: {requested}")
                if requested in _entry_aliases(entry):
                    if entry is target:
                        print(f"already aliases: {requested}\t{root}")
                        return 0
                    raise common.WorkflowError(f"alias path is already registered elsewhere: {requested}")
            target["aliases"] = sorted(
                [*map(str, _entry_aliases(target)), str(requested)])
            common.validate_registry_path_uniqueness(projects, path)
            _write_registry(path, registry)
            print(f"alias added: {requested}\t{root}")
            return 0

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
            projects.append({"name": name, "path": str(root), "aliases": []})
            common.validate_registry_path_uniqueness(projects, path)
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


def _check_command_project_state(argv: list[str]) -> None:
    """Refuse unsupported state for the project addressed by this CLI invocation."""
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
        with common.hold_project_lock(root):
            common.require_supported_project_state(root)


def _module_checks_project_state(argv: list[str]) -> bool:
    """Whitelist modules whose direct CLI entry point performs its own project-state check."""
    if not argv:
        return False
    if argv[0] in {"task", "review", "delegate", "run", "overlay", "check", "roadmap"}:
        return True
    return argv[0] == "round" and len(argv) > 1 and argv[1] in {"close", "reclose"}


def main(argv: list[str]) -> int:
    group = argv[0] if argv else None
    # The prompt renderer is a strict read-only fast path. It bypasses legacy state checks, their
    # locks, and machine registry parsing so unsupported/outside projects remain untouched.
    if group == "statusline":
        try:
            return _statusline_main(argv[1:])
        except Exception:  # noqa: BLE001 — never break the host prompt for an optional display
            print(_statusline_error("status unavailable"))
            return 0
    try:
        # Lock acquisition order is registry -> project -> record. Project registry verbs keep the
        # registry lock across state checks and registry RMW so one CLI entry acquires it once.
        if group == "project":
            with common.hold_lock(common.registry_lock_path()):
                common.require_supported_machine_state()
                _check_command_project_state(argv)
                return _project_main(argv[1:])
        with common.hold_lock(common.registry_lock_path()):
            common.require_supported_machine_state()
        if argv and not _module_checks_project_state(argv):
            _check_command_project_state(argv)
    except common.WorkflowError as e:
        print(f"waystone state check: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waystone state check: filesystem failure: {e}", file=sys.stderr)
        return 2
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    group, rest = argv[0], argv[1:]

    # new-style modules expose main(argv)
    if group == "consent":
        return _consent_main(rest)
    if group == "install":
        return _install_main(rest)
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
    if group == "run":
        from waystone.cli import run_group
        return run_group.main(rest)
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
        if rest and rest[0] in {"close", "reclose"}:
            return _run_module_main("round", rest)
        print("waystone round: expected 'close', 'reclose', or 'merge'", file=sys.stderr)
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
