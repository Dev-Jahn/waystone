"""Shared helpers for waystone scripts (imported by sibling scripts)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_NAME = ".waystone.yml"
LEGACY_CONFIG_NAME = ".jahns-workflow.yml"
TASKS_NAME = "tasks.yaml"


def machine_dir(home: Path | None = None) -> Path:
    """Waystone's host-neutral machine data root, optionally resolved under an injected home."""
    override = os.environ.get("WAYSTONE_HOME")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise WorkflowError(
                f"WAYSTONE_HOME must be an absolute path after user expansion, got {override!r}")
        return path
    return (Path.home() if home is None else Path(home)) / ".waystone"


def project_state_path(root: Path) -> Path:
    """Return the project-local state root without touching the filesystem."""
    return Path(root) / ".waystone"


def ensure_project_state_dir(root: Path) -> Path:
    """Create the project-local state root and restore its self-ignore file when needed."""
    state = project_state_path(root)
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != "*\n":
        ignore.write_text("*\n", encoding="utf-8")
    return state


def worktrees_cache_dir(home: Path | None = None) -> Path:
    return machine_dir(home) / "cache" / "worktrees"


def registry_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "projects.json"


def _legacy_claude_root(home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".claude" / "waystone"


def _legacy_codex_root(home: Path | None = None) -> Path:
    base_home = Path.home() if home is None else Path(home)
    codex_home = (Path(os.environ["CODEX_HOME"]).expanduser()
                  if os.environ.get("CODEX_HOME") else base_home / ".codex")
    return codex_home / "waystone"


def _legacy_data_dir(home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".claude" / "jahns-workflow"


def _preserved_legacy_root(root: Path) -> Path:
    return root.with_name(f"{root.name}.pre-0.9")


def _legacy_roots(home: Path | None = None) -> list[tuple[str, Path]]:
    roots = [("claude", _legacy_claude_root(home)), ("codex", _legacy_codex_root(home))]
    seen: set[str] = set()
    return [(host, root) for host, root in roots
            if not (str(root.expanduser().absolute()) in seen
                    or seen.add(str(root.expanduser().absolute())))]


def _read_registry(path: Path) -> dict:
    if not path.is_file():
        return {"projects": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(f"migration registry unreadable/unparseable: {path} ({type(e).__name__})")
    if not isinstance(registry, dict) or not isinstance(registry.get("projects", []), list):
        raise WorkflowError(f"migration registry has wrong shape: {path}")
    registry.setdefault("projects", [])
    return registry


def _registry_key(entry: object, source: Path) -> tuple[str, str]:
    if not isinstance(entry, dict):
        raise WorkflowError(f"migration registry entry is not an object: {source}")
    path = entry.get("path")
    if isinstance(path, str) and path:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            raise WorkflowError(f"migration registry path is not absolute: {source} ({path!r})")
        return "path", str(resolved.resolve())
    repo = entry.get("repo")
    if isinstance(repo, str) and repo:
        return "repo", repo
    raise WorkflowError(f"migration registry entry has neither path nor repo: {source}")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(text)
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise


def _merge_registries(sources: list[tuple[str, Path]], destination: Path) -> list[dict]:
    registry = _read_registry(destination)
    merged: list[dict] = []
    keys: dict[tuple[str, str], tuple[dict, str]] = {}
    for entry in registry["projects"]:
        key = _registry_key(entry, destination)
        if key in keys:
            raise WorkflowError(
                f"migration registry has duplicate {key[0]} key {key[1]!r}: {destination}")
        merged.append(entry)
        keys[key] = (entry, "machine")
    for host, path in sources:
        if not path.is_file():
            continue
        for entry in _read_registry(path)["projects"]:
            key = _registry_key(entry, path)
            label = entry.get("name", key[1])
            if key in keys:
                kept, owner = keys[key]
                print(
                    f"waystone migration: projects.json {host} entry {label!r} duplicate "
                    f"{key[0]}={key[1]!r}; kept {owner} entry {kept.get('name', '?')!r}",
                    file=sys.stderr,
                )
                continue
            merged.append(entry)
            keys[key] = (entry, host)
            print(
                f"waystone migration: projects.json added {host} entry {label!r} "
                f"({key[0]}={key[1]!r})",
                file=sys.stderr,
            )
    if merged != registry["projects"] or (not destination.exists() and merged):
        registry["projects"] = merged
        _write_text_atomic(destination, json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    return merged


def _decision_lines(path: Path, order: int) -> list[tuple[float, int, int, str]]:
    if not path.is_file():
        return []
    rows: list[tuple[float, int, int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise WorkflowError(f"migration decisions unreadable: {path} ({e})")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            at = row.get("at") if isinstance(row, dict) else None
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else None
            if parsed is None or parsed.tzinfo is None:
                raise ValueError("missing timezone-aware at")
        except (json.JSONDecodeError, ValueError) as e:
            raise WorkflowError(f"migration decisions row invalid: {path}:{number} ({e})")
        rows.append((parsed.timestamp(), order, number, line))
    return rows


def _migrate_improve(sources: list[tuple[str, Path]], destination: Path) -> None:
    machine_improve = destination / "improve"
    decision_sources = [("machine", machine_improve / "decisions.jsonl")]
    decision_sources.extend((host, root / "improve" / "decisions.jsonl") for host, root in sources)
    rows: list[tuple[float, int, int, str]] = []
    for order, (_host, path) in enumerate(decision_sources):
        rows.extend(_decision_lines(path, order))

    claude_root = next((root for host, root in sources if host == "claude"), None)
    if claude_root is not None:
        source = claude_root / "improve"
        if source.is_dir():
            for child in sorted(source.iterdir()):
                if child.name == "decisions.jsonl":
                    continue
                target = machine_improve / child.name
                if target.exists():
                    print(
                        f"waystone migration: improve conflict {child} -> {target}; preserved source",
                        file=sys.stderr,
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(target))
                print(f"waystone migration: moved improve projection {child} -> {target}",
                      file=sys.stderr)

    for host, root in sources:
        if host != "codex":
            continue
        projection = root / "improve"
        if projection.is_dir() and any(p.name != "decisions.jsonl" for p in projection.iterdir()):
            print(
                f"waystone migration: preserved regenerable Codex improve projection at {projection}; "
                "regenerate with `waystone improve trace --host codex`",
                file=sys.stderr,
            )

    if rows:
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        _write_text_atomic(
            machine_improve / "decisions.jsonl", "".join(f"{row[3]}\n" for row in rows))
        print(
            f"waystone migration: merged {len(rows)} decision row(s) by timestamp -> "
            f"{machine_improve / 'decisions.jsonl'}",
            file=sys.stderr,
        )


def _registry_slugs(projects: list[dict]) -> set[str]:
    slugs = set()
    for entry in projects:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            slugs.add(_project_slug(Path(entry["path"]).expanduser()))
    return slugs


def _legacy_project_slugs(root: Path) -> set[str]:
    slugs: set[str] = set()
    for area in ("delegations", "overlay", "exposure", "worktrees"):
        base = root / area
        if base.is_dir():
            slugs.update(path.name for path in base.iterdir() if path.is_dir())
    for area in ("resume", "start_here"):
        base = root / area
        if base.is_dir():
            slugs.update(path.stem for path in base.glob("*.md"))
    return slugs


_PROJECT_AREAS = {"resume", "start_here", "delegations", "overlay", "exposure"}


def _phase1_conflict_path(preserved: Path, child: Path) -> Path:
    return _unique_path(preserved / "phase1-conflicts" / child.name)


def _merge_phase1_project_area(child: Path, target: Path, preserved: Path) -> bool:
    changed = False
    for item in sorted(child.iterdir()):
        destination = target / item.name
        if destination.exists():
            continue
        shutil.move(str(item), str(destination))
        changed = True
    if not any(child.iterdir()):
        empty_target = _phase1_conflict_path(preserved, child)
        empty_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(child), str(empty_target))
        changed = True
    return changed


def _report_unmapped_slugs(host: str, root: Path, slugs: set[str]) -> None:
    if not slugs:
        return
    preserved = _preserved_legacy_root(root)
    marker = preserved / ".migration-v2-unmapped-slugs.json"
    reported: set[str] = set()
    if marker.is_file():
        try:
            rows = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                reported = {row for row in rows if isinstance(row, str)}
        except (OSError, json.JSONDecodeError):
            reported = set()
    new = slugs - reported
    for slug in sorted(new):
        print(
            f"waystone migration: unmapped legacy project slug {slug!r} in {host} root {root}; "
            "preserved for manual identification",
            file=sys.stderr,
        )
    if new:
        preserved.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(marker, json.dumps(sorted(reported | slugs), indent=2) + "\n")


def _preserve_phase1_root(root: Path) -> None:
    preserved = _preserved_legacy_root(root)
    worktrees = root / "worktrees"
    if not preserved.exists() and not worktrees.exists():
        os.rename(root, preserved)
        print(f"waystone migration: preserved legacy root {root} -> {preserved}", file=sys.stderr)
        return
    created = not preserved.exists()
    preserved.mkdir(parents=True, exist_ok=True)
    changed = created
    for child in sorted(root.iterdir()):
        if child.name == "worktrees":
            continue
        target = preserved / child.name
        if target.exists():
            if child.name in _PROJECT_AREAS and child.is_dir() and target.is_dir():
                changed = _merge_phase1_project_area(child, target, preserved) or changed
                continue
            if (child.name == "profile.yml" and child.is_file() and target.is_file()
                    and child.read_bytes() == target.read_bytes()):
                conflict = _phase1_conflict_path(preserved, child)
                conflict.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(conflict))
                changed = True
                continue
            if child.name != "profile.yml":
                conflict = _phase1_conflict_path(preserved, child)
                conflict.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(conflict))
                print(
                    f"waystone migration: preservation conflict {child} -> {target}; "
                    f"preserved re-entry copy at {conflict}",
                    file=sys.stderr,
                )
                changed = True
                continue
            print(
                f"waystone migration: preservation conflict {child} -> {target}; "
                "left the plain legacy source in place",
                file=sys.stderr,
            )
            continue
        shutil.move(str(child), str(target))
        changed = True
    if worktrees.exists() and changed:
        print(
            f"waystone migration: left legacy worktrees at {worktrees} so git back-links remain valid",
            file=sys.stderr,
        )
    if not worktrees.exists() and not any(root.iterdir()):
        empty_target = _unique_path(preserved / "phase1-reentries" / root.name)
        empty_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(empty_target))


def migrate_home_data(home: Path | None = None) -> Path:
    """Phase 1: eagerly merge machine state and preserve both legacy host roots without deletion."""
    # 0.9.0-b wraps this entry point in registry.lock; C2 intentionally contains no flock logic.
    old = _legacy_data_dir(home)
    claude = _legacy_claude_root(home)
    if old.exists() and not claude.exists():
        claude.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(claude))
    elif old.exists() and claude.exists():
        print(
            f"waystone: legacy data dir {old} and legacy waystone dir {claude} both exist; "
            f"leaving {old} untouched",
            file=sys.stderr,
        )

    roots = [(host, root) for host, root in _legacy_roots(home) if root.exists()]
    destination = machine_dir(home)
    if not roots:
        return destination

    registry_sources = [(host, root / "projects.json") for host, root in roots]
    projects = _merge_registries(registry_sources, registry_path(home))
    _migrate_improve(roots, destination)
    known = _registry_slugs(projects)
    for host, root in roots:
        _report_unmapped_slugs(host, root, _legacy_project_slugs(root) - known)
    for _host, root in roots:
        _preserve_phase1_root(root)
    return destination


def _phase2_sources(home: Path | None = None) -> list[tuple[str, Path]]:
    sources = []
    seen: set[str] = set()
    for host, plain in _legacy_roots(home):
        for source in (_preserved_legacy_root(plain), plain):
            key = str(source.expanduser().absolute())
            if source.exists() and key not in seen:
                seen.add(key)
                sources.append((host, source))
    return sources


def _ensure_project_state_raw(root: Path) -> Path:
    state = Path(root) / ".waystone"
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != "*\n":
        ignore.write_text("*\n", encoding="utf-8")
    return state


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 10000):
        candidate = path.with_name(f"{path.stem}.{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise WorkflowError(f"migration cannot allocate a preservation path beside {path}")


def _quarantine(state: Path, host: str, logical: Path, source: Path) -> Path:
    target = _unique_path(state / "migration-conflicts" / host / logical)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


def _migrate_file(state: Path, logical: Path, candidates: list[tuple[str, Path]]) -> None:
    live = state / logical
    if live.exists() and not (live.is_file() or live.is_symlink()):
        raise WorkflowError(f"migration destination must be a file, found {live}")
    rows = []
    for host, path in candidates:
        if path.is_file() or path.is_symlink():
            rows.append({
                "host": host, "path": path, "mtime": path.stat().st_mtime_ns,
                "bytes": path.read_bytes(), "live": False,
            })
    if live.is_file() or live.is_symlink():
        rows.append({
            "host": "live", "path": live, "mtime": live.stat().st_mtime_ns,
            "bytes": live.read_bytes(), "live": True,
        })
    if not rows:
        return
    rank = {"codex": 0, "claude": 1, "live": 2}
    winner = max(rows, key=lambda row: (
        row["mtime"], rank.get(row["host"], -1), str(row["path"])))
    chosen = winner["bytes"]

    if live.exists() and not winner["live"]:
        live_row = next(row for row in rows if row["live"])
        if live_row["bytes"] != chosen:
            preserved = _quarantine(state, "live", logical, live)
            print(
                f"waystone migration: conflict {logical}; newer {winner['path']} becomes live, "
                f"preserved previous live file at {preserved}",
                file=sys.stderr,
            )
        else:
            winner = live_row

    if not winner["live"]:
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(winner["path"]), str(live))

    for row in rows:
        path = row["path"]
        if row["live"] or path == winner["path"] or not path.exists():
            continue
        preserved = _quarantine(state, row["host"], logical, path)
        if row["bytes"] != chosen:
            print(
                f"waystone migration: conflict {logical}; kept newer live file and preserved "
                f"{row['host']} loser at {preserved}",
                file=sys.stderr,
            )


def _move_empty_source(state: Path, host: str, source: Path, logical: Path) -> None:
    if not source.is_dir():
        return
    if any(path.is_file() or path.is_symlink() for path in source.rglob("*")):
        return
    target = _unique_path(state / "migration-conflicts" / host / "empty-sources" / logical)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _migrate_tree(state: Path, logical: Path, sources: list[tuple[str, Path]]) -> None:
    groups: dict[Path, list[tuple[str, Path]]] = {}
    for host, source in sources:
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*")):
            if path.is_file() or path.is_symlink():
                groups.setdefault(path.relative_to(source), []).append((host, path))
    for relative in sorted(groups, key=str):
        _migrate_file(state, logical / relative, groups[relative])
    for host, source in sources:
        _move_empty_source(state, host, source, logical)


def _profile_seed(root: Path, state: Path, sources: list[tuple[str, Path]]) -> None:
    live = state / "profile.yml"
    if live.exists():
        return
    profiles = [(host, source / "profile.yml") for host, source in sources
                if (source / "profile.yml").is_file()]
    if not profiles:
        return
    bodies = {path.read_bytes() for _host, path in profiles}
    if len(bodies) != 1:
        paths = ", ".join(str(path) for _host, path in profiles)
        raise WorkflowError(
            f"legacy profile conflict for {root}: {paths}; choose the project profile manually")
    chosen = next((path for host, path in profiles if host == "claude"), profiles[0][1])
    _ensure_project_state_raw(root)
    shutil.copy2(chosen, live)
    print(
        f"waystone migration: seeded project profile {live} from {chosen}; legacy seed preserved",
        file=sys.stderr,
    )


def _overlay_rule_conflicts(root: Path, sources: list[tuple[str, Path]], slug: str) -> None:
    by_rule: dict[str, dict[str, list[Path]]] = {}
    for host, source in sources:
        deltas = source / "overlay" / slug / "deltas"
        if not deltas.is_dir():
            continue
        for path in sorted(deltas.glob("*.json")):
            try:
                delta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise WorkflowError(f"legacy overlay delta unreadable: {path} ({e})")
            rule = delta.get("rule") if isinstance(delta, dict) else None
            if not isinstance(rule, str) or not rule:
                raise WorkflowError(f"legacy overlay delta has no rule id: {path}")
            by_rule.setdefault(rule, {}).setdefault(host, []).append(path)
    for rule, hosts in sorted(by_rule.items()):
        if "claude" in hosts and "codex" in hosts:
            paths = ", ".join(str(path) for paths in hosts.values() for path in paths)
            raise WorkflowError(
                f"overlay rule-id conflict {rule!r} for {root}: {paths}; human selection required")


def _delegation_sources(
        sources: list[tuple[str, Path]], slug: str) -> dict[str, list[tuple[str, Path]]]:
    by_did: dict[str, list[tuple[str, Path]]] = {}
    for host, source in sources:
        base = source / "delegations" / slug
        if not base.is_dir():
            continue
        for record in sorted(base.iterdir()):
            if record.is_dir():
                by_did.setdefault(record.name, []).append((host, record))
    return by_did


def _worktree_sources(
        sources: list[tuple[str, Path]], slug: str) -> dict[str, list[tuple[str, Path]]]:
    by_did: dict[str, list[tuple[str, Path]]] = {}
    for host, source in sources:
        base = source / "worktrees" / slug
        if not base.is_dir():
            continue
        for worktree in sorted(base.iterdir()):
            if worktree.is_dir():
                by_did.setdefault(worktree.name, []).append((host, worktree))
    return by_did


def _warn_did_collision(did: str, candidates: list[tuple[str, Path]]) -> None:
    paths = ", ".join(str(path) for _host, path in candidates)
    print(
        f"waystone migration: WARNING delegation id collision {did!r}; skipped and preserved {paths}",
        file=sys.stderr,
    )


def _mark_worktree_discard_only(state: Path, did: str, reason: str) -> None:
    record = state / "delegations" / did
    if not record.is_dir():
        return
    status_path = record / "status.json"
    status: dict = {}
    if status_path.exists():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("not an object")
            status = loaded
        except (OSError, json.JSONDecodeError, ValueError):
            _quarantine(state, "live", Path("delegations") / did / "status.json", status_path)
    status.setdefault("at_transitions", []).append({
        "state": "migration-worktree-failed", "at": datetime.now().astimezone().isoformat(),
    })
    status["state"] = "migration-worktree-failed"
    status["migration"] = {"disposition": "discard-only", "reason": reason}
    _write_text_atomic(status_path, json.dumps(status, ensure_ascii=False, indent=2) + "\n")


def _migrate_worktree(root: Path, state: Path, slug: str, did: str, old: Path) -> None:
    new = worktrees_cache_dir() / slug / did
    new.parent.mkdir(parents=True, exist_ok=True)
    move_rc, _out, move_err = git_rc(root, "worktree", "move", str(old), str(new))
    if move_rc == 0:
        print(f"waystone migration: moved worktree {old} -> {new} with git worktree move",
              file=sys.stderr)
        return

    filesystem_error = ""
    if new.exists():
        filesystem_error = f"destination already exists: {new}"
    else:
        try:
            shutil.move(str(old), str(new))
        except OSError as e:
            filesystem_error = str(e)
    repair_rc, _out, repair_err = ((1, "", filesystem_error) if filesystem_error else
                                   git_rc(root, "worktree", "repair", str(new)))
    if repair_rc == 0:
        print(
            f"waystone migration: git worktree move failed ({move_err or move_rc}); "
            f"moved {old} -> {new} and repaired git metadata",
            file=sys.stderr,
        )
        return

    reason = "; ".join(part for part in (
        f"git worktree move: {move_err or move_rc}",
        f"fallback move/repair: {repair_err or repair_rc}",
    ) if part)
    _mark_worktree_discard_only(state, did, reason)
    print(
        f"waystone migration: WARNING — WORKTREE MIGRATION FAILED FOR {did}; "
        f"DELEGATION IS DISCARD-ONLY. {reason}",
        file=sys.stderr,
    )


def migrate_project_state(root: Path, home: Path | None = None) -> bool:
    """Phase 2: lazily move one project's legacy host-keyed state into its project-local tier."""
    # 0.9.0-b adds the short project-lock span around this entry point; C2 must remain lock-free.
    root = Path(root).resolve()
    sources = _phase2_sources(home)
    if not sources:
        return False
    slug = _project_slug(root)
    state = root / ".waystone"
    try:
        profiles_present = any((source / "profile.yml").is_file() for _host, source in sources)
        profile_needs_seed = profiles_present and not (state / "profile.yml").exists()
        _overlay_rule_conflicts(root, sources, slug)
        if profile_needs_seed:
            profiles = [(host, source / "profile.yml") for host, source in sources
                        if (source / "profile.yml").is_file()]
            if len({path.read_bytes() for _host, path in profiles}) != 1:
                paths = ", ".join(str(path) for _host, path in profiles)
                raise WorkflowError(
                    f"legacy profile conflict for {root}: {paths}; choose the project profile manually")

        resume = [(host, source / "resume" / f"{slug}.md") for host, source in sources]
        start_here = [(host, source / "start_here" / f"{slug}.md") for host, source in sources]
        trees = {
            "overlay": [(host, source / "overlay" / slug) for host, source in sources],
            "exposure": [(host, source / "exposure" / slug) for host, source in sources],
        }
        delegations = _delegation_sources(sources, slug)
        worktrees = _worktree_sources(sources, slug)
        has_project_items = any(path.is_file() for _host, path in resume + start_here)
        has_project_items = has_project_items or any(
            path.is_dir() for source_list in trees.values() for _host, path in source_list)
        has_project_items = has_project_items or bool(delegations) or bool(worktrees)
        if not profile_needs_seed and not has_project_items:
            return False

        _ensure_project_state_raw(root)
        _profile_seed(root, state, sources)
        _migrate_file(state, Path("resume.md"), resume)
        _migrate_file(state, Path("start-here.md"), start_here)
        for area, area_sources in trees.items():
            _migrate_tree(state, Path(area), area_sources)

        blocked = set()
        for did, candidates in delegations.items():
            if len(candidates) > 1:
                blocked.add(did)
                _warn_did_collision(did, candidates)
                continue
            _migrate_tree(state, Path("delegations") / did, candidates)

        for did, candidates in worktrees.items():
            if len(candidates) > 1:
                if did not in blocked:
                    _warn_did_collision(did, candidates)
                blocked.add(did)
                continue
            if did in blocked:
                continue
            _host, old = candidates[0]
            _migrate_worktree(root, state, slug, did, old)
        return True
    except WorkflowError:
        raise
    except OSError as e:
        raise WorkflowError(f"project migration failed for {root}: {e}") from e


class WorkflowError(Exception):
    """A recoverable workflow error raised by library helpers. Library code must raise this (an
    ordinary Exception, catchable by rollback logic) rather than calling sys.exit() — only CLI
    main() converts it to an exit code. (sys.exit raises SystemExit/BaseException, which slips past
    `except Exception` rollbacks.)"""
TASK_TYPES = ("feat", "fix", "perf", "gate", "spike", "decision", "docs", "chore")
TASK_STATUSES = ("pending", "active", "blocked", "done", "dropped")
MILESTONE_STATUSES = ("pending", "active", "done")
SEVERITIES = ("blocker", "major", "minor")

TASK_ID_RE = re.compile(r"^(?:%s)/[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$" % "|".join(TASK_TYPES))
MILESTONE_ID_RE = re.compile(r"^M[1-9][0-9]*$")
ROUND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$")


def find_project_root(start: Path) -> Path | None:
    """Walk upward from `start` to find either the current or legacy project config."""
    cur = start.resolve()
    for p in (cur, *cur.parents):
        if has_project_config(p):
            return p
    return None


def has_project_config(root: Path) -> bool:
    return any((root / name).is_file() for name in (CONFIG_NAME, LEGACY_CONFIG_NAME))


def _migrate_project_config(root: Path) -> Path:
    legacy = root / LEGACY_CONFIG_NAME
    current = root / CONFIG_NAME
    if current.exists():
        if legacy.exists():
            print(
                f"waystone: legacy config {legacy} and new config {current} both exist; "
                f"using {current} and leaving {legacy} untouched",
                file=sys.stderr,
            )
        return current
    if legacy.is_file():
        os.rename(legacy, current)
    return current


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_config(cfg: dict | None) -> dict:
    """Apply defaults + validation to a parsed config mapping (from disk OR from a PR head)."""
    cfg = dict(cfg or {})
    cfg.setdefault("progress", "PROGRESS.md")
    cfg.setdefault("adr_dir", "docs/adr")
    cfg.setdefault("reviews_dir", "docs/reviews")
    cfg.setdefault("progress_archive_dir", "docs/progress")
    cfg.setdefault("generated_dir", "docs/ssot")
    cfg.setdefault("digest_max_lines", 150)
    gen = Path(cfg["generated_dir"])
    if gen.is_absolute() or ".." in gen.parts:
        raise ValueError(f"generated_dir must be a relative path inside the repo: {cfg['generated_dir']!r}")
    rv = cfg.setdefault("review", {})
    if not isinstance(rv, dict):
        raise ValueError("review: must be a mapping (mode/reviewers/require_ci/approvers/operators)")
    rv.setdefault("mode", "packet")  # packet | pr
    rv.setdefault("reviewers", ["codex", "gpt-5.5-pro"])
    rv.setdefault("require_ci", False)
    rv.setdefault("approvers", [])  # extra trusted approver logins beyond the repo owner
    # GitHub actors trusted to POST cycle/result/findings markers (beyond the repo owner). The
    # logical `reviewer` in a result marker is just a model id; `operators` is who vouched for it
    # on GitHub — a separate provenance, so a collaborator can't forge a macro reviewer's verdict.
    rv.setdefault("operators", [])
    if rv["mode"] not in ("packet", "pr"):
        raise ValueError(f"review.mode must be 'packet' or 'pr', got {rv['mode']!r}")
    if not (isinstance(rv["reviewers"], list) and all(isinstance(r, str) for r in rv["reviewers"])):
        raise ValueError("review.reviewers must be a list of strings")
    if not isinstance(rv["require_ci"], bool):
        raise ValueError("review.require_ci must be a boolean")
    if not (isinstance(rv["approvers"], list) and all(isinstance(a, str) for a in rv["approvers"])):
        raise ValueError("review.approvers must be a list of strings")
    if not (isinstance(rv["operators"], list) and all(isinstance(o, str) for o in rv["operators"])):
        raise ValueError("review.operators must be a list of strings")
    dl = cfg.setdefault("delegation", {})
    if not isinstance(dl, dict):
        raise ValueError("delegation: must be a mapping (env_prep)")
    dl.setdefault("env_prep", None)  # None -> lockfile auto-detection at delegation time (no sandbox knob, R7)
    ep = dl["env_prep"]
    if ep is not None and not (isinstance(ep, list) and all(isinstance(x, str) for x in ep)):
        raise ValueError("delegation.env_prep must be a list of shell command strings")
    return cfg


def load_config(root: Path) -> dict:
    return normalize_config(load_yaml(_migrate_project_config(root)))


def git_rc(root: Path, *args: str) -> tuple[int, str, str]:
    """Run git; return (returncode, stdout, stderr). Distinguishes failure from empty output."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return (127, "", str(e))
    return (out.returncode, out.stdout.strip(), out.stderr.strip())


def git_full_sha(root: Path, ref: str = "HEAD") -> str | None:
    """Full 40-char commit sha for `ref`, or None if it does not resolve."""
    rc, out, _ = git_rc(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return out if rc == 0 and out else None


def upstream_ref(root: Path) -> str | None:
    """The tracked upstream (e.g. 'origin/main') of the current branch, or None."""
    rc, out, _ = git_rc(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    return out if rc == 0 and out else None


def is_ancestor(root: Path, a: str, b: str) -> bool:
    """True iff commit `a` is an ancestor of (i.e. contained in) commit `b`."""
    rc, _, _ = git_rc(root, "merge-base", "--is-ancestor", a, b)
    return rc == 0


def head_pushed(root: Path, fetch: bool = True) -> tuple[bool, dict]:
    """Is the current HEAD contained in its tracked upstream (i.e. actually pushed)?
    Returns (pushed, info). Fail-closed: a fetch failure (network/auth/remote) returns
    (False, reason) rather than trusting a stale ref. Pass fetch=False for explicit offline use."""
    up = upstream_ref(root)
    if not up:
        return (False, {"reason": "no upstream tracking branch"})
    if fetch:
        rc, _, err = git_rc(root, "fetch", "--quiet", up.split("/", 1)[0])
        if rc != 0:
            return (False, {"reason": f"fetch failed — remote unverifiable: {err or 'error'}", "upstream": up})
    head = git_full_sha(root, "HEAD")
    pushed = is_ancestor(root, "HEAD", up)
    rc, out, _ = git_rc(root, "rev-list", "--count", f"HEAD..{up}")
    behind = int(out) if rc == 0 and out.isdigit() else None
    return (pushed, {"upstream": up, "head": head, "behind": behind})


def load_tasks(root: Path) -> dict:
    data = load_yaml(root / TASKS_NAME)
    return data if isinstance(data, dict) else {}


def git(root: Path, *args: str) -> str:
    """Run a git command in `root`; return stdout or '' on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_branch_info(root: Path) -> dict:
    branch = git(root, "branch", "--show-current") or "(detached)"
    dirty = len([ln for ln in git(root, "status", "--porcelain").splitlines() if ln])
    ahead_behind = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    behind, ahead = (ahead_behind.split() + ["", ""])[:2] if ahead_behind else ("?", "?")
    return {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind}


def next_actionable(data: dict, cap: int = 6) -> list[tuple[str, str]]:
    """Tasks ready to pick up next: pending/active (or stale-blocked — `blocked` with every dep
    already done) whose deps are all satisfied. Pure: returns [(id, title)] sorted by id."""
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    by_id = {t["id"]: t for t in tasks}
    out = []
    for t in tasks:
        if t.get("status") not in ("pending", "active", "blocked"):
            continue
        deps = t.get("deps", []) or []
        if all(by_id.get(d, {}).get("status") == "done" for d in deps):
            out.append((t["id"], t.get("title", "")))
    return sorted(out)[:cap]


def _project_slug(root: Path) -> str:
    rp = str(root.resolve())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", rp).strip("-")[:60].rstrip("-")
    return f"{slug}-{hashlib.sha1(rp.encode('utf-8')).hexdigest()[:8]}"


def resume_path(root: Path) -> Path:
    """Project-local EPHEMERAL re-entry snapshot (NOT committed to the repo). Written
    deterministically by the PreCompact/SessionEnd hook (structured: HEAD/round/tasks) and CONSUMED
    by the next SessionStart."""
    return project_state_path(root) / "resume.md"


def start_here_path(root: Path) -> Path:
    """Project-local PERSISTENT re-entry pointer (NOT committed, NOT consumed). The
    MODEL overwrites it at round close / after review with a bounded live-frontier narrative; the
    SessionStart hook injects it so a new/resumed session picks up without a manual 'pick up where
    we left off'. Complements the ephemeral structured resume_path — narrative vs. structured."""
    return project_state_path(root) / "start-here.md"


def slugify(text: str, max_len: int = 40) -> str:
    """Filename slug for generated SSOT sections. Keeps Hangul (Korean headings stay
    readable); task IDs are NOT slugified with this — their grammar stays ASCII."""
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "section"
