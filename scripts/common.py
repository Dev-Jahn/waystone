"""Shared helpers for waystone scripts (imported by sibling scripts)."""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIG_NAME = ".waystone.yml"
TASKS_NAME = "tasks.yaml"


class WorkflowError(Exception):
    """A recoverable workflow error raised by library helpers. Library code must raise this (an
    ordinary Exception, catchable by rollback logic) rather than calling sys.exit() — only CLI
    main() converts it to an exit code. (sys.exit raises SystemExit/BaseException, which slips past
    `except Exception` rollbacks.)"""


class Pre09StateError(WorkflowError):
    """Typed refusal for unresolved state from the removed pre-0.9 migration subsystem."""

    code = "unsupported_pre_0_9_layout"

    def __init__(self, paths: list[Path]):
        self.paths = tuple(sorted({Path(path) for path in paths}, key=str))
        locations = ", ".join(map(str, self.paths))
        super().__init__(
            f"{self.code}: found pre-0.9 Waystone state at {locations}; Waystone 0.12 does not "
            "migrate or repair this layout. Run a released Waystone 0.11.x once on this machine "
            "and project, then retry; otherwise migrate the state manually.")


def _real_directory(path: Path, label: str) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as e:
        raise WorkflowError(f"waystone cannot inspect {label} {path}: {e}") from e
    if stat.S_ISLNK(mode):
        raise WorkflowError(f"waystone refuses symlinked {label}: {path}")
    if not stat.S_ISDIR(mode):
        raise WorkflowError(f"waystone {label} must be a directory: {path}")
    return True


def _regular_file(path: Path, label: str) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as e:
        raise WorkflowError(f"waystone cannot inspect {label} {path}: {e}") from e
    if stat.S_ISLNK(mode):
        raise WorkflowError(f"waystone refuses symlinked {label}: {path}")
    if not stat.S_ISREG(mode):
        raise WorkflowError(f"waystone {label} must be a regular file: {path}")
    return True


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
    """Atomically restore the canonical project-state self-ignore when absent or damaged."""
    ignore = state / ".gitignore"
    try:
        info = ignore.lstat()
        if stat.S_ISREG(info.st_mode) and ignore.read_bytes() == b"*\n":
            return
    except FileNotFoundError:
        pass
    write_text_atomic(ignore, "*\n")


def ensure_project_state_dir(root: Path) -> Path:
    """Create the project-local state root and restore its self-ignore file when needed."""
    require_initialized_root(root)
    state = project_state_path(root)
    state.mkdir(parents=True, exist_ok=True)
    _real_directory(state, "project state directory")
    _ensure_project_self_ignore(state)
    return state


def consent_path(root: Path) -> Path:
    """Project-local append-only consent event log."""
    return project_state_path(root) / "consents.jsonl"


def canonical_payload_hash(payload: object) -> str:
    """SHA-256 over the unique compact JSON encoding used to bind consent to a candidate."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def record_consent(root: Path, surface: str, choice: str, context: dict | None = None) -> dict:
    """Append one standard consent event after the host has presented the choice to the user."""
    if not isinstance(surface, str) or not surface.strip():
        raise WorkflowError("consent surface must be a non-empty string")
    if not isinstance(choice, str) or not choice.strip():
        raise WorkflowError("consent choice must be a non-empty string")
    if context is None:
        context = {}
    if (not isinstance(context, dict)
            or any(not isinstance(key, str) or not isinstance(value, (str, int, float, bool, type(None)))
                   for key, value in context.items())):
        raise WorkflowError("consent context must be a flat object with scalar values")
    row = {
        "surface": surface.strip(), "choice": choice.strip(),
        "at": datetime.now(timezone.utc).isoformat(), "context": dict(sorted(context.items())),
    }
    ensure_project_state_dir(root)
    with consent_path(root).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def has_accepted_consent(root: Path, surface: str, context: dict) -> bool:
    """Whether the latest valid event for an exact surface/context pair is an acceptance."""
    path = consent_path(root)
    if not path.is_file():
        return False
    latest = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise WorkflowError(f"consent log unreadable: {path} ({e})") from e
    expected = dict(sorted(context.items()))
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise WorkflowError(f"corrupt consent log {path}:{line_number} ({e})") from e
        if (not isinstance(row, dict) or not isinstance(row.get("surface"), str)
                or not isinstance(row.get("choice"), str) or not isinstance(row.get("at"), str)
                or not isinstance(row.get("context"), dict)):
            raise WorkflowError(f"corrupt consent log {path}:{line_number}")
        if row["surface"] == surface and row["context"] == expected:
            latest = row
    return latest is not None and latest["choice"] == "accept"


def worktrees_cache_dir(home: Path | None = None) -> Path:
    return machine_dir(home) / "cache" / "worktrees"


def registry_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "projects.json"


# Any nested acquisition must follow this single order: registry -> overlay -> project -> record.
# Never acquire in reverse. Locking normally belongs to CLI/hook entry points; the cross-project
# promote-user transaction owns all three leading locks itself because its evidence read and user
# overlay write must be one machine-wide snapshot.
# Intentionally unlocked (§2.4): warnings/decisions JSONL use one O_APPEND write; improve outputs are
# reproducible; SSOT views inherit round close's project lock (standalone regeneration is idempotent);
# start-here follows its single-writer round-close convention.
def registry_lock_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "registry.lock"


def overlay_lock_path(home: Path | None = None) -> Path:
    return machine_dir(home) / "overlay.lock"


def project_lock_path(root: Path) -> Path:
    """Return the project lock path without touching the filesystem."""
    return project_state_path(root) / "lock"


def require_initialized_root(root: Path) -> None:
    """The single write gate for project-local state: creating .waystone under a root that has no
    project config scaffolds state at an arbitrary path (the `task drop --reason` incident seeded a
    profile into a typo'd directory). Initialization needs no bypass here — init writes .waystone.yml
    before the first state-creating CLI call (skills/init: Step 3 precedes consent recording), so an
    uninitialized root reaching this gate is always a caller error."""
    if has_project_config(Path(root)):
        return
    raise WorkflowError(
        f"waystone: {root} is not an initialized waystone project (.waystone.yml missing) — "
        "refusing to create project state there; check the path or initialize the project first")


def hold_project_lock(root: Path, timeout: float | None = None):
    """Project-lock chokepoint: every flow that creates or mutates project-local state acquires
    the lock through here, so the initialized-root gate lives at this one point instead of being
    re-checked per entry point."""
    require_initialized_root(root)
    return hold_lock(project_lock_path(root), timeout=timeout)


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


def _pre_0_9_host_roots(home: Path | None = None) -> tuple[Path, ...]:
    base_home = Path.home() if home is None else Path(home)
    codex_home = (Path(os.environ["CODEX_HOME"]).expanduser()
                  if os.environ.get("CODEX_HOME") else base_home / ".codex")
    roots = (base_home / ".claude" / "waystone", codex_home / "waystone")
    seen: set[str] = set()
    unique = []
    for root in roots:
        key = str(root.expanduser().absolute())
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


def _preserved_pre_0_9_root(root: Path) -> Path:
    return root.with_name(f"{root.name}.pre-0.9")


def _checked_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise WorkflowError(
            f"pre_0_9_layout_check_failed: cannot inspect legacy state path {path}: {e}") from e


def _checked_entries(path: Path) -> list[Path] | None:
    info = _checked_lstat(path)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return []
    try:
        return sorted(path.iterdir())
    except OSError as e:
        raise WorkflowError(
            f"pre_0_9_layout_check_failed: cannot enumerate legacy state path {path}: {e}") from e


def _unresolved_pre_0_9_machine_paths(home: Path | None = None) -> list[Path]:
    offenders: list[Path] = []
    project_areas = {"resume", "start_here", "delegations", "overlay", "exposure", "worktrees"}
    for root in _pre_0_9_host_roots(home):
        entries = _checked_entries(root)
        if entries is None:
            continue
        info = _checked_lstat(root)
        if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)):
            offenders.append(root)
            continue
        for child in entries:
            if child.name in project_areas:
                continue
            if child.name == "improve":
                nested = _checked_entries(child)
                if nested is None:
                    continue
                child_info = _checked_lstat(child)
                if (child_info is not None
                        and (stat.S_ISLNK(child_info.st_mode)
                             or not stat.S_ISDIR(child_info.st_mode))):
                    offenders.append(child)
                elif nested:
                    offenders.append(child)
                continue
            offenders.append(child)
    return offenders


def _append_existing(path: Path, offenders: list[Path]) -> None:
    if _checked_lstat(path) is not None:
        offenders.append(path)


def _append_children(path: Path, offenders: list[Path]) -> None:
    entries = _checked_entries(path)
    if entries is None:
        return
    info = _checked_lstat(path)
    if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)):
        offenders.append(path)
        return
    offenders.extend(entries)


def _append_preserved_profile_conflicts(
        root: Path, home: Path | None, offenders: list[Path]) -> None:
    profiles: list[Path] = []
    for plain in _pre_0_9_host_roots(home):
        preserved = _preserved_pre_0_9_root(plain)
        preserved_info = _checked_lstat(preserved)
        if preserved_info is None:
            continue
        if stat.S_ISLNK(preserved_info.st_mode) or not stat.S_ISDIR(preserved_info.st_mode):
            offenders.append(preserved)
            continue
        profile = preserved / "profile.yml"
        info = _checked_lstat(profile)
        if info is None:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            offenders.append(profile)
            continue
        profiles.append(profile)
    if not profiles:
        return

    state = root / ".waystone"
    state_info = _checked_lstat(state)
    if state_info is not None and (
            stat.S_ISLNK(state_info.st_mode) or not stat.S_ISDIR(state_info.st_mode)):
        offenders.append(state)
        return
    live = state / "profile.yml"
    live_info = _checked_lstat(live)
    if live_info is not None:
        if stat.S_ISLNK(live_info.st_mode) or not stat.S_ISREG(live_info.st_mode):
            offenders.append(live)
            return

    try:
        bodies = {profile.read_bytes() for profile in profiles}
    except OSError as e:
        raise WorkflowError(
            f"pre_0_9_layout_check_failed: cannot read legacy profile: {e}") from e
    if len(bodies) > 1:
        offenders.extend(profiles)


def _unresolved_pre_0_9_project_paths(
        root: Path, home: Path | None = None) -> list[Path]:
    offenders = _unresolved_pre_0_9_machine_paths(home)
    _append_preserved_profile_conflicts(root, home, offenders)
    slug = _project_slug(root)
    sources = [
        source
        for plain in _pre_0_9_host_roots(home)
        for source in (plain, _preserved_pre_0_9_root(plain))
    ]
    for source in sources:
        source_info = _checked_lstat(source)
        if source_info is not None and (
                stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode)):
            if source not in _pre_0_9_host_roots(home):
                offenders.append(source)
            continue
        _append_existing(source / "resume" / f"{slug}.md", offenders)
        _append_existing(source / "start_here" / f"{slug}.md", offenders)
        _append_existing(source / "overlay" / slug, offenders)
        _append_existing(source / "exposure" / slug, offenders)
        _append_children(source / "delegations" / slug, offenders)
        _append_children(source / "worktrees" / slug, offenders)

    marker_root = machine_dir(home)
    marker_ancestors = (
        marker_root,
        marker_root / "cache",
        worktrees_cache_dir(home),
    )
    for marker_ancestor in marker_ancestors:
        marker_info = _checked_lstat(marker_ancestor)
        if marker_info is None:
            return offenders
        if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISDIR(marker_info.st_mode):
            offenders.append(marker_ancestor)
            return offenders

    marker_dir = marker_ancestors[-1] / slug
    marker_entries = _checked_entries(marker_dir)
    if marker_entries is not None:
        marker_info = _checked_lstat(marker_dir)
        if marker_info is not None and (
                stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISDIR(marker_info.st_mode)):
            offenders.append(marker_dir)
        else:
            offenders.extend(path for path in marker_entries if path.name.endswith(".migrating"))
    return offenders


def require_supported_machine_state(home: Path | None = None) -> Path:
    """Refuse unresolved pre-0.9 machine state without moving or repairing any bytes."""
    offenders = _unresolved_pre_0_9_machine_paths(home)
    if offenders:
        raise Pre09StateError(offenders)
    return machine_dir(home)


def require_supported_project_state(root: Path, home: Path | None = None) -> bool:
    """Refuse unresolved pre-0.9 state for one project; current layouts are a no-op."""
    root = Path(root).resolve()
    offenders = _unresolved_pre_0_9_project_paths(root, home)
    if offenders:
        raise Pre09StateError(offenders)
    return False


def migrate_home_data(home: Path | None = None) -> Path:
    """Compatibility entry point for callers predating the migration subsystem sunset."""
    return require_supported_machine_state(home)


def migrate_project_state(root: Path, home: Path | None = None) -> bool:
    """Compatibility entry point; automatic migration was removed in Waystone 0.12."""
    return require_supported_project_state(root, home)


def _read_registry(path: Path) -> dict:
    if not _regular_file(path, "registry file"):
        return {"projects": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WorkflowError(f"registry unreadable/unparseable: {path} ({type(e).__name__})")
    if not isinstance(registry, dict) or not isinstance(registry.get("projects", []), list):
        raise WorkflowError(f"registry has wrong shape: {path}")
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


def _record_scope_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip("`'\"")
    candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if (not candidate or candidate.startswith(("/", "~")) or "://" in candidate
            or "\\" in candidate or any(part == ".." for part in candidate.split("/"))
            or any(char.isspace() for char in candidate)):
        return None
    return candidate


def normalize_scope_prefix(value: object) -> str | None:
    """Canonical repo-relative prefix for task.scope and packet.declared_scope."""
    if not isinstance(value, str):
        return None
    path = value.strip()
    if path.startswith("./"):
        path = path[2:]
    if (not path or path.startswith(("/", "~")) or ":" in path or "\\" in path
            or any(part in ("", "..") for part in path.rstrip("/").split("/"))
            or any(char.isspace() for char in path) or any(char in path for char in "*?[")):
        return None
    return path.rstrip("/") or None


def canonical_scope_prefixes(value: object) -> list[str]:
    """Validate structured scope without mining any natural-language field."""
    if not isinstance(value, list):
        raise WorkflowError("scope must be a list of repo-relative path prefixes")
    out: list[str] = []
    for index, raw in enumerate(value):
        path = normalize_scope_prefix(raw)
        if path is None:
            raise WorkflowError(
                f"scope[{index}] must be a repo-relative path prefix without glob, '..', URL, or whitespace")
        if path not in out:
            out.append(path)
    return out


def parse_iso_timestamp(value: object) -> datetime | None:
    """Parse a timezone-qualified ISO-8601 timestamp; return None for ambiguous/invalid input."""
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _packet_declared_scope(packet: dict) -> tuple[list[str], str]:
    try:
        paths = canonical_scope_prefixes(packet.get("declared_scope"))
    except WorkflowError:
        return [], "unknown"
    return (paths, "explicit") if paths else ([], "unknown")


def _path_in_declared_scope(path: str, declared: list[str]) -> bool:
    for scope in declared:
        if path == scope or path.startswith(scope + "/"):
            return True
    return False


def delegation_scope_drift(record_dir: Path) -> dict:
    """Compare one delegation packet's declared path scope with its computed changed files.

    The record directory is the whole interface so live boundary rules can reuse the same calculation.
    Only packet.declared_scope is consumed. Notes, anchors, commands, URLs, and acceptance text are
    never interpreted as paths; absent structured scope remains unknown.
    """
    packet_path = Path(record_dir) / "packet.yaml"
    contract_path = Path(record_dir) / "artifact" / "contract.yaml"
    base = {
        "rule": "packet-declared-scope-v2", "evaluable": False, "provenance": "unknown",
        "fired": False, "declared_scope": [], "changed_files": [], "outside_scope": [],
    }
    for path, label in ((packet_path, "packet"), (contract_path, "contract")):
        if not path.is_file():
            return {**base, "coverage_reason": f"missing-{label}"}
    try:
        packet = yaml.safe_load(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {**base, "coverage_reason": "unreadable-packet"}
    try:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {**base, "coverage_reason": "unreadable-contract"}
    if not isinstance(packet, dict):
        return {**base, "coverage_reason": "invalid-packet"}
    if not isinstance(contract, dict):
        return {**base, "coverage_reason": "invalid-contract"}

    declared, provenance = _packet_declared_scope(packet)
    if not declared:
        return {**base, "coverage_reason": "scope-unknown"}
    raw_changed = contract.get("changed_files")
    if not isinstance(raw_changed, list):
        return {**base, "declared_scope": declared, "provenance": provenance,
                "coverage_reason": "invalid-changed-files"}
    changed: list[str] = []
    for row in raw_changed:
        path = _record_scope_path(row.get("path") if isinstance(row, dict) else None)
        if path is None:
            return {**base, "declared_scope": declared, "provenance": provenance,
                    "coverage_reason": "invalid-changed-files"}
        changed.append(path)
    changed = sorted(set(changed))
    outside = [path for path in changed if not _path_in_declared_scope(path, declared)]
    return {
        "rule": "packet-declared-scope-v2", "evaluable": True, "provenance": provenance,
        "fired": bool(outside),
        "declared_scope": declared, "changed_files": changed, "outside_scope": outside,
        "coverage_reason": None,
    }


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


TASK_TYPES = ("feat", "fix", "perf", "gate", "spike", "decision", "docs", "chore")
TASK_STATUSES = ("pending", "active", "blocked", "parked", "done", "dropped")
MILESTONE_STATUSES = ("pending", "active", "done")
SEVERITIES = ("blocker", "major", "minor")

TASK_ID_RE = re.compile(r"^(?:%s)/[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$" % "|".join(TASK_TYPES))
MILESTONE_ID_RE = re.compile(r"^M[1-9][0-9]*$")
ROUND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$")


def find_project_root(start: Path) -> Path | None:
    """Walk upward from `start` to find the current project config."""
    cur = start.resolve()
    for p in (cur, *cur.parents):
        if has_project_config(p):
            return p
    return None


def has_project_config(root: Path) -> bool:
    return (root / CONFIG_NAME).is_file()


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_config(cfg: dict | None, *, source: Path | None = None) -> dict:
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
    # `waystone:init` writes role:reviewer explicitly for new projects. A config created by an
    # older release may omit the field entirely, so preserve that release's implicit literals.
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
        raise ValueError(
            "delegation: must be a mapping (enabled/env_prep)")
    if "codex_runner_verified" in dl:
        if source is not None:
            marker_path = source.parent / ".waystone" / "codex-runner-verified"
            print(
                f"waystone: legacy delegation.codex_runner_verified in {source} is ignored; "
                f"remove the key from {source}; Codex runner proof is checkout-local at "
                f"{marker_path}",
                file=sys.stderr,
            )
        dl.pop("codex_runner_verified")
    dl.setdefault("enabled", True)
    if not isinstance(dl["enabled"], bool):
        raise ValueError("delegation.enabled must be a boolean")
    dl.setdefault("env_prep", None)  # None -> lockfile auto-detection at delegation time (no sandbox knob, R7)
    ep = dl["env_prep"]
    if ep is not None and not (isinstance(ep, list) and all(isinstance(x, str) for x in ep)):
        raise ValueError("delegation.env_prep must be a list of shell command strings")
    policy = cfg.setdefault("policy", {})
    if not isinstance(policy, dict):
        raise ValueError("policy: must be a mapping (start_level)")
    # Before start_level had a runtime consumer, omitted fields still emitted warning-stage stderr.
    # Preserve that behavior for existing projects; init writes an explicit user choice for new ones.
    policy.setdefault("start_level", "warn-allowed")
    if policy["start_level"] not in ("observe-only", "warn-allowed"):
        raise ValueError("policy.start_level must be 'observe-only' or 'warn-allowed'")
    return cfg


def load_config(root: Path) -> dict:
    path = root / CONFIG_NAME
    return normalize_config(load_yaml(path), source=path)


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
    tracking = _upstream_tracking(root)
    return tracking[0] if tracking is not None else None


def _upstream_tracking(root: Path) -> tuple[str, str, str] | None:
    """Return (display name, remote, exact remote branch ref) without resolving its SHA."""
    rc, local_ref, _ = git_rc(root, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0 or not local_ref.startswith("refs/heads/"):
        return None
    fmt = "%(upstream:short)%00%(upstream:remotename)%00%(upstream:remoteref)"
    rc, out, _ = git_rc(root, "for-each-ref", f"--format={fmt}", local_ref)
    fields = out.split("\0") if rc == 0 and out else []
    if (len(fields) != 3 or not all(fields)
            or not fields[2].startswith("refs/heads/")):
        return None
    return fields[0], fields[1], fields[2]


_VERIFY_FETCH_REF_PREFIX = "refs/waystone/verify-fetch-"
_VERIFY_FETCH_REF_RE = re.compile(
    rf"{re.escape(_VERIFY_FETCH_REF_PREFIX)}([1-9][0-9]*)-[0-9a-f]{{32}}")


def _sweep_stale_verify_fetch_refs(root: Path) -> str | None:
    rc, out, error = git_rc(
        root, "for-each-ref", "--format=%(refname)", f"{_VERIFY_FETCH_REF_PREFIX}*")
    if rc != 0:
        return f"cannot enumerate temporary fetch refs: {error or 'git for-each-ref failed'}"
    for ref in out.splitlines():
        match = _VERIFY_FETCH_REF_RE.fullmatch(ref)
        if match is None:
            continue
        # PID is only a same-host liveness locator; uncertainty preserves the ref.
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            pass
        except (OSError, OverflowError):
            continue
        else:
            continue
        cleanup_rc, _, cleanup_error = git_rc(root, "update-ref", "-d", ref)
        if cleanup_rc != 0:
            return f"cannot delete stale temporary fetch ref {ref}: {cleanup_error or 'error'}"
    return None


def fetch_upstream_head(root: Path) -> tuple[str | None, dict]:
    """Fetch the exact tracked branch into a private ref and return its live commit.

    A command-line refspec deliberately bypasses configured fetch mappings. The fetched SHA is
    read from a unique temporary ref, then the ref is deleted, so concurrent writes to the shared
    FETCH_HEAD pseudoref cannot change the publication evidence.
    """
    sweep_error = _sweep_stale_verify_fetch_refs(root)
    if sweep_error is not None:
        return (None, {
            "reason": f"temporary fetch ref sweep failed — remote unverifiable: {sweep_error}",
        })
    tracking = _upstream_tracking(root)
    if tracking is None:
        return (None, {"reason": "no upstream tracking branch"})
    upstream, remote_name, branch_ref = tracking
    info = {"upstream": upstream, "remote": remote_name, "branch_ref": branch_ref}
    if remote_name == ".":
        return (None, {
            **info,
            "reason": "upstream remote '.' is local repository state, not remote publication",
        })
    temporary_ref = f"{_VERIFY_FETCH_REF_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    rc, _, fetch_error = git_rc(
        root, "fetch", "--quiet", "--no-tags", "--force", remote_name,
        f"+{branch_ref}:{temporary_ref}")
    if rc != 0:
        cleanup_rc, _, cleanup_error = git_rc(root, "update-ref", "-d", temporary_ref)
        if cleanup_rc != 0:
            return (None, {
                **info,
                "reason": (
                    "fetch failed and temporary fetch ref cleanup failed — remote unverifiable: "
                    f"{cleanup_error or 'error'}"),
            })
        probe_rc, _, probe_error = git_rc(
            root, "ls-remote", "--exit-code", "--refs", remote_name, branch_ref)
        if probe_rc == 2:
            return (None, {
                **info,
                "reason": f"upstream branch {branch_ref} is absent on remote {remote_name}",
            })
        detail = fetch_error or probe_error or "error"
        return (None, {
            **info, "reason": f"fetch failed — remote unverifiable: {detail}",
        })
    sha_rc, remote_sha, sha_error = git_rc(
        root, "rev-parse", "--verify", f"{temporary_ref}^{{commit}}")
    cleanup_rc, _, cleanup_error = git_rc(root, "update-ref", "-d", temporary_ref)
    if cleanup_rc != 0:
        return (None, {
            **info,
            "reason": (
                "temporary fetch ref cleanup failed — remote unverifiable: "
                f"{cleanup_error or 'error'}"),
        })
    if sha_rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", remote_sha):
        return (None, {
            **info,
            "reason": (
                "fetch succeeded but temporary fetch ref is empty or invalid: "
                f"{sha_error or 'no commit'}"),
        })
    return remote_sha, info


def ancestry_status(root: Path, a: str, b: str) -> tuple[bool | None, str]:
    """Return True/False for containment, or None when Git cannot decide."""
    rc, _, error = git_rc(root, "merge-base", "--is-ancestor", a, b)
    if rc == 0:
        return True, ""
    if rc == 1:
        shallow_rc, shallow, shallow_error = git_rc(
            root, "rev-parse", "--is-shallow-repository")
        if shallow_rc != 0:
            detail = shallow_error or f"git rev-parse exited {shallow_rc}"
            return None, f"cannot determine whether repository is shallow: {detail}"
        if shallow == "true":
            return None, (
                "repository is shallow; git merge-base exit 1 cannot prove non-containment")
        if shallow == "false":
            return False, ""
        return None, (
            "git rev-parse --is-shallow-repository returned unexpected output: "
            f"{shallow!r}")
    return None, error or f"git merge-base exited {rc}"


def is_ancestor(root: Path, a: str, b: str) -> bool:
    """True only when `a` is proven contained in `b`; unverifiable fails closed to False."""
    status, _ = ancestry_status(root, a, b)
    return status is True


def head_pushed(root: Path, fetch: bool = True) -> tuple[bool, dict]:
    """Is the current HEAD contained in its tracked upstream (i.e. actually pushed)?
    Returns (pushed, info). Fail-closed: a fetch failure (network/auth/remote) returns
    (False, reason) rather than trusting a stale ref. Offline verification cannot prove
    publication and therefore also fails closed."""
    if not fetch:
        tracking = _upstream_tracking(root)
        if tracking is None:
            return (False, {"reason": "no upstream tracking branch"})
        return (False, {
            "reason": "live remote fetch required — offline state cannot prove publication",
            "upstream": tracking[0],
        })
    remote_sha, info = fetch_upstream_head(root)
    if remote_sha is None:
        return (False, info)
    head = git_full_sha(root, "HEAD")
    if head is None:
        return (False, {**info, "reason": "local HEAD is not a commit"})
    pushed, ancestry_error = ancestry_status(root, head, remote_sha)
    if pushed is None:
        return (False, {
            **info,
            "reason": (
                "cannot determine whether local HEAD is contained in the live upstream: "
                f"{ancestry_error}"),
            "head": head,
            "remote_sha": remote_sha,
        })
    rc, out, _ = git_rc(root, "rev-list", "--count", f"{head}..{remote_sha}")
    behind = int(out) if rc == 0 and out.isdigit() else None
    return (pushed, {**info, "head": head, "remote_sha": remote_sha, "behind": behind})


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
