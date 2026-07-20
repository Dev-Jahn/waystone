"""Transactional runtime state for one initialized Waystone project."""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Sequence

from waystone.core import WorkflowError, _ensure_project_self_ignore, content_hash
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    validate_sha256_digest,
)


SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5_000
_MAX_RUN_ID_ATTEMPTS = 32
_RUN_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SUPPORTED_LOCAL_FILESYSTEMS = frozenset({
    "apfs", "btrfs", "ext2", "ext3", "ext4", "f2fs", "hfs", "hfsplus",
    "ufs", "xfs", "zfs",
})
_REQUIRED_FILESYSTEM_PROPERTIES = (
    "process locking", "atomic replace", "sync/durability", "WAL journal mode",
)


class StoreError(WorkflowError):
    """Base class for typed runtime-store failures."""

    code = "runtime_store_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class UninitializedProjectError(StoreError):
    """The explicit root is not eligible for project-local state."""

    code = "uninitialized_project"

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        super().__init__(
            f"{project_root} has no regular .waystone.yml; refusing to create .waystone state")


class UnsupportedStateFilesystemError(StoreError):
    """The selected project-local database path cannot prove the SQLite contract."""

    code = "unsupported_state_filesystem"

    def __init__(
            self, path: Path, filesystem: str, reason: str, *, actual_journal_mode: str | None = None):
        self.path = Path(path)
        self.filesystem = filesystem
        self.reason = reason
        self.actual_journal_mode = actual_journal_mode
        self.required_properties = _REQUIRED_FILESYSTEM_PROPERTIES
        journal = (
            f", actual journal_mode={actual_journal_mode!r}" if actual_journal_mode is not None else "")
        super().__init__(
            f"{path} on {filesystem!r} cannot satisfy {', '.join(self.required_properties)} "
            f"({reason}{journal}); configure an explicit supported machine-local state path")


class InvalidStatePathError(StoreError):
    """A state parent or database path is not a real directory/regular file."""

    code = "invalid_state_path"

    def __init__(self, path: Path, detail: str):
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"{path}: {detail}")


class UnsupportedSchemaVersionError(StoreError):
    """This engine must not open a database created by a newer schema."""

    code = "unsupported_schema_version"

    def __init__(self, found: int, supported: int = SCHEMA_VERSION):
        self.found = found
        self.supported = supported
        super().__init__(f"database schema {found} is newer than supported schema {supported}")


class StateSchemaError(StoreError):
    """Schema metadata is absent, malformed, or incomplete."""

    code = "invalid_state_schema"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class StateDatabaseError(StoreError):
    """SQLite could not complete the requested store operation."""

    code = "state_database_error"

    def __init__(self, operation: str, detail: str):
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation}: {detail}")


class RecordNotFoundError(StoreError):
    """A requested current-state row or artifact reference does not exist."""

    code = "runtime_record_not_found"

    def __init__(self, record_kind: str, record_id: str):
        self.record_kind = record_kind
        self.record_id = record_id
        super().__init__(f"{record_kind} {record_id!r} does not exist")


class EntityVersionConflict(StoreError):
    """Expected-state CAS lost without committing any part of the transition."""

    code = "entity_version_conflict"

    def __init__(self, entity_kind: "EntityKind", entity_id: str, expected: int, actual: int):
        self.entity_kind = entity_kind
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{entity_kind.value} {entity_id!r} expected version {expected}, current version is {actual}")


class AppendOnlyConflict(StoreError):
    """An immutable identity is already present and cannot be overwritten or resumed."""

    code = "append_only_conflict"

    def __init__(self, record_kind: str, record_id: str):
        self.record_kind = record_kind
        self.record_id = record_id
        super().__init__(f"{record_kind} identity {record_id!r} already exists")


class RunnerInvocationConflict(StoreError):
    """A runner invocation reservation conflicts with durable action lineage."""

    code = "runner_invocation_conflict"

    def __init__(self, lineage_key: str, action_id: str, detail: str):
        self.lineage_key = lineage_key
        self.action_id = action_id
        self.detail = detail
        super().__init__(
            f"runner lineage {lineage_key!r} conflicts at action {action_id!r}: {detail}")


class GuardedEffectTransitionRequired(StoreError):
    """An effect action cannot advance through the generic transition surface."""

    code = "guarded_effect_transition_required"

    def __init__(self, action_id: str):
        self.action_id = action_id
        super().__init__(
            f"effect action {action_id!r} requires a LeaseManager guarded transition")


class InvalidEffectTransition(StoreError):
    """A guarded effect transition does not follow the five-stage lifecycle."""

    code = "invalid_effect_transition"

    def __init__(
            self, action_id: str, prev_state: str, next_state: str,
            reason: "TransitionReason"):
        self.action_id = action_id
        self.prev_state = prev_state
        self.next_state = next_state
        self.reason = reason
        super().__init__(
            f"effect action {action_id!r} cannot transition {prev_state!r} -> "
            f"{next_state!r} for reason {reason.value!r}")


class CorruptRuntimeRecordError(StoreError):
    """One logical run graph cannot prove its current-state integrity."""

    code = "corrupt_runtime_record"

    def __init__(
            self, run_id: str, entity_kind: "EntityKind | str", entity_id: str, detail: str):
        self.run_id = run_id
        self.entity_kind = (
            entity_kind.value if isinstance(entity_kind, EntityKind) else str(entity_kind))
        self.entity_id = entity_id
        self.detail = detail
        self.state = "unknown"
        super().__init__(
            f"run {run_id!r} has corrupt {self.entity_kind} {entity_id!r}: {detail}; state is unknown")


class RunIdCollisionError(StoreError):
    """Repeated CSPRNG identities collided beyond the bounded insertion attempt."""

    code = "run_id_collision_exhausted"

    def __init__(self, attempts: int):
        self.attempts = attempts
        super().__init__(f"could not allocate a unique UUIDv7 after {attempts} attempts")


class EntityKind(str, Enum):
    RUN = "run"
    JOB = "job"
    ATTEMPT = "attempt"
    ACTION = "action"


class TransitionReason(str, Enum):
    """Closed v1 vocabulary taken from accepted runtime lifecycle documents."""

    CREATED = "created"
    PLANNED = "planned"
    CLAIMED = "claimed"
    PROCESS_STARTED = "process-started"
    EFFECT_OBSERVED = "effect-observed"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel-requested"


@dataclass(frozen=True)
class FilesystemInfo:
    filesystem: str
    mount_point: Path
    options: frozenset[str] = frozenset()
    writable: bool | None = None


@dataclass(frozen=True)
class EntityRecord:
    entity_kind: EntityKind
    entity_id: str
    run_id: str
    state: str
    version: int
    parent_job_id: str | None = None
    parent_attempt_id: str | None = None


_TABLES = {
    EntityKind.RUN: ("runs", "run_id"),
    EntityKind.JOB: ("jobs", "job_id"),
    EntityKind.ATTEMPT: ("attempts", "attempt_id"),
    EntityKind.ACTION: ("actions", "action_id"),
}


def generate_run_id(unix_ms: int | None = None) -> str:
    """Generate an RFC 9562 UUIDv7 using a 48-bit Unix-ms field and 74 CSPRNG bits."""
    if unix_ms is None:
        unix_ms = time.time_ns() // 1_000_000
    if isinstance(unix_ms, bool) or not isinstance(unix_ms, int) or not 0 <= unix_ms < (1 << 48):
        raise ValueError("UUIDv7 unix_ms must be an integer in the unsigned 48-bit range")
    random_bits = secrets.randbits(74)
    random_a = random_bits >> 62
    random_b = random_bits & ((1 << 62) - 1)
    value = (
        (unix_ms << 80)
        | (0b0111 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return str(uuid.UUID(int=value))


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run_id generator returned a non-canonical RFC 9562 UUIDv7")
    parsed = uuid.UUID(run_id)
    if parsed.version != 7 or str(parsed) != run_id:
        raise ValueError("run_id generator returned a non-canonical RFC 9562 UUIDv7")
    return run_id


def _mount_unescape(value: str) -> str:
    return (value.replace(r"\040", " ").replace(r"\011", "\t")
            .replace(r"\012", "\n").replace(r"\134", "\\"))


def _contains_path(mount_point: Path, path: Path) -> bool:
    try:
        path.relative_to(mount_point)
        return True
    except ValueError:
        return False


def _linux_mounts() -> list[FilesystemInfo]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    mounts: list[FilesystemInfo] = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_mount_unescape(fields[4]))
            filesystem = fields[separator + 1].lower()
            options = frozenset({
                *fields[5].lower().split(","),
                *fields[separator + 3].lower().split(","),
            })
        except (ValueError, IndexError):
            continue
        mounts.append(FilesystemInfo(
            filesystem=filesystem, mount_point=mount_point, options=options))
    return mounts


def _darwin_mounts() -> list[FilesystemInfo]:
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    mounts: list[FilesystemInfo] = []
    pattern = re.compile(r"^.* on (.+) \(([^()]*)\)$")
    for line in result.stdout.splitlines():
        matched = pattern.match(line)
        if matched is None:
            continue
        fields = [field.strip().lower() for field in matched.group(2).split(",")]
        if not fields:
            continue
        mounts.append(FilesystemInfo(
            filesystem=fields[0],
            mount_point=Path(_mount_unescape(matched.group(1))),
            options=frozenset(fields[1:]),
        ))
    data_mounts = [entry for entry in mounts if "root data" in entry.options]
    data_mount = data_mounts[0] if len(data_mounts) == 1 else None
    if data_mount is not None:
        try:
            firmlinks = Path("/usr/share/firmlinks").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            firmlinks = []
        for line in firmlinks:
            fields = line.split("\t", 1)
            if len(fields) != 2 or not fields[0].startswith("/"):
                continue
            mounts.append(FilesystemInfo(
                filesystem=data_mount.filesystem,
                mount_point=Path(fields[0]),
                options=data_mount.options,
            ))
    return mounts


def _probe_state_filesystem(existing_parent: Path) -> FilesystemInfo:
    """Resolve the mount owning an existing parent; unknown remains unsupported."""
    try:
        resolved = Path(existing_parent).resolve(strict=True)
    except OSError:
        return FilesystemInfo(
            filesystem="unknown", mount_point=Path(existing_parent), writable=False)
    system = platform.system()
    if system == "Linux":
        mounts = _linux_mounts()
    elif system == "Darwin":
        mounts = _darwin_mounts()
    else:
        mounts = []
    try:
        target_device = resolved.stat().st_dev
    except OSError:
        return FilesystemInfo(filesystem="unknown", mount_point=resolved, writable=False)
    same_device: list[FilesystemInfo] = []
    for entry in mounts:
        try:
            if entry.mount_point.stat().st_dev == target_device:
                same_device.append(entry)
        except OSError:
            continue
    candidates = [entry for entry in same_device if _contains_path(entry.mount_point, resolved)]
    if not candidates:
        return FilesystemInfo(filesystem="unknown", mount_point=resolved, writable=False)
    depth = max(len(entry.mount_point.parts) for entry in candidates)
    most_specific = [entry for entry in candidates if len(entry.mount_point.parts) == depth]
    signatures = {(entry.filesystem, entry.mount_point, entry.options) for entry in most_specific}
    if len(signatures) != 1:
        return FilesystemInfo(filesystem="ambiguous", mount_point=resolved, writable=False)
    selected = most_specific[0]
    writable = os.access(resolved, os.W_OK)
    lowered_path = str(resolved).lower()
    if "/library/cloudstorage/" in lowered_path or "/library/mobile documents/" in lowered_path:
        return FilesystemInfo(
            filesystem="sync-overlay", mount_point=selected.mount_point,
            options=selected.options, writable=writable)
    return FilesystemInfo(
        filesystem=selected.filesystem, mount_point=selected.mount_point,
        options=selected.options, writable=writable)


def _connect(database_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        database_path,
        timeout=_BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
        check_same_thread=False,
    )


def _enable_wal(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    return "" if row is None else str(row[0]).lower()


def _negotiate_wal(
        connection: sqlite3.Connection, state_directory: Path,
        filesystem: FilesystemInfo) -> None:
    deadline = time.monotonic() + (_BUSY_TIMEOUT_MS / 1_000)
    while True:
        try:
            selected_mode = _enable_wal(connection)
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            raise UnsupportedStateFilesystemError(
                state_directory, filesystem.filesystem, "journal_mode_unavailable") from error
        except sqlite3.DatabaseError as error:
            raise UnsupportedStateFilesystemError(
                state_directory, filesystem.filesystem, "journal_mode_unavailable") from error
        if selected_mode != "wal":
            raise UnsupportedStateFilesystemError(
                state_directory, filesystem.filesystem, "journal_mode_mismatch",
                actual_journal_mode=selected_mode)
        try:
            actual_row = connection.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.DatabaseError as error:
            raise UnsupportedStateFilesystemError(
                state_directory, filesystem.filesystem, "journal_mode_unavailable") from error
        actual_mode = "" if actual_row is None else str(actual_row[0]).lower()
        if actual_mode != "wal":
            raise UnsupportedStateFilesystemError(
                state_directory, filesystem.filesystem, "journal_mode_mismatch",
                actual_journal_mode=actual_mode)
        return


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


@contextmanager
def _read_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN")
    try:
        yield
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _reason_sql() -> str:
    return ", ".join(f"'{reason.value}'" for reason in TransitionReason)


def _kind_sql(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{entry.value}'" for entry in enum_type)


def _migration_v1(connection: sqlite3.Connection) -> None:
    """Create schema v1. The caller owns the surrounding immediate transaction."""
    entity_kinds = _kind_sql(EntityKind)
    reasons = _reason_sql()
    artifact_kinds = _kind_sql(ArtifactReferenceKind)
    statements = (
        """CREATE TABLE schema_version (
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
               version INTEGER NOT NULL CHECK (version >= 1)
           )""",
        """CREATE TABLE runs (
               run_id TEXT NOT NULL PRIMARY KEY,
               state TEXT NOT NULL CHECK (length(state) > 0),
               version INTEGER NOT NULL CHECK (version >= 0),
               record_digest TEXT NOT NULL
           )""",
        """CREATE TABLE jobs (
               job_id TEXT NOT NULL PRIMARY KEY,
               run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
               state TEXT NOT NULL CHECK (length(state) > 0),
               version INTEGER NOT NULL CHECK (version >= 0),
               record_digest TEXT NOT NULL,
               UNIQUE (run_id, job_id)
           )""",
        """CREATE TABLE attempts (
               attempt_id TEXT NOT NULL PRIMARY KEY,
               run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
               job_id TEXT NOT NULL,
               state TEXT NOT NULL CHECK (length(state) > 0),
               version INTEGER NOT NULL CHECK (version >= 0),
               record_digest TEXT NOT NULL,
               UNIQUE (run_id, job_id, attempt_id),
               FOREIGN KEY (run_id, job_id)
                   REFERENCES jobs(run_id, job_id) ON DELETE RESTRICT
           )""",
        """CREATE TABLE actions (
               action_id TEXT NOT NULL PRIMARY KEY,
               run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
               job_id TEXT NOT NULL,
               attempt_id TEXT NOT NULL,
               state TEXT NOT NULL CHECK (length(state) > 0),
               version INTEGER NOT NULL CHECK (version >= 0),
               record_digest TEXT NOT NULL,
               FOREIGN KEY (run_id, job_id, attempt_id)
                   REFERENCES attempts(run_id, job_id, attempt_id) ON DELETE RESTRICT
           )""",
        f"""CREATE TABLE transitions (
               transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
               entity_kind TEXT NOT NULL CHECK (entity_kind IN ({entity_kinds})),
               entity_id TEXT NOT NULL CHECK (length(entity_id) > 0),
               prev_state TEXT,
               next_state TEXT NOT NULL CHECK (length(next_state) > 0),
               entity_version INTEGER NOT NULL CHECK (entity_version >= 0),
               reason TEXT NOT NULL CHECK (reason IN ({reasons})),
               evidence_digest TEXT,
               UNIQUE (entity_kind, entity_id, entity_version)
           )""",
        """CREATE TABLE leases (
               lease_id TEXT NOT NULL PRIMARY KEY,
               run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
               entity_kind TEXT NOT NULL,
               entity_id TEXT NOT NULL,
               entity_version INTEGER NOT NULL CHECK (entity_version >= 0),
               owner_token TEXT,
               fencing_epoch INTEGER NOT NULL DEFAULT 0 CHECK (fencing_epoch >= 0),
               expires_at TEXT,
               observed_at TEXT
           )""",
        """CREATE TABLE action_runtime (
               action_id TEXT NOT NULL PRIMARY KEY REFERENCES actions(action_id) ON DELETE RESTRICT,
               entity_version INTEGER NOT NULL CHECK (entity_version >= 0),
               phase TEXT,
               heartbeat_at TEXT,
               observed_at TEXT
           )""",
        f"""CREATE TABLE artifacts (
               reference_id TEXT NOT NULL PRIMARY KEY,
               run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
               transition_id INTEGER NOT NULL REFERENCES transitions(transition_id) ON DELETE RESTRICT,
               entity_kind TEXT NOT NULL CHECK (entity_kind IN ({entity_kinds})),
               entity_id TEXT NOT NULL CHECK (length(entity_id) > 0),
               entity_version INTEGER NOT NULL CHECK (entity_version >= 0),
               reference_kind TEXT NOT NULL CHECK (reference_kind IN ({artifact_kinds})),
               digest TEXT NOT NULL,
               size INTEGER NOT NULL CHECK (size >= 0)
           )""",
        """CREATE TABLE cache (
               cache_key TEXT NOT NULL PRIMARY KEY,
               digest TEXT NOT NULL,
               observed_at TEXT NOT NULL
           )""",
        """CREATE TRIGGER transitions_no_update
           BEFORE UPDATE ON transitions BEGIN
               SELECT RAISE(ABORT, 'transitions are append-only');
           END""",
        """CREATE TRIGGER transitions_no_delete
           BEFORE DELETE ON transitions BEGIN
               SELECT RAISE(ABORT, 'transitions are append-only');
           END""",
        """CREATE TRIGGER artifacts_no_update
           BEFORE UPDATE ON artifacts BEGIN
               SELECT RAISE(ABORT, 'artifact references are append-only');
           END""",
        """CREATE TRIGGER artifacts_no_delete
           BEFORE DELETE ON artifacts BEGIN
               SELECT RAISE(ABORT, 'artifact references are append-only');
           END""",
        """CREATE TRIGGER runs_identity_no_update
           BEFORE UPDATE OF run_id ON runs BEGIN
               SELECT RAISE(ABORT, 'run identity is immutable');
           END""",
        """CREATE TRIGGER runs_no_delete
           BEFORE DELETE ON runs BEGIN
               SELECT RAISE(ABORT, 'run identity is immutable');
           END""",
        """CREATE TRIGGER jobs_identity_no_update
           BEFORE UPDATE OF job_id, run_id ON jobs BEGIN
               SELECT RAISE(ABORT, 'job identity is immutable');
           END""",
        """CREATE TRIGGER jobs_no_delete
           BEFORE DELETE ON jobs BEGIN
               SELECT RAISE(ABORT, 'job identity is immutable');
           END""",
        """CREATE TRIGGER attempts_identity_no_update
           BEFORE UPDATE OF attempt_id, run_id, job_id ON attempts BEGIN
               SELECT RAISE(ABORT, 'attempt identity is append-only');
           END""",
        """CREATE TRIGGER attempts_no_delete
           BEFORE DELETE ON attempts BEGIN
               SELECT RAISE(ABORT, 'attempt identity is append-only');
           END""",
        """CREATE TRIGGER actions_identity_no_update
           BEFORE UPDATE OF action_id, run_id, job_id, attempt_id ON actions BEGIN
               SELECT RAISE(ABORT, 'action identity is immutable');
           END""",
        """CREATE TRIGGER actions_no_delete
           BEFORE DELETE ON actions BEGIN
               SELECT RAISE(ABORT, 'action identity is immutable');
           END""",
        "INSERT INTO schema_version(singleton, version) VALUES (1, 1)",
    )
    for statement in statements:
        connection.execute(statement)


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _migration_v1}


def _existing_schema_version(connection: sqlite3.Connection) -> int:
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        }
    except sqlite3.DatabaseError as error:
        raise StateSchemaError(f"cannot read state schema catalog: {error}") from error
    if "schema_version" not in tables:
        if tables:
            raise StateSchemaError(
                "state database has user tables but no schema_version authority")
        return 0
    try:
        rows = connection.execute(
            "SELECT singleton, version FROM schema_version").fetchall()
    except sqlite3.DatabaseError as error:
        raise StateSchemaError(f"cannot read schema_version: {error}") from error
    if (len(rows) != 1 or rows[0][0] != 1 or isinstance(rows[0][1], bool)
            or not isinstance(rows[0][1], int) or rows[0][1] < 1):
        raise StateSchemaError("schema_version must contain exactly singleton=1 and a positive version")
    return rows[0][1]


def _migrate(connection: sqlite3.Connection) -> int:
    while True:
        with _immediate_transaction(connection):
            version = _existing_schema_version(connection)
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(version)
            if version == SCHEMA_VERSION:
                return version
            target = version + 1
            migration = _MIGRATIONS.get(target)
            if migration is None:
                raise StateSchemaError(f"no migration registered for schema version {target}")
            migration(connection)
            migrated = _existing_schema_version(connection)
            if migrated != target:
                raise StateSchemaError(
                    f"migration {target} produced schema version {migrated} instead of {target}")


def _validate_schema(connection: sqlite3.Connection) -> None:
    required_tables = {
        "schema_version", "runs", "jobs", "attempts", "actions", "transitions",
        "leases", "action_runtime", "artifacts", "cache",
    }
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        raise StateSchemaError(f"schema v1 is missing tables: {', '.join(missing_tables)}")
    required_columns = {
        "schema_version": {"singleton", "version"},
        "runs": {"run_id", "state", "version", "record_digest"},
        "jobs": {"job_id", "run_id", "state", "version", "record_digest"},
        "attempts": {"attempt_id", "run_id", "job_id", "state", "version", "record_digest"},
        "actions": {
            "action_id", "run_id", "job_id", "attempt_id", "state", "version",
            "record_digest",
        },
        "transitions": {
            "transition_id", "run_id", "entity_kind", "entity_id", "prev_state",
            "next_state", "entity_version", "reason", "evidence_digest",
        },
        "leases": {
            "lease_id", "run_id", "entity_kind", "entity_id", "entity_version",
            "owner_token", "fencing_epoch", "expires_at", "observed_at",
        },
        "action_runtime": {
            "action_id", "entity_version", "phase", "heartbeat_at", "observed_at",
        },
        "artifacts": {
            "reference_id", "run_id", "transition_id", "entity_kind", "entity_id",
            "entity_version", "reference_kind", "digest", "size",
        },
        "cache": {"cache_key", "digest", "observed_at"},
    }
    for table, expected in required_columns.items():
        observed = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(expected - observed)
        if missing:
            raise StateSchemaError(
                f"schema v1 table {table} is missing columns: {', '.join(missing)}")
    required_triggers = {
        "transitions_no_update", "transitions_no_delete", "artifacts_no_update",
        "artifacts_no_delete", "runs_identity_no_update", "runs_no_delete",
        "jobs_identity_no_update", "jobs_no_delete", "attempts_identity_no_update",
        "attempts_no_delete", "actions_identity_no_update", "actions_no_delete",
    }
    triggers = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    missing_triggers = sorted(required_triggers - triggers)
    if missing_triggers:
        raise StateSchemaError(f"schema v1 is missing triggers: {', '.join(missing_triggers)}")


def _store_authorizer(action, table, column, database, source):
    del database, source
    if table in {"transitions", "artifacts"} and action in {
            sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        return sqlite3.SQLITE_DENY
    immutable_columns = {
        "runs": {"run_id"},
        "jobs": {"job_id", "run_id"},
        "attempts": {"attempt_id", "run_id", "job_id"},
        "actions": {"action_id", "run_id", "job_id", "attempt_id"},
    }
    if table in immutable_columns and action == sqlite3.SQLITE_DELETE:
        return sqlite3.SQLITE_DENY
    if (table in immutable_columns and action == sqlite3.SQLITE_UPDATE
            and column in immutable_columns[table]):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _record_digest(record: EntityRecord) -> str:
    payload = json.dumps({
        "entity_id": record.entity_id,
        "entity_kind": record.entity_kind.value,
        "parent_attempt_id": record.parent_attempt_id,
        "parent_job_id": record.parent_job_id,
        "run_id": record.run_id,
        "state": record.state,
        "version": record.version,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{content_hash(payload)}"


_RUN_STORE_CONSTRUCTION_TOKEN = object()


class RunStore:
    """One project-local SQLite authority with audited CAS state transitions."""

    def __init__(
            self, project_root: Path, database_path: Path, connection: sqlite3.Connection,
            filesystem: FilesystemInfo, schema_version: int, *, _token: object | None = None):
        if _token is not _RUN_STORE_CONSTRUCTION_TOKEN:
            raise TypeError("RunStore must be constructed with RunStore.open(project_root)")
        self.project_root = project_root
        self.database_path = database_path
        self.filesystem = filesystem
        self.schema_version = schema_version
        self._connection = connection
        self._connection_lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, project_root: Path) -> "RunStore":
        supplied_root = Path(project_root)
        marker = supplied_root / ".waystone.yml"
        try:
            marker_info = marker.lstat()
        except FileNotFoundError as error:
            raise UninitializedProjectError(supplied_root) from error
        except OSError as error:
            raise InvalidStatePathError(marker, f"cannot inspect project marker: {error}") from error
        if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
            raise UninitializedProjectError(supplied_root)
        try:
            resolved_root = supplied_root.resolve(strict=True)
        except OSError as error:
            raise UninitializedProjectError(supplied_root) from error

        state_directory = resolved_root / ".waystone"
        try:
            state_info = state_directory.lstat()
        except FileNotFoundError:
            probe_parent = resolved_root
            state_info = None
        except OSError as error:
            raise InvalidStatePathError(state_directory, f"cannot inspect state directory: {error}") from error
        else:
            if stat.S_ISLNK(state_info.st_mode) or not stat.S_ISDIR(state_info.st_mode):
                raise InvalidStatePathError(state_directory, "state directory must be a real directory")
            probe_parent = state_directory

        filesystem = _probe_state_filesystem(probe_parent)
        reported_read_only = bool({"ro", "read-only"} & filesystem.options)
        if (filesystem.filesystem not in _SUPPORTED_LOCAL_FILESYSTEMS
                or reported_read_only
                or filesystem.writable is not True):
            reason = (
                "filesystem_is_read_only"
                if (reported_read_only
                    or (filesystem.filesystem in _SUPPORTED_LOCAL_FILESYSTEMS
                        and filesystem.writable is False))
                else "filesystem_not_proven_supported")
            raise UnsupportedStateFilesystemError(
                state_directory, filesystem.filesystem, reason)

        if state_info is None:
            try:
                state_directory.mkdir(exist_ok=True)
            except OSError as error:
                raise InvalidStatePathError(
                    state_directory, f"cannot create state directory: {error}") from error
        try:
            state_info = state_directory.lstat()
        except OSError as error:
            raise InvalidStatePathError(
                state_directory, f"cannot verify state directory: {error}") from error
        if stat.S_ISLNK(state_info.st_mode) or not stat.S_ISDIR(state_info.st_mode):
            raise InvalidStatePathError(state_directory, "state directory must be a real directory")
        try:
            _ensure_project_self_ignore(state_directory)
        except (OSError, WorkflowError) as error:
            raise InvalidStatePathError(
                state_directory / ".gitignore",
                f"cannot establish project-state self-ignore: {error}") from error

        database_path = state_directory / "state.db"
        try:
            database_info = database_path.lstat()
        except FileNotFoundError:
            database_info = None
        except OSError as error:
            raise InvalidStatePathError(database_path, f"cannot inspect database: {error}") from error
        if database_info is not None and (
                stat.S_ISLNK(database_info.st_mode) or not stat.S_ISREG(database_info.st_mode)):
            raise InvalidStatePathError(database_path, "database must be a regular non-symlink file")

        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(database_path)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
            if foreign_keys is None or foreign_keys[0] != 1:
                raise StateDatabaseError("open", "SQLite foreign_keys could not be enabled")

            existing_version = _existing_schema_version(connection)
            if existing_version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(existing_version)

            _negotiate_wal(connection, state_directory, filesystem)
            version = _migrate(connection)
            _validate_schema(connection)
            connection.set_authorizer(_store_authorizer)
            return cls(
                resolved_root, database_path, connection, filesystem, version,
                _token=_RUN_STORE_CONSTRUCTION_TOKEN)
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            raise StateDatabaseError("open runtime store", str(error)) from error
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._connection_lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _transaction_fault_point(self, stage: str) -> None:
        """Private deterministic crash-injection seam; production performs no action."""
        del stage

    def _insert_entity(self, record: EntityRecord) -> EntityRecord:
        table, identity_column = _TABLES[record.entity_kind]
        digest = _record_digest(record)
        try:
            with self._connection_lock, _immediate_transaction(self._connection):
                if self._connection.execute(
                        f"SELECT 1 FROM {table} WHERE {identity_column} = ?",
                        (record.entity_id,)).fetchone() is not None:
                    raise AppendOnlyConflict(record.entity_kind.value, record.entity_id)

                if record.entity_kind is EntityKind.RUN:
                    self._connection.execute(
                        "INSERT INTO runs(run_id, state, version, record_digest) VALUES (?, ?, ?, ?)",
                        (record.run_id, record.state, record.version, digest),
                    )
                elif record.entity_kind is EntityKind.JOB:
                    parent = self._load_record(EntityKind.RUN, record.run_id)
                    if parent.run_id != record.run_id:
                        raise CorruptRuntimeRecordError(
                            record.run_id, EntityKind.RUN, record.run_id, "run identity mismatch")
                    self._connection.execute(
                        "INSERT INTO jobs(job_id, run_id, state, version, record_digest) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (record.entity_id, record.run_id, record.state, record.version, digest),
                    )
                elif record.entity_kind is EntityKind.ATTEMPT:
                    parent = self._load_record(EntityKind.JOB, record.parent_job_id or "")
                    if parent.run_id != record.run_id:
                        raise ValueError("attempt parent job belongs to a different run")
                    self._connection.execute(
                        "INSERT INTO attempts(attempt_id, run_id, job_id, state, version, record_digest) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (record.entity_id, record.run_id, record.parent_job_id, record.state,
                         record.version, digest),
                    )
                else:
                    parent = self._load_record(EntityKind.ATTEMPT, record.parent_attempt_id or "")
                    if (parent.run_id != record.run_id
                            or parent.parent_job_id != record.parent_job_id):
                        raise ValueError("action parent attempt belongs to a different run or job")
                    self._connection.execute(
                        "INSERT INTO actions(action_id, run_id, job_id, attempt_id, state, version, "
                        "record_digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (record.entity_id, record.run_id, record.parent_job_id,
                         record.parent_attempt_id, record.state, record.version, digest),
                    )
                self._connection.execute(
                    "INSERT INTO transitions(run_id, entity_kind, entity_id, prev_state, next_state, "
                    "entity_version, reason, evidence_digest) VALUES (?, ?, ?, NULL, ?, 0, ?, NULL)",
                    (record.run_id, record.entity_kind.value, record.entity_id, record.state,
                     TransitionReason.CREATED.value),
                )
            return record
        except (StoreError, ValueError):
            raise
        except sqlite3.IntegrityError as error:
            raise StateDatabaseError("create entity", str(error)) from error
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError("create entity", str(error)) from error

    def create_run(self, initial_state: str = "created") -> EntityRecord:
        state = _nonempty(initial_state, "initial_state")
        for _ in range(_MAX_RUN_ID_ATTEMPTS):
            run_id = _validate_run_id(generate_run_id())
            record = EntityRecord(
                entity_kind=EntityKind.RUN,
                entity_id=run_id,
                run_id=run_id,
                state=state,
                version=0,
            )
            try:
                return self._insert_entity(record)
            except AppendOnlyConflict:
                continue
        raise RunIdCollisionError(_MAX_RUN_ID_ATTEMPTS)

    def create_job(
            self, run_id: str, job_id: str, initial_state: str = "queued") -> EntityRecord:
        record = EntityRecord(
            entity_kind=EntityKind.JOB,
            entity_id=_nonempty(job_id, "job_id"),
            run_id=_nonempty(run_id, "run_id"),
            state=_nonempty(initial_state, "initial_state"),
            version=0,
        )
        return self._insert_entity(record)

    def create_attempt(
            self, run_id: str, job_id: str, attempt_id: str,
            initial_state: str = "queued") -> EntityRecord:
        record = EntityRecord(
            entity_kind=EntityKind.ATTEMPT,
            entity_id=_nonempty(attempt_id, "attempt_id"),
            run_id=_nonempty(run_id, "run_id"),
            state=_nonempty(initial_state, "initial_state"),
            version=0,
            parent_job_id=_nonempty(job_id, "job_id"),
        )
        return self._insert_entity(record)

    def create_action(
            self, run_id: str, job_id: str, attempt_id: str, action_id: str,
            initial_state: str = "planned") -> EntityRecord:
        record = EntityRecord(
            entity_kind=EntityKind.ACTION,
            entity_id=_nonempty(action_id, "action_id"),
            run_id=_nonempty(run_id, "run_id"),
            state=_nonempty(initial_state, "initial_state"),
            version=0,
            parent_job_id=_nonempty(job_id, "job_id"),
            parent_attempt_id=_nonempty(attempt_id, "attempt_id"),
        )
        return self._insert_entity(record)

    def _create_planned_effect_action(
            self, run_id: str, job_id: str, attempt_id: str, action_id: str, *,
            evidence_digest: str,
            artifact_references: Sequence[ArtifactReference],
            runner_lineage_key: str | None = None,
            runner_retry_of: str | None = None) -> EntityRecord:
        """Create one effect action and its evidence-bound planned state atomically."""
        run_identity = _nonempty(run_id, "run_id")
        job_identity = _nonempty(job_id, "job_id")
        attempt_identity = _nonempty(attempt_id, "attempt_id")
        action_identity = _nonempty(action_id, "action_id")
        digest = validate_sha256_digest(evidence_digest)
        references = tuple(artifact_references)
        seen_reference_ids: set[str] = set()
        for reference in references:
            if not isinstance(reference, ArtifactReference):
                raise TypeError("artifact_references must contain ArtifactReference values")
            if reference.reference_id in seen_reference_ids:
                raise AppendOnlyConflict("artifact reference", reference.reference_id)
            seen_reference_ids.add(reference.reference_id)

        lineage_prefix: str | None = None
        if runner_lineage_key is not None:
            lineage_key = validate_sha256_digest(runner_lineage_key)
            lineage_prefix = f"runner-invocation:{lineage_key}:"
            lineage_reference_id = f"{lineage_prefix}{action_identity}"
            if lineage_reference_id not in seen_reference_ids:
                raise ValueError(
                    "runner lineage reservation must be included in artifact_references")
            if runner_retry_of is not None:
                runner_retry_of = _nonempty(runner_retry_of, "runner_retry_of")
        elif runner_retry_of is not None:
            raise ValueError("runner_retry_of requires runner_lineage_key")

        created = EntityRecord(
            entity_kind=EntityKind.ACTION,
            entity_id=action_identity,
            run_id=run_identity,
            state="created",
            version=0,
            parent_job_id=job_identity,
            parent_attempt_id=attempt_identity,
        )
        planned = replace(created, state="planned", version=1)
        try:
            with self._connection_lock, _immediate_transaction(self._connection):
                if self._connection.execute(
                        "SELECT 1 FROM actions WHERE action_id = ?",
                        (action_identity,)).fetchone() is not None:
                    raise AppendOnlyConflict(EntityKind.ACTION.value, action_identity)

                parent_run = self._load_record(EntityKind.RUN, run_identity)
                parent_job = self._load_record(EntityKind.JOB, job_identity)
                parent_attempt = self._load_record(EntityKind.ATTEMPT, attempt_identity)
                if parent_run.run_id != run_identity:
                    raise CorruptRuntimeRecordError(
                        run_identity, EntityKind.RUN, run_identity, "run identity mismatch")
                if parent_job.run_id != run_identity:
                    raise ValueError("action parent job belongs to a different run")
                if (parent_attempt.run_id != run_identity
                        or parent_attempt.parent_job_id != job_identity):
                    raise ValueError("action parent attempt belongs to a different run or job")

                if lineage_prefix is not None:
                    lineage_rows = self._connection.execute(
                        "SELECT a.reference_id, a.entity_id, x.state FROM artifacts a "
                        "JOIN actions x ON x.action_id = a.entity_id "
                        "WHERE substr(a.reference_id, 1, length(?)) = ? "
                        "ORDER BY a.transition_id",
                        (lineage_prefix, lineage_prefix),
                    ).fetchall()
                    prior: list[EntityRecord] = []
                    for row in lineage_rows:
                        if row["reference_id"] != f"{lineage_prefix}{row['entity_id']}":
                            raise CorruptRuntimeRecordError(
                                run_identity, EntityKind.ACTION, row["entity_id"],
                                "runner lineage reference identity is malformed")
                        record = self._load_record(EntityKind.ACTION, row["entity_id"])
                        if (record.run_id != run_identity
                                or record.parent_job_id != job_identity
                                or record.state != row["state"]):
                            raise CorruptRuntimeRecordError(
                                record.run_id, EntityKind.ACTION, record.entity_id,
                                "runner lineage scope or current state is incoherent")
                        prior.append(record)
                    if prior and runner_retry_of is None:
                        raise RunnerInvocationConflict(
                            runner_lineage_key, prior[-1].entity_id,
                            "a repeated invocation requires explicit retry lineage")
                    prior_ids = {record.entity_id for record in prior}
                    if runner_retry_of is not None and runner_retry_of not in prior_ids:
                        raise RunnerInvocationConflict(
                            runner_lineage_key, runner_retry_of,
                            "retry lineage does not name this runner invocation")
                    nonterminal = [record for record in prior if record.state != "completed"]
                    if nonterminal:
                        raise RunnerInvocationConflict(
                            runner_lineage_key, nonterminal[-1].entity_id,
                            "the runner invocation has a nonterminal or uncertain action")

                self._connection.execute(
                    "INSERT INTO actions(action_id, run_id, job_id, attempt_id, state, version, "
                    "record_digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (action_identity, run_identity, job_identity, attempt_identity,
                     created.state, created.version, _record_digest(created)),
                )
                self._connection.execute(
                    "INSERT INTO transitions(run_id, entity_kind, entity_id, prev_state, next_state, "
                    "entity_version, reason, evidence_digest) "
                    "VALUES (?, ?, ?, NULL, ?, 0, ?, NULL)",
                    (run_identity, EntityKind.ACTION.value, action_identity, created.state,
                     TransitionReason.CREATED.value),
                )
                cursor = self._connection.execute(
                    "INSERT INTO transitions(run_id, entity_kind, entity_id, prev_state, next_state, "
                    "entity_version, reason, evidence_digest) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (run_identity, EntityKind.ACTION.value, action_identity, created.state,
                     planned.state, TransitionReason.PLANNED.value, digest),
                )
                transition_id = cursor.lastrowid
                self._transaction_fault_point("after_transition_insert")

                result = self._connection.execute(
                    "UPDATE actions SET state = ?, version = ?, record_digest = ? "
                    "WHERE action_id = ? AND version = 0",
                    (planned.state, planned.version, _record_digest(planned), action_identity),
                )
                if result.rowcount != 1:
                    actual_row = self._connection.execute(
                        "SELECT version FROM actions WHERE action_id = ?", (action_identity,)
                    ).fetchone()
                    actual = -1 if actual_row is None else int(actual_row[0])
                    raise EntityVersionConflict(
                        EntityKind.ACTION, action_identity, created.version, actual)

                for reference in references:
                    if self._connection.execute(
                            "SELECT 1 FROM artifacts WHERE reference_id = ?",
                            (reference.reference_id,)).fetchone() is not None:
                        raise AppendOnlyConflict("artifact reference", reference.reference_id)
                    self._connection.execute(
                        "INSERT INTO artifacts(reference_id, run_id, transition_id, entity_kind, "
                        "entity_id, entity_version, reference_kind, digest, size) "
                        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (reference.reference_id, run_identity, transition_id,
                         EntityKind.ACTION.value, action_identity, reference.kind.value,
                         reference.digest, reference.size),
                    )
                self._transaction_fault_point("after_artifact_references")
            return planned
        except (StoreError, ValueError, TypeError):
            raise
        except sqlite3.IntegrityError as error:
            raise StateDatabaseError("create planned effect action", str(error)) from error
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError("create planned effect action", str(error)) from error

    def _row_to_record(
            self, entity_kind: EntityKind, row: sqlite3.Row, requested_id: str) -> EntityRecord:
        raw_run_id = row["run_id"]
        error_run_id = (
            raw_run_id if isinstance(raw_run_id, str) and raw_run_id.strip()
            else requested_id if entity_kind is EntityKind.RUN else "<unknown>")
        try:
            run_id = _nonempty(raw_run_id, "stored run_id")
            state = _nonempty(row["state"], "stored state")
            version = row["version"]
            if (isinstance(version, bool) or not isinstance(version, int) or version < 0):
                raise ValueError("stored version must be a non-negative integer")
            if entity_kind is EntityKind.RUN:
                entity_id = _nonempty(row["run_id"], "stored run_id")
                return EntityRecord(entity_kind, entity_id, run_id, state, version)
            if entity_kind is EntityKind.JOB:
                entity_id = _nonempty(row["job_id"], "stored job_id")
                return EntityRecord(entity_kind, entity_id, run_id, state, version)
            if entity_kind is EntityKind.ATTEMPT:
                entity_id = _nonempty(row["attempt_id"], "stored attempt_id")
                parent_job_id = _nonempty(row["job_id"], "stored job_id")
                return EntityRecord(
                    entity_kind, entity_id, run_id, state, version,
                    parent_job_id=parent_job_id)
            entity_id = _nonempty(row["action_id"], "stored action_id")
            parent_job_id = _nonempty(row["job_id"], "stored job_id")
            parent_attempt_id = _nonempty(row["attempt_id"], "stored attempt_id")
            return EntityRecord(
                entity_kind, entity_id, run_id, state, version,
                parent_job_id=parent_job_id, parent_attempt_id=parent_attempt_id)
        except (TypeError, ValueError) as error:
            raise CorruptRuntimeRecordError(
                error_run_id, entity_kind, requested_id, f"invalid current-state row: {error}") from error

    def _load_record(self, entity_kind: EntityKind, entity_id: str) -> EntityRecord:
        table, identity_column = _TABLES[entity_kind]
        row = self._connection.execute(
            f"SELECT * FROM {table} WHERE {identity_column} = ?", (entity_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError(entity_kind.value, entity_id)
        record = self._row_to_record(entity_kind, row, entity_id)
        if record.entity_id != entity_id:
            raise CorruptRuntimeRecordError(
                record.run_id, entity_kind, entity_id, "queried identity does not match stored identity")
        try:
            expected_digest = validate_sha256_digest(row["record_digest"])
        except (TypeError, ValueError) as error:
            raise CorruptRuntimeRecordError(
                record.run_id, entity_kind, entity_id, "record digest is not canonical") from error
        if _record_digest(record) != expected_digest:
            raise CorruptRuntimeRecordError(
                record.run_id, entity_kind, entity_id, "current-state digest mismatch")
        self._validate_transition_chain(record)
        return record

    def _validate_transition_chain(self, record: EntityRecord) -> None:
        rows = self._connection.execute(
            "SELECT run_id, prev_state, next_state, entity_version, reason, evidence_digest "
            "FROM transitions WHERE entity_kind = ? AND entity_id = ? ORDER BY entity_version",
            (record.entity_kind.value, record.entity_id),
        ).fetchall()
        if len(rows) != record.version + 1:
            raise CorruptRuntimeRecordError(
                record.run_id, record.entity_kind, record.entity_id,
                "transition versions are missing, duplicated, or ahead of current state")
        previous_state: str | None = None
        for version, row in enumerate(rows):
            try:
                next_state = _nonempty(row["next_state"], "transition next_state")
                reason = TransitionReason(row["reason"])
                if row["evidence_digest"] is not None:
                    validate_sha256_digest(row["evidence_digest"])
            except (TypeError, ValueError) as error:
                raise CorruptRuntimeRecordError(
                    record.run_id, record.entity_kind, record.entity_id,
                    f"transition {version} is malformed: {error}") from error
            expected_previous = None if version == 0 else previous_state
            if (row["run_id"] != record.run_id or row["entity_version"] != version
                    or row["prev_state"] != expected_previous):
                raise CorruptRuntimeRecordError(
                    record.run_id, record.entity_kind, record.entity_id,
                    f"transition {version} breaks run binding or state continuity")
            if version == 0 and reason is not TransitionReason.CREATED:
                raise CorruptRuntimeRecordError(
                    record.run_id, record.entity_kind, record.entity_id,
                    "initial transition reason is not created")
            previous_state = next_state
        if previous_state != record.state:
            raise CorruptRuntimeRecordError(
                record.run_id, record.entity_kind, record.entity_id,
                "latest transition does not match current state")

    def get_entity(self, entity_kind: EntityKind, entity_id: str) -> EntityRecord:
        kind = EntityKind(entity_kind)
        identity = _nonempty(entity_id, "entity_id")
        try:
            with self._connection_lock, _read_transaction(self._connection):
                return self._load_record(kind, identity)
        except StoreError:
            raise
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError("read entity", str(error)) from error

    def _membership_owner(self, entity_kind: EntityKind, entity_id: str) -> str | None:
        """Attribute one child without letting either side of a broken binding poison a peer run."""
        table, identity_column = _TABLES[entity_kind]
        row = self._connection.execute(
            f"SELECT * FROM {table} WHERE {identity_column} = ?", (entity_id,)).fetchone()
        if row is not None:
            try:
                record = self._row_to_record(entity_kind, row, entity_id)
                expected_digest = validate_sha256_digest(row["record_digest"])
            except (CorruptRuntimeRecordError, TypeError, ValueError):
                pass
            else:
                if record.entity_id == entity_id and _record_digest(record) == expected_digest:
                    return record.run_id
        creation = self._connection.execute(
            "SELECT run_id FROM transitions "
            "WHERE entity_kind = ? AND entity_id = ? AND entity_version = 0",
            (entity_kind.value, entity_id),
        ).fetchone()
        if creation is not None:
            try:
                return _nonempty(creation["run_id"], "creation transition run_id")
            except (TypeError, ValueError):
                pass
        return None

    def get_run(self, run_id: str) -> EntityRecord:
        identity = _nonempty(run_id, "run_id")
        try:
            with self._connection_lock, _read_transaction(self._connection):
                try:
                    run = self._load_record(EntityKind.RUN, identity)
                except RecordNotFoundError as error:
                    if self._connection.execute(
                            "SELECT 1 FROM transitions WHERE run_id = ? LIMIT 1",
                            (identity,)).fetchone() is not None:
                        raise CorruptRuntimeRecordError(
                            identity, EntityKind.RUN, identity,
                            "current run row is missing while audit transitions remain") from error
                    raise
                audited_rows = self._connection.execute(
                    "SELECT DISTINCT entity_kind, entity_id FROM transitions WHERE run_id = ?",
                    (identity,),
                ).fetchall()
                audited_children: set[tuple[EntityKind, str]] = set()
                for row in audited_rows:
                    try:
                        child_kind = EntityKind(row["entity_kind"])
                        child_id = _nonempty(row["entity_id"], "transition entity_id")
                    except (TypeError, ValueError) as error:
                        raise CorruptRuntimeRecordError(
                            identity, "transition", identity,
                            f"run graph has malformed entity identity: {error}") from error
                    if child_kind is EntityKind.RUN:
                        if child_id != identity:
                            owner = self._membership_owner(child_kind, child_id)
                            if owner is not None and owner != identity:
                                continue
                            raise CorruptRuntimeRecordError(
                                identity, child_kind, child_id,
                                "run audit contains a different run identity")
                    else:
                        owner = self._membership_owner(child_kind, child_id)
                        if owner is None or owner == identity:
                            audited_children.add((child_kind, child_id))

                current_children: set[tuple[EntityKind, str]] = set()
                for kind, table, column in (
                        (EntityKind.JOB, "jobs", "job_id"),
                        (EntityKind.ATTEMPT, "attempts", "attempt_id"),
                        (EntityKind.ACTION, "actions", "action_id")):
                    child_ids = self._connection.execute(
                        f"SELECT {column} FROM {table} WHERE run_id = ?",
                        (identity,),
                    ).fetchall()
                    for row in child_ids:
                        try:
                            child_id = _nonempty(row[0], f"stored {column}")
                        except (TypeError, ValueError) as error:
                            raise CorruptRuntimeRecordError(
                                identity, kind, "<unknown>",
                                f"run graph has malformed current identity: {error}") from error
                        owner = self._membership_owner(kind, child_id)
                        if owner is not None and owner != identity:
                            # A damaged current or audit binding must not make its forged target
                            # run corrupt. A valid current-row digest wins; otherwise creation does.
                            continue
                        current_children.add((kind, child_id))
                if audited_children != current_children:
                    differing = sorted(
                        audited_children ^ current_children,
                        key=lambda item: (item[0].value, item[1]),
                    )[0]
                    raise CorruptRuntimeRecordError(
                        identity, differing[0], differing[1],
                        "current run membership differs from append-only audit membership")
                for kind, child_id in sorted(
                        audited_children, key=lambda item: (item[0].value, item[1])):
                    try:
                        child = self._load_record(kind, child_id)
                    except (RecordNotFoundError, CorruptRuntimeRecordError) as error:
                        detail = getattr(error, "detail", str(error))
                        raise CorruptRuntimeRecordError(
                            identity, kind, child_id, f"run child is corrupt: {detail}") from error
                    if child.run_id != identity:
                        raise CorruptRuntimeRecordError(
                            identity, kind, child_id, "child is attributed to a different run")
                return run
        except StoreError:
            raise
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError("read run", str(error)) from error

    def record_transition(
            self, entity_kind: EntityKind, entity_id: str, *, expected_version: int,
            next_state: str, reason: TransitionReason, evidence_digest: str | None = None,
            artifact_references: Sequence[ArtifactReference] = ()) -> EntityRecord:
        """CAS one current row and its audit/reference facts in one immediate transaction."""
        kind = EntityKind(entity_kind)
        identity = _nonempty(entity_id, "entity_id")
        state = _nonempty(next_state, "next_state")
        try:
            typed_reason = TransitionReason(reason)
        except (TypeError, ValueError) as error:
            raise ValueError("reason must be a TransitionReason supported by schema v1") from error
        if (isinstance(expected_version, bool) or not isinstance(expected_version, int)
                or expected_version < 0):
            raise ValueError("expected_version must be a non-negative integer")
        if evidence_digest is not None:
            evidence_digest = validate_sha256_digest(evidence_digest)
        references = tuple(artifact_references)
        seen_reference_ids: set[str] = set()
        for reference in references:
            if not isinstance(reference, ArtifactReference):
                raise TypeError("artifact_references must contain ArtifactReference values")
            if reference.reference_id in seen_reference_ids:
                raise AppendOnlyConflict("artifact reference", reference.reference_id)
            seen_reference_ids.add(reference.reference_id)

        try:
            with self._connection_lock, _immediate_transaction(self._connection):
                current = self._load_record(kind, identity)
                if current.version != expected_version:
                    raise EntityVersionConflict(
                        kind, identity, expected_version, current.version)
                if (kind is EntityKind.ACTION
                        and self._connection.execute(
                            "SELECT 1 FROM artifacts WHERE reference_id = ? "
                            "AND entity_kind = ? AND entity_id = ?",
                            (f"effect-plan:{identity}", EntityKind.ACTION.value, identity),
                        ).fetchone() is not None):
                    raise GuardedEffectTransitionRequired(identity)
                next_version = expected_version + 1
                cursor = self._connection.execute(
                    "INSERT INTO transitions(run_id, entity_kind, entity_id, prev_state, next_state, "
                    "entity_version, reason, evidence_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (current.run_id, kind.value, identity, current.state, state, next_version,
                     typed_reason.value, evidence_digest),
                )
                transition_id = cursor.lastrowid
                self._transaction_fault_point("after_transition_insert")

                updated = replace(current, state=state, version=next_version)
                table, identity_column = _TABLES[kind]
                result = self._connection.execute(
                    f"UPDATE {table} SET state = ?, version = ?, record_digest = ? "
                    f"WHERE {identity_column} = ? AND version = ?",
                    (updated.state, updated.version, _record_digest(updated), identity,
                     expected_version),
                )
                if result.rowcount != 1:
                    actual_row = self._connection.execute(
                        f"SELECT version FROM {table} WHERE {identity_column} = ?", (identity,)
                    ).fetchone()
                    actual = -1 if actual_row is None else int(actual_row[0])
                    raise EntityVersionConflict(kind, identity, expected_version, actual)

                for reference in references:
                    if self._connection.execute(
                            "SELECT 1 FROM artifacts WHERE reference_id = ?",
                            (reference.reference_id,)).fetchone() is not None:
                        raise AppendOnlyConflict("artifact reference", reference.reference_id)
                    self._connection.execute(
                        "INSERT INTO artifacts(reference_id, run_id, transition_id, entity_kind, "
                        "entity_id, entity_version, reference_kind, digest, size) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (reference.reference_id, current.run_id, transition_id, kind.value, identity,
                         next_version, reference.kind.value, reference.digest, reference.size),
                    )
                self._transaction_fault_point("after_artifact_references")
            return updated
        except (StoreError, ValueError, TypeError):
            raise
        except sqlite3.IntegrityError as error:
            raise StateDatabaseError("record transition", str(error)) from error
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError("record transition", str(error)) from error

    def _record_guarded_action_transition(
            self, action_id: str, *, expected_version: int, owner_token: str,
            fencing_epoch: int, next_state: str, reason: TransitionReason,
            evidence_digest: str | None = None,
            artifact_references: Sequence[ArtifactReference] = ()) -> EntityRecord:
        """Advance one action and its exact lease tuple inside an existing guard transaction."""
        identity = _nonempty(action_id, "action_id")
        owner = _nonempty(owner_token, "owner_token")
        state = _nonempty(next_state, "next_state")
        if (isinstance(expected_version, bool) or not isinstance(expected_version, int)
                or not 0 <= expected_version < (1 << 63) - 1):
            raise ValueError(
                "expected_version must be a non-negative incrementable signed 64-bit integer")
        if (isinstance(fencing_epoch, bool) or not isinstance(fencing_epoch, int)
                or not 1 <= fencing_epoch <= (1 << 63) - 1):
            raise ValueError("fencing_epoch must be a positive signed 64-bit integer")
        try:
            typed_reason = TransitionReason(reason)
        except (TypeError, ValueError) as error:
            raise ValueError("reason must be a TransitionReason supported by schema v1") from error
        if evidence_digest is not None:
            evidence_digest = validate_sha256_digest(evidence_digest)
        references = tuple(artifact_references)
        seen_reference_ids: set[str] = set()
        for reference in references:
            if not isinstance(reference, ArtifactReference):
                raise TypeError("artifact_references must contain ArtifactReference values")
            if reference.reference_id in seen_reference_ids:
                raise AppendOnlyConflict("artifact reference", reference.reference_id)
            seen_reference_ids.add(reference.reference_id)

        operation = "record guarded action transition"
        try:
            if not self._connection.in_transaction:
                raise StateDatabaseError(
                    operation, "requires an active LeaseManager guard transaction")

            current = self._load_record(EntityKind.ACTION, identity)
            if current.version != expected_version:
                raise EntityVersionConflict(
                    EntityKind.ACTION, identity, expected_version, current.version)
            is_effect_action = self._connection.execute(
                "SELECT 1 FROM artifacts WHERE reference_id = ? "
                "AND entity_kind = ? AND entity_id = ?",
                (f"effect-plan:{identity}", EntityKind.ACTION.value, identity),
            ).fetchone() is not None
            effect_edges = {
                ("planned", "claimed", TransitionReason.CLAIMED),
                ("claimed", "effect", TransitionReason.PROCESS_STARTED),
                ("effect", "observed", TransitionReason.EFFECT_OBSERVED),
                ("observed", "completed", TransitionReason.COMPLETED),
            }
            if (is_effect_action
                    and (current.state, state, typed_reason) not in effect_edges):
                raise InvalidEffectTransition(
                    identity, current.state, state, typed_reason)
            lease_rows = self._connection.execute(
                "SELECT lease_id, run_id, entity_kind, entity_id, entity_version, "
                "owner_token, fencing_epoch FROM leases "
                "WHERE lease_id = ? OR (entity_kind = ? AND entity_id = ?)",
                (identity, EntityKind.ACTION.value, identity),
            ).fetchall()
            if len(lease_rows) != 1:
                raise StateDatabaseError(
                    operation, "current action lease row is missing or ambiguous")
            lease = lease_rows[0]
            if (lease["lease_id"] != identity
                    or lease["run_id"] != current.run_id
                    or lease["entity_kind"] != EntityKind.ACTION.value
                    or lease["entity_id"] != identity
                    or lease["entity_version"] != expected_version
                    or lease["owner_token"] != owner
                    or lease["fencing_epoch"] != fencing_epoch):
                raise StateDatabaseError(
                    operation, "current action lease tuple does not exactly match")

            next_version = expected_version + 1
            cursor = self._connection.execute(
                "INSERT INTO transitions(run_id, entity_kind, entity_id, prev_state, next_state, "
                "entity_version, reason, evidence_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (current.run_id, EntityKind.ACTION.value, identity, current.state, state,
                 next_version, typed_reason.value, evidence_digest),
            )
            transition_id = cursor.lastrowid
            self._transaction_fault_point("after_transition_insert")

            updated = replace(current, state=state, version=next_version)
            result = self._connection.execute(
                "UPDATE actions SET state = ?, version = ?, record_digest = ? "
                "WHERE action_id = ? AND version = ?",
                (updated.state, updated.version, _record_digest(updated), identity,
                 expected_version),
            )
            if result.rowcount != 1:
                actual_row = self._connection.execute(
                    "SELECT version FROM actions WHERE action_id = ?", (identity,)
                ).fetchone()
                actual = -1 if actual_row is None else int(actual_row[0])
                raise EntityVersionConflict(
                    EntityKind.ACTION, identity, expected_version, actual)

            for reference in references:
                if self._connection.execute(
                        "SELECT 1 FROM artifacts WHERE reference_id = ?",
                        (reference.reference_id,)).fetchone() is not None:
                    raise AppendOnlyConflict("artifact reference", reference.reference_id)
                self._connection.execute(
                    "INSERT INTO artifacts(reference_id, run_id, transition_id, entity_kind, "
                    "entity_id, entity_version, reference_kind, digest, size) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (reference.reference_id, current.run_id, transition_id,
                     EntityKind.ACTION.value, identity, next_version, reference.kind.value,
                     reference.digest, reference.size),
                )
            self._transaction_fault_point("after_artifact_references")

            lease_result = self._connection.execute(
                "UPDATE leases SET entity_version = ? "
                "WHERE lease_id = ? AND run_id = ? AND entity_kind = ? AND entity_id = ? "
                "AND owner_token = ? AND fencing_epoch = ? AND entity_version = ?",
                (next_version, identity, current.run_id, EntityKind.ACTION.value, identity,
                 owner, fencing_epoch, expected_version),
            )
            if lease_result.rowcount != 1:
                raise StateDatabaseError(
                    operation, "lease entity-version CAS did not select one current row")

            runtime = self._connection.execute(
                "SELECT entity_version FROM action_runtime WHERE action_id = ?", (identity,)
            ).fetchone()
            if runtime is not None:
                runtime_version = runtime["entity_version"]
                if (isinstance(runtime_version, bool) or not isinstance(runtime_version, int)
                        or runtime_version != expected_version):
                    raise StateDatabaseError(
                        operation, "action runtime entity version is incoherent")
                runtime_result = self._connection.execute(
                    "UPDATE action_runtime SET entity_version = ? "
                    "WHERE action_id = ? AND entity_version = ?",
                    (next_version, identity, expected_version),
                )
                if runtime_result.rowcount != 1:
                    raise StateDatabaseError(
                        operation,
                        "action runtime entity-version CAS did not select one current row")
            return updated
        except (StoreError, ValueError, TypeError):
            raise
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError(operation, str(error)) from error

    def get_artifact_reference(self, reference_id: str) -> ArtifactReference:
        identity = _nonempty(reference_id, "reference_id")
        try:
            with self._connection_lock, _read_transaction(self._connection):
                row = self._connection.execute(
                    "SELECT a.reference_id, a.reference_kind, a.digest, a.size, a.run_id, "
                    "a.entity_kind, a.entity_id, a.entity_version, "
                    "t.transition_id AS joined_transition_id, "
                    "t.run_id AS transition_run_id, t.entity_kind AS transition_entity_kind, "
                    "t.entity_id AS transition_entity_id, t.entity_version AS transition_version "
                    "FROM artifacts a LEFT JOIN transitions t ON t.transition_id = a.transition_id "
                    "WHERE a.reference_id = ?",
                    (identity,),
                ).fetchone()
                if row is None:
                    raise RecordNotFoundError("artifact reference", identity)
                if row["joined_transition_id"] is None:
                    raise CorruptRuntimeRecordError(
                        row["run_id"] if isinstance(row["run_id"], str) else "<unknown>",
                        row["entity_kind"], row["entity_id"],
                        f"artifact reference {identity!r} points to a missing transition")
                if (row["run_id"] != row["transition_run_id"]
                        or row["entity_kind"] != row["transition_entity_kind"]
                        or row["entity_id"] != row["transition_entity_id"]
                        or row["entity_version"] != row["transition_version"]):
                    raise CorruptRuntimeRecordError(
                        row["run_id"], row["entity_kind"], row["entity_id"],
                        f"artifact reference {identity!r} is detached from its transition")
                try:
                    return ArtifactReference(
                        reference_id=row["reference_id"],
                        kind=ArtifactReferenceKind(row["reference_kind"]),
                        digest=row["digest"],
                        size=row["size"],
                    )
                except (TypeError, ValueError) as error:
                    raise CorruptRuntimeRecordError(
                        row["run_id"], row["entity_kind"], row["entity_id"],
                        f"artifact reference {identity!r} is malformed") from error
        except StoreError:
            raise
        except sqlite3.DatabaseError as error:
            raise StateDatabaseError("read artifact reference", str(error)) from error


def open_store(project_root: Path) -> RunStore:
    """Open the project-local runtime authority without consulting ambient state roots."""
    return RunStore.open(project_root)
