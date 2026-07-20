"""Foundational errors, filesystem, locking, serialization, and validation helpers."""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml


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


def canonical_payload_hash(payload: object) -> str:
    """SHA-256 over the unique compact JSON encoding used to bind consent to a candidate."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def slugify(text: str, max_len: int = 40) -> str:
    """Filename slug for generated SSOT sections. Keeps Hangul (Korean headings stay
    readable); task IDs are NOT slugified with this — their grammar stays ASCII."""
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "section"
