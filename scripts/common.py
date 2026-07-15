"""Shared helpers for waystone scripts (imported by sibling scripts)."""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_NAME = ".waystone.yml"
LEGACY_CONFIG_NAME = ".jahns-workflow.yml"
TASKS_NAME = "tasks.yaml"


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _real_directory(path: Path, label: str) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as e:
        raise WorkflowError(f"migration cannot inspect {label} {path}: {e}") from e
    if stat.S_ISLNK(mode):
        raise WorkflowError(f"migration refuses symlinked {label}: {path}")
    if not stat.S_ISDIR(mode):
        raise WorkflowError(f"migration {label} must be a directory: {path}")
    return True


def _regular_file(path: Path, label: str) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as e:
        raise WorkflowError(f"migration cannot inspect {label} {path}: {e}") from e
    if stat.S_ISLNK(mode):
        raise WorkflowError(f"migration refuses symlinked {label}: {path}")
    if not stat.S_ISREG(mode):
        raise WorkflowError(f"migration {label} must be a regular file: {path}")
    return True


def _migration_files(root: Path, label: str) -> list[Path]:
    if not _real_directory(root, label):
        return []
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir(), reverse=True):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise WorkflowError(f"migration refuses symlink in {label}: {path}")
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise WorkflowError(f"migration refuses unsupported entry in {label}: {path}")
    return sorted(files)


def _validate_legacy_root(root: Path) -> None:
    if not _real_directory(root, "legacy root"):
        return
    for child in sorted(root.iterdir()):
        mode = child.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise WorkflowError(f"migration refuses symlink in legacy root: {child}")
        if child.name != "worktrees":
            if stat.S_ISDIR(mode):
                _migration_files(child, "legacy data directory")
            elif not stat.S_ISREG(mode):
                raise WorkflowError(f"migration refuses unsupported legacy entry: {child}")
            continue
        if not stat.S_ISDIR(mode):
            raise WorkflowError(f"migration worktrees root must be a directory: {child}")
        for slug in sorted(child.iterdir()):
            if not _real_directory(slug, "legacy worktree slug directory"):
                continue
            for record in sorted(slug.iterdir()):
                _real_directory(record, "legacy worktree directory")


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


def _ensure_project_self_ignore(state: Path) -> None:
    """Create the project-state self-ignore once without racing another bootstrapper."""
    try:
        with (state / ".gitignore").open("x", encoding="utf-8") as stream:
            stream.write("*\n")
    except FileExistsError:
        pass


def ensure_project_state_dir(root: Path) -> Path:
    """Create the project-local state root and restore its self-ignore file when needed."""
    state = project_state_path(root)
    state.mkdir(parents=True, exist_ok=True)
    _real_directory(state, "project state directory")
    _ensure_project_self_ignore(state)
    return state


def worktrees_cache_dir(home: Path | None = None) -> Path:
    return machine_dir(home) / "cache" / "worktrees"


def registry_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "projects.json"


# Any nested acquisition must follow this single order: registry -> project -> record. Never acquire
# in reverse. Locking belongs to CLI/hook entry points; library functions below remain lock-free so
# composed verbs such as round close cannot deadlock themselves on flock's non-reentrant semantics.
# Intentionally unlocked (§2.4): warnings/decisions JSONL use one O_APPEND write; improve outputs are
# reproducible; SSOT views inherit round close's project lock (standalone regeneration is idempotent);
# start-here follows its single-writer round-close convention.
def registry_lock_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "registry.lock"


def project_lock_path(root: Path) -> Path:
    """Return the project lock path without touching the filesystem."""
    return project_state_path(root) / "lock"


def _lock_verb() -> str:
    """Best-effort diagnostic verb; flock, not this label, is the lock authority."""
    argv = [str(arg) for arg in sys.argv]
    program = Path(argv[0]).stem.removesuffix(".py") if argv else "waystone"
    args = argv[1:]
    if program == "waystone":
        return " ".join(args[:2]) or "waystone"
    group = {"tasks": "task", "tasks_guard": "tasks-guard"}.get(program, program)
    return " ".join([group, *args[:1]])


def _lock_timeout(timeout: float | None) -> float:
    raw = os.environ.get("WAYSTONE_LOCK_TIMEOUT", "10") if timeout is None else timeout
    try:
        value = float(raw)
    except (TypeError, ValueError) as e:
        raise WorkflowError(f"WAYSTONE_LOCK_TIMEOUT must be a non-negative finite number, got {raw!r}") from e
    if not math.isfinite(value) or value < 0:
        raise WorkflowError(f"WAYSTONE_LOCK_TIMEOUT must be a non-negative finite number, got {raw!r}")
    return value


def _lock_holder_message(path: Path, stream) -> str:
    try:
        stream.seek(0)
        holder = json.loads(stream.read() or "{}")
    except (OSError, ValueError, TypeError):
        holder = {}
    if not isinstance(holder, dict):
        holder = {}
    pid = holder.get("pid", "unknown")
    host = holder.get("host", "unknown")
    verb = holder.get("verb", "unknown")
    at = holder.get("at")
    try:
        since = datetime.fromisoformat(str(at)).strftime("%H:%M:%S")
    except ValueError:
        since = str(at or "unknown")
    return (f"waystone: {path} is held by pid {pid} ({host}, {verb}, since {since}) — "
            "retry after it finishes, or raise WAYSTONE_LOCK_TIMEOUT")


@contextmanager
def hold_lock(path: Path, timeout: float | None = None):
    """Hold one persistent flock marker; the file is diagnostic only and is never unlinked."""
    path = Path(path)
    wait = _lock_timeout(timeout)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _real_directory(path.parent, "lock directory")
        if path.name == "lock":
            _ensure_project_self_ignore(path.parent)
        stream = path.open("a+", encoding="utf-8")
    except OSError as e:
        raise WorkflowError(f"waystone: cannot open lock {path}: {e}") from e

    acquired = False
    started = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = wait - (time.monotonic() - started)
                if remaining <= 0:
                    raise WorkflowError(_lock_holder_message(path, stream))
                time.sleep(min(0.1, remaining))
            except OSError as e:
                raise WorkflowError(f"waystone: cannot lock {path}: {e}") from e

        holder = {
            "pid": os.getpid(),
            "host": os.environ.get("WAYSTONE_HOST", "unknown"),
            "verb": _lock_verb(),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(holder, ensure_ascii=False) + "\n")
            stream.flush()
        except OSError as e:
            raise WorkflowError(f"waystone: cannot write lock diagnostics {path}: {e}") from e
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        else:
            stream.close()


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
    if not _regular_file(path, "registry file"):
        return {"projects": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(f"migration registry unreadable/unparseable: {path} ({type(e).__name__})")
    if not isinstance(registry, dict) or not isinstance(registry.get("projects", []), list):
        raise WorkflowError(f"migration registry has wrong shape: {path}")
    registry.setdefault("projects", [])
    validate_registry_path_uniqueness(registry["projects"], path)
    return registry


def _normalized_registry_path(value: str, source: Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkflowError(f"registry {label} is not absolute: {source} ({value!r})")
    return path.resolve()


def registry_entry_paths(entry: object, source: Path) -> tuple[Path, ...]:
    """Return one local registry entry's normalized canonical+alias identity set."""
    if not isinstance(entry, dict):
        raise WorkflowError(f"registry entry is not an object: {source}")
    aliases = entry.get("aliases", [])
    if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias for alias in aliases):
        raise WorkflowError(f"registry entry aliases must be a list of non-empty paths: {source}")
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        if aliases:
            raise WorkflowError(f"registry aliases require a canonical path: {source}")
        return ()
    canonical = _normalized_registry_path(raw_path, source, "canonical path")
    normalized_aliases = tuple(
        _normalized_registry_path(alias, source, "alias path") for alias in aliases)
    return (canonical, *normalized_aliases)


def validate_registry_path_uniqueness(projects: list, source: Path) -> None:
    """Fail loud unless every normalized canonical or alias path has one registry owner."""
    owners: dict[Path, str] = {}
    for index, entry in enumerate(projects):
        label = (entry.get("name") if isinstance(entry, dict) else None) or f"entry {index}"
        for position, path in enumerate(registry_entry_paths(entry, source)):
            kind = "canonical" if position == 0 else "alias"
            owner = f"{label!r} {kind}"
            if path in owners:
                raise WorkflowError(
                    f"registry path {path} already belongs to {owners[path]}; "
                    f"cannot also assign it to {owner}")
            owners[path] = owner


def resolve_project_paths(project_root: Path, source: Path | None = None) -> tuple[Path, ...]:
    """Resolve a logical project's canonical+alias roots; an unregistered root resolves to itself."""
    path = registry_path() if source is None else source
    registry = _read_registry(path)
    wanted = Path(project_root).expanduser().resolve()
    for entry in registry["projects"]:
        identities = registry_entry_paths(entry, path)
        if wanted in identities:
            return identities
    return (wanted,)


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


def write_text_atomic(path: Path, text: str) -> None:
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


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(content)
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise


def _copy_file_atomic(source: Path, destination: Path, *, remove_source: bool) -> None:
    _regular_file(source, "source file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp",
                delete=False) as stream:
            tmp = Path(stream.name)
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)
        if remove_source:
            source.unlink()
    except BaseException:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise


def _move_entry(source: Path, destination: Path) -> None:
    mode = source.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise WorkflowError(f"migration refuses symlinked source entry: {source}")
    if stat.S_ISREG(mode):
        _copy_file_atomic(source, destination, remove_source=True)
        return
    if stat.S_ISDIR(mode):
        shutil.move(str(source), str(destination))
        return
    raise WorkflowError(f"migration refuses unsupported source entry: {source}")


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
    validate_registry_path_uniqueness(merged, destination)
    if merged != registry["projects"] or (not destination.exists() and merged):
        registry["projects"] = merged
        write_text_atomic(destination, json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    return merged


def _decision_lines(path: Path, order: int) -> list[tuple[float, int, int, str]]:
    if not _regular_file(path, "decisions file"):
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

    claude_root = next((root for host, root in sources if host == "claude"), None)
    if claude_root is not None:
        source = claude_root / "improve"
        if _real_directory(source, "legacy improve directory"):
            for child in sorted(source.iterdir()):
                if child.name == "decisions.jsonl":
                    continue
                target = machine_improve / child.name
                if _lexists(target):
                    print(
                        f"waystone migration: improve conflict {child} -> {target}; preserved source",
                        file=sys.stderr,
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                _move_entry(child, target)
                print(f"waystone migration: moved improve projection {child} -> {target}",
                      file=sys.stderr)

    for host, root in sources:
        if host != "codex":
            continue
        projection = root / "improve"
        if (_real_directory(projection, "legacy improve directory")
                and any(p.name != "decisions.jsonl" for p in projection.iterdir())):
            print(
                f"waystone migration: preserved regenerable Codex improve projection at {projection}; "
                "regenerate with `waystone improve trace --host codex`",
                file=sys.stderr,
            )

    decision_sources = [
        (host, root / "improve" / "decisions.jsonl") for host, root in sources]
    pending: list[tuple[Path, Path]] = []
    for _host, path in decision_sources:
        if not _regular_file(path, "decisions file"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        marker = machine_improve / f".merged-{digest}"
        if _lexists(marker):
            _regular_file(marker, "decisions merge marker")
            continue
        pending.append((path, marker))

    if pending:
        rows = _decision_lines(machine_improve / "decisions.jsonl", 0)
        for order, (path, _marker) in enumerate(pending, 1):
            rows.extend(_decision_lines(path, order))
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        write_text_atomic(
            machine_improve / "decisions.jsonl", "".join(f"{row[3]}\n" for row in rows))
        for marker in dict.fromkeys(marker for _path, marker in pending):
            if not _lexists(marker):
                write_text_atomic(marker, "")
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
        if _real_directory(base, f"legacy {area} directory"):
            for path in base.iterdir():
                if _real_directory(path, f"legacy {area} slug directory"):
                    slugs.add(path.name)
    for area in ("resume", "start_here"):
        base = root / area
        if _real_directory(base, f"legacy {area} directory"):
            for path in base.glob("*.md"):
                if _regular_file(path, f"legacy {area} file"):
                    slugs.add(path.stem)
    return slugs


_PROJECT_AREAS = {"resume", "start_here", "delegations", "overlay", "exposure"}


def _phase1_conflict_path(preserved: Path, child: Path) -> Path:
    return _unique_path(preserved / "phase1-conflicts" / child.name)


def _merge_phase1_project_area(child: Path, target: Path, preserved: Path) -> bool:
    changed = False
    for item in sorted(child.iterdir()):
        destination = target / item.name
        if _lexists(destination):
            continue
        _move_entry(item, destination)
        changed = True
    if not any(child.iterdir()):
        empty_target = _phase1_conflict_path(preserved, child)
        empty_target.parent.mkdir(parents=True, exist_ok=True)
        _move_entry(child, empty_target)
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
        write_text_atomic(marker, json.dumps(sorted(reported | slugs), indent=2) + "\n")


def _preserve_phase1_root(root: Path) -> None:
    _validate_legacy_root(root)
    preserved = _preserved_legacy_root(root)
    worktrees = root / "worktrees"
    if _lexists(preserved):
        _real_directory(preserved, "preserved legacy root")
    if not _lexists(preserved) and not _lexists(worktrees):
        os.rename(root, preserved)
        print(f"waystone migration: preserved legacy root {root} -> {preserved}", file=sys.stderr)
        return
    created = not _lexists(preserved)
    if created:
        preserved.mkdir(parents=True)
    changed = created
    for child in sorted(root.iterdir()):
        if child.name == "worktrees":
            continue
        target = preserved / child.name
        if _lexists(target):
            if (child.name in _PROJECT_AREAS
                    and _real_directory(child, "legacy project area")
                    and _real_directory(target, "preserved project area")):
                changed = _merge_phase1_project_area(child, target, preserved) or changed
                continue
            if (child.name == "profile.yml"
                    and _regular_file(child, "legacy profile")
                    and _regular_file(target, "preserved profile")
                    and child.read_bytes() == target.read_bytes()):
                conflict = _phase1_conflict_path(preserved, child)
                conflict.parent.mkdir(parents=True, exist_ok=True)
                _move_entry(child, conflict)
                changed = True
                continue
            if child.name != "profile.yml":
                conflict = _phase1_conflict_path(preserved, child)
                conflict.parent.mkdir(parents=True, exist_ok=True)
                _move_entry(child, conflict)
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
        _move_entry(child, target)
        changed = True
    if _lexists(worktrees) and changed:
        print(
            f"waystone migration: left legacy worktrees at {worktrees} so git back-links remain valid",
            file=sys.stderr,
        )
    if not _lexists(worktrees) and not any(root.iterdir()):
        empty_target = _unique_path(preserved / "phase1-reentries" / root.name)
        empty_target.parent.mkdir(parents=True, exist_ok=True)
        _move_entry(root, empty_target)


def migrate_home_data(home: Path | None = None) -> Path:
    """Phase 1: eagerly merge machine state and preserve both legacy host roots without deletion."""
    # 0.9.0-b wraps this entry point in registry.lock; C2 intentionally contains no flock logic.
    old = _legacy_data_dir(home)
    claude = _legacy_claude_root(home)
    old_present = _real_directory(old, "jahns-workflow legacy root")
    claude_present = _real_directory(claude, "Claude legacy root")
    if old_present and not claude_present:
        _validate_legacy_root(old)
        claude.mkdir(parents=True)
        for child in sorted(old.iterdir()):
            if child.name == "worktrees":
                continue
            _move_entry(child, claude / child.name)
        if not any(old.iterdir()):
            old.rmdir()
    elif old_present and claude_present:
        print(
            f"waystone: legacy data dir {old} and legacy waystone dir {claude} both exist; "
            f"leaving {old} untouched",
            file=sys.stderr,
        )

    roots = []
    for host, root in _legacy_roots(home):
        if _real_directory(root, f"{host} legacy root"):
            _validate_legacy_root(root)
            roots.append((host, root))
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
            if _real_directory(source, f"{host} legacy root") and key not in seen:
                _validate_legacy_root(source)
                seen.add(key)
                sources.append((host, source))
    return sources


def _phase2_worktree_sources(home: Path | None = None) -> list[tuple[str, Path]]:
    source = _legacy_data_dir(home)
    if not _real_directory(source, "jahns-workflow legacy root"):
        return []
    _validate_legacy_root(source)
    return [("claude", source)]


def _ensure_project_state_raw(root: Path) -> Path:
    state = Path(root) / ".waystone"
    if not _real_directory(state, "project state directory"):
        state.mkdir(parents=True)
    ignore = state / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != "*\n":
        ignore.write_text("*\n", encoding="utf-8")
    return state


def _unique_path(path: Path) -> Path:
    if not _lexists(path):
        return path
    for number in range(2, 10000):
        candidate = path.with_name(f"{path.stem}.{number}{path.suffix}")
        if not _lexists(candidate):
            return candidate
    raise WorkflowError(f"migration cannot allocate a preservation path beside {path}")


def _quarantine(state: Path, host: str, logical: Path, source: Path) -> Path:
    target = _unique_path(state / "migration-conflicts" / host / logical)
    target.parent.mkdir(parents=True, exist_ok=True)
    _move_entry(source, target)
    return target


def _migrate_file(state: Path, logical: Path, candidates: list[tuple[str, Path]]) -> None:
    live = state / logical
    live_present = _regular_file(live, "destination file")
    rows = []
    for host, path in candidates:
        if _regular_file(path, "legacy file"):
            rows.append({
                "host": host, "path": path, "mtime": path.stat().st_mtime_ns,
                "bytes": path.read_bytes(), "live": False,
            })
    if live_present:
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

    if live_present and not winner["live"]:
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
        _copy_file_atomic(winner["path"], live, remove_source=True)

    for row in rows:
        path = row["path"]
        if row["live"] or path == winner["path"] or not _lexists(path):
            continue
        if row["bytes"] == chosen:
            path.unlink()
            continue
        preserved = _quarantine(state, row["host"], logical, path)
        if row["bytes"] != chosen:
            print(
                f"waystone migration: conflict {logical}; kept newer live file and preserved "
                f"{row['host']} loser at {preserved}",
                file=sys.stderr,
            )


def _move_empty_source(state: Path, host: str, source: Path, logical: Path) -> None:
    if not _real_directory(source, "legacy source directory"):
        return
    if _migration_files(source, "legacy source directory"):
        return
    target = _unique_path(state / "migration-conflicts" / host / "empty-sources" / logical)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _migrate_tree(state: Path, logical: Path, sources: list[tuple[str, Path]]) -> None:
    groups: dict[Path, list[tuple[str, Path]]] = {}
    for host, source in sources:
        if not _real_directory(source, "legacy project directory"):
            continue
        for path in _migration_files(source, "legacy project directory"):
            groups.setdefault(path.relative_to(source), []).append((host, path))
    for relative in sorted(groups, key=str):
        _migrate_file(state, logical / relative, groups[relative])
    for host, source in sources:
        _move_empty_source(state, host, source, logical)


def _profile_seed(root: Path, state: Path, sources: list[tuple[str, Path]]) -> None:
    live = state / "profile.yml"
    if _regular_file(live, "project profile"):
        return
    profiles = [(host, source / "profile.yml") for host, source in sources
                if _regular_file(source / "profile.yml", "legacy profile")]
    if not profiles:
        return
    bodies = {path.read_bytes() for _host, path in profiles}
    if len(bodies) != 1:
        paths = ", ".join(str(path) for _host, path in profiles)
        raise WorkflowError(
            f"legacy profile conflict for {root}: {paths}; choose the project profile manually")
    chosen = next((path for host, path in profiles if host == "claude"), profiles[0][1])
    _ensure_project_state_raw(root)
    _copy_file_atomic(chosen, live, remove_source=False)
    print(
        f"waystone migration: seeded project profile {live} from {chosen}; legacy seed preserved",
        file=sys.stderr,
    )


def _overlay_rule_conflicts(
        root: Path, state: Path, sources: list[tuple[str, Path]], slug: str) -> set[Path]:
    by_rule: dict[str, list[tuple[str, Path, bytes, bool]]] = {}
    for host, source in sources:
        deltas = source / "overlay" / slug / "deltas"
        if not _real_directory(deltas, "legacy overlay deltas directory"):
            continue
        for path in sorted(deltas.glob("*.json")):
            _regular_file(path, "legacy overlay delta")
            try:
                delta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise WorkflowError(f"legacy overlay delta unreadable: {path} ({e})")
            rule = delta.get("rule") if isinstance(delta, dict) else None
            if not isinstance(rule, str) or not rule:
                raise WorkflowError(f"legacy overlay delta has no rule id: {path}")
            by_rule.setdefault(rule, []).append((host, path, path.read_bytes(), False))

    live_deltas = state / "overlay" / "deltas"
    if _real_directory(live_deltas, "live overlay deltas directory"):
        for path in sorted(live_deltas.glob("*.json")):
            _regular_file(path, "live overlay delta")
            try:
                delta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise WorkflowError(f"live overlay delta unreadable: {path} ({e})")
            rule = delta.get("rule") if isinstance(delta, dict) else None
            if not isinstance(rule, str) or not rule:
                raise WorkflowError(f"live overlay delta has no rule id: {path}")
            by_rule.setdefault(rule, []).append(("live", path, path.read_bytes(), True))

    cleanup: set[Path] = set()
    for rule, entries in sorted(by_rule.items()):
        incoming = [entry for entry in entries if not entry[3]]
        incoming_hosts = {entry[0] for entry in incoming}
        live = [entry for entry in entries if entry[3]]
        if not incoming or (not live and len(incoming_hosts) < 2):
            continue
        if len({entry[2] for entry in entries}) != 1:
            paths = ", ".join(str(entry[1]) for entry in entries)
            raise WorkflowError(
                f"overlay rule-id conflict {rule!r} for {root}: {paths}; human selection required")
        if live:
            cleanup.update(entry[1] for entry in incoming)
        else:
            cleanup.update(entry[1] for entry in incoming[1:])
    return cleanup


def _record_snapshot(record: Path, label: str) -> tuple[tuple[str, str, bytes], ...]:
    _real_directory(record, label)
    rows: list[tuple[str, str, bytes]] = []
    pending = [record]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            relative = str(path.relative_to(record))
            if stat.S_ISLNK(mode):
                raise WorkflowError(f"migration refuses symlink in {label}: {path}")
            if stat.S_ISDIR(mode):
                rows.append((relative, "directory", b""))
                pending.append(path)
            elif stat.S_ISREG(mode):
                rows.append((relative, "file", path.read_bytes()))
            else:
                raise WorkflowError(f"migration refuses unsupported entry in {label}: {path}")
    return tuple(sorted(rows))


def _delegation_sources(
        sources: list[tuple[str, Path]], slug: str) -> dict[str, list[tuple[str, Path]]]:
    by_did: dict[str, list[tuple[str, Path]]] = {}
    for host, source in sources:
        base = source / "delegations" / slug
        if not _real_directory(base, "legacy delegation slug directory"):
            continue
        for record in sorted(base.iterdir()):
            if _real_directory(record, "legacy delegation record"):
                by_did.setdefault(record.name, []).append((host, record))
    return by_did


def _worktree_sources(
        sources: list[tuple[str, Path]], slug: str) -> dict[str, list[tuple[str, Path]]]:
    by_did: dict[str, list[tuple[str, Path]]] = {}
    for host, source in sources:
        base = source / "worktrees" / slug
        if not _real_directory(base, "legacy worktree slug directory"):
            continue
        for worktree in sorted(base.iterdir()):
            if _real_directory(worktree, "legacy worktree directory"):
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
    if not _real_directory(record, "live delegation record"):
        return
    status_path = record / "status.json"
    status: dict = {}
    if _regular_file(status_path, "delegation status file"):
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
    write_text_atomic(status_path, json.dumps(status, ensure_ascii=False, indent=2) + "\n")


def _worktree_migration_marker(new: Path) -> Path:
    return new.with_name(f"{new.name}.migrating")


def _worktree_is_valid(path: Path) -> tuple[bool, str]:
    rc, _out, error = git_rc(path, "rev-parse", "--git-dir")
    return rc == 0, error or str(rc)


def _finish_worktree_fallback(
        root: Path, state: Path, did: str, old: Path, new: Path, marker: Path,
        move_error: str) -> bool:
    repair_rc, _out, repair_error = git_rc(root, "worktree", "repair", str(new))
    valid, validation_error = _worktree_is_valid(new) if repair_rc == 0 else (False, "")
    if repair_rc == 0 and valid:
        marker.unlink()
        git_rc(root, "worktree", "unlock", str(new))
        print(
            f"waystone migration: git worktree move failed ({move_error}); "
            f"moved {old} -> {new} and repaired git metadata",
            file=sys.stderr,
        )
        return True

    reason = "; ".join(part for part in (
        f"git worktree move: {move_error}",
        f"fallback repair: {repair_error or repair_rc}" if repair_rc else "",
        f"fallback validation: {validation_error}" if repair_rc == 0 and not valid else "",
    ) if part)
    _mark_worktree_discard_only(state, did, reason)
    print(
        f"waystone migration: WARNING — WORKTREE MIGRATION FAILED FOR {did}; "
        f"DELEGATION IS DISCARD-ONLY. {reason}",
        file=sys.stderr,
    )
    return False


def _pending_worktree_markers(slug: str) -> dict[str, tuple[Path, Path, Path]]:
    directory = worktrees_cache_dir() / slug
    if not _real_directory(directory, "worktree cache slug directory"):
        return {}
    pending = {}
    for marker in sorted(directory.glob("*.migrating")):
        _regular_file(marker, "worktree migration marker")
        did = marker.name.removesuffix(".migrating")
        old_text = marker.read_text(encoding="utf-8")
        old = Path(old_text)
        if not old.is_absolute() or old.name != did or old.parent.name != slug:
            raise WorkflowError(f"invalid worktree migration marker {marker}: {old_text!r}")
        new = directory / did
        if _lexists(new):
            _real_directory(new, "migrating worktree destination")
            if _lexists(old):
                raise WorkflowError(
                    f"worktree migration has both source and destination: {old}, {new}")
        pending[did] = (old, new, marker)
    return pending


def _migrate_worktree(root: Path, state: Path, slug: str, did: str, old: Path) -> None:
    new = worktrees_cache_dir() / slug / did
    new.parent.mkdir(parents=True, exist_ok=True)
    marker = _worktree_migration_marker(new)
    resuming = False
    if _lexists(marker):
        resuming = True
        _regular_file(marker, "worktree migration marker")
        marker_old = marker.read_text(encoding="utf-8")
        if marker_old != str(old):
            raise WorkflowError(
                f"worktree migration marker source mismatch: {marker} contains {marker_old!r}, "
                f"expected {str(old)!r}")
        if _lexists(new):
            if _lexists(old):
                raise WorkflowError(
                    f"worktree migration has both source and destination: {old}, {new}")
            _finish_worktree_fallback(root, state, did, old, new, marker, "previous attempt")
            return
    move_rc, _out, move_err = ((1, "", "previous attempt") if resuming else
                               git_rc(root, "worktree", "move", str(old), str(new)))
    if move_rc == 0:
        print(f"waystone migration: moved worktree {old} -> {new} with git worktree move",
              file=sys.stderr)
        return

    filesystem_error = ""
    if _lexists(new):
        filesystem_error = f"destination already exists: {new}"
    else:
        git_rc(root, "worktree", "lock", str(old))
        write_text_atomic(marker, str(old))
        try:
            shutil.move(str(old), str(new))
        except OSError as e:
            filesystem_error = str(e)
    if filesystem_error:
        reason = "; ".join((
            f"git worktree move: {move_err or move_rc}",
            f"fallback move: {filesystem_error}",
        ))
        _mark_worktree_discard_only(state, did, reason)
        print(
            f"waystone migration: WARNING — WORKTREE MIGRATION FAILED FOR {did}; "
            f"DELEGATION IS DISCARD-ONLY. {reason}",
            file=sys.stderr,
        )
        return
    _finish_worktree_fallback(
        root, state, did, old, new, marker, move_err or str(move_rc))


def migrate_project_state(root: Path, home: Path | None = None) -> bool:
    """Phase 2: lazily move one project's legacy host-keyed state into its project-local tier."""
    # 0.9.0-b adds the short project-lock span around this entry point; C2 must remain lock-free.
    root = Path(root).resolve()
    slug = _project_slug(root)
    pending_worktrees = _pending_worktree_markers(slug)
    sources = _phase2_sources(home)
    worktree_sources = _phase2_worktree_sources(home)
    if not sources and not worktree_sources and not pending_worktrees:
        return False
    state = root / ".waystone"
    try:
        _real_directory(state, "project state directory")
        profiles_present = any(
            _regular_file(source / "profile.yml", "legacy profile")
            for _host, source in sources)
        profile_needs_seed = profiles_present and not _lexists(state / "profile.yml")
        profiles = [(host, source / "profile.yml") for host, source in sources
                    if _regular_file(source / "profile.yml", "legacy profile")]
        if len({path.read_bytes() for _host, path in profiles}) > 1:
            paths = ", ".join(str(path) for _host, path in profiles)
            raise WorkflowError(
                f"legacy profile conflict for {root}: {paths}; choose the project profile manually")
        migrated_overlay_paths = _overlay_rule_conflicts(root, state, sources, slug)

        resume = [(host, source / "resume" / f"{slug}.md") for host, source in sources]
        start_here = [(host, source / "start_here" / f"{slug}.md") for host, source in sources]
        trees = {
            "overlay": [(host, source / "overlay" / slug) for host, source in sources],
            "exposure": [(host, source / "exposure" / slug) for host, source in sources],
        }
        delegations = _delegation_sources(sources, slug)
        worktrees = _worktree_sources(sources + worktree_sources, slug)
        has_project_items = any(path.is_file() for _host, path in resume + start_here)
        has_project_items = has_project_items or any(
            path.is_dir() for source_list in trees.values() for _host, path in source_list)
        has_project_items = has_project_items or bool(delegations) or bool(worktrees)
        has_project_items = has_project_items or bool(pending_worktrees)
        if not profile_needs_seed and not has_project_items:
            return False

        _ensure_project_state_raw(root)
        for path in migrated_overlay_paths:
            path.unlink()
        for did, (old, new, marker) in pending_worktrees.items():
            if not _lexists(new):
                if not _lexists(old):
                    raise WorkflowError(
                        f"worktree migration marker has neither source nor destination: {marker}")
                continue
            _finish_worktree_fallback(
                root, state, did, old, new, marker, "previous attempt")
        _profile_seed(root, state, sources)
        _migrate_file(state, Path("resume.md"), resume)
        _migrate_file(state, Path("start-here.md"), start_here)
        for area, area_sources in trees.items():
            _migrate_tree(state, Path(area), area_sources)

        blocked = set()
        for did, candidates in delegations.items():
            live_record = state / "delegations" / did
            if _lexists(live_record):
                live_snapshot = _record_snapshot(live_record, "live delegation record")
                if all(
                        _record_snapshot(record, "legacy delegation record") == live_snapshot
                        for _host, record in candidates):
                    for _host, record in candidates:
                        shutil.rmtree(record)
                    continue
                blocked.add(did)
                _warn_did_collision(did, candidates)
                continue
            if len(candidates) > 1:
                blocked.add(did)
                _warn_did_collision(did, candidates)
                continue
            _migrate_tree(state, Path("delegations") / did, candidates)

        for did, candidates in worktrees.items():
            if did in pending_worktrees and not _lexists(candidates[0][1]):
                continue
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
    invalid_role_refs = [
        reviewer for reviewer in rv["reviewers"]
        if reviewer.startswith("role:") and reviewer != "role:reviewer"
    ]
    if invalid_role_refs:
        raise ValueError(
            "review.reviewers role references must be exactly 'role:reviewer'; "
            f"got {invalid_role_refs[0]!r}")
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
