"""Detached runner supervision with fenced process identity evidence."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Sequence

from waystone.core import WorkflowError
from waystone.runs.artifacts import ArtifactStore, validate_sha256_digest
from waystone.runs.effects import (
    EffectPlan,
    RunnerCompletionMarker,
    RunnerLaunchIntent,
    publish_runner_completion,
)
from waystone.runs.lease import LeaseManager, LeasePrincipal
from waystone.runs.store import EntityKind, RunStore


_LAUNCH_SCHEMA = "waystone-supervisor-launch-1"
_RUNTIME_SCHEMA = "waystone-supervisor-runtime-1"
_HEARTBEAT_SCHEMA = "waystone-supervisor-heartbeat-1"
_WAIT_SCHEMA = "waystone-supervisor-wait-1"
_MARKER_SCHEMA = "waystone-runner-completion-1"
_MARKER_SCHEMA_V2 = "waystone-runner-completion-2"


class SupervisorError(WorkflowError):
    """Base class for fail-loud supervisor failures."""

    code = "supervisor_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class SupervisorLaunchRefused(SupervisorError):
    """A launch cannot establish one unique, fenced supervisor incarnation."""

    code = "supervisor_launch_refused"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class SupervisorAlreadyStarted(SupervisorLaunchRefused):
    """An immutable launch reservation already fences the action."""

    code = "supervisor_already_started"

    def __init__(self, action_id: str, prior_incarnation: str):
        self.prior_incarnation = prior_incarnation
        super().__init__(
            action_id, f"a prior supervisor incarnation is {prior_incarnation}")


class SupervisorStateError(SupervisorError):
    """Engine-owned supervisor evidence is missing, malformed, or unsafe."""

    code = "supervisor_state_error"

    def __init__(self, path: Path, detail: str):
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"{path}: {detail}")


class ProcessIdentityUnavailable(SupervisorError):
    """The host cannot establish one required process identity component."""

    code = "process_identity_unavailable"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class CompletionMarkerRefused(SupervisorError):
    """A marker is not attributable to the engine-owned supervisor incarnation."""

    code = "completion_marker_refused"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class LivenessState(str, Enum):
    ALIVE = "alive"
    EXITED = "exited"
    UNKNOWN = "unknown"


class HeartbeatFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_finite(value: float, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{label} must be a positive finite number")
    return float(value)


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("supervisor evidence must be canonical-JSON serializable") from error


def _canonical_text(payload: object) -> str:
    return _canonical_bytes(payload).decode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _action_filename(action_id: str, suffix: str) -> str:
    identity = _nonempty(action_id, "action_id")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest() + suffix


def _real_directory(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise SupervisorStateError(path, f"cannot create directory: {error}") from error
    if not _real_directory(path):
        raise SupervisorStateError(path, "directory must be real and non-symlink")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise SupervisorStateError(path, f"cannot durably sync directory: {error}") from error


def _publish_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    _ensure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        content = _canonical_bytes(payload)
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    except FileExistsError:
        raise
    except OSError as error:
        raise SupervisorStateError(path, f"exclusive publication failed: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _replace_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _ensure_directory(path.parent)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "wb", dir=path.parent, prefix=".supervisor-", suffix=".tmp",
                delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_canonical_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except SupervisorError:
        raise
    except OSError as error:
        raise SupervisorStateError(path, f"atomic publication failed: {error}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_object(path: Path, *, schema: str, fields: set[str]) -> dict[str, object]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SupervisorStateError(path, "evidence path is not a regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except SupervisorError:
        raise
    except FileNotFoundError as error:
        raise SupervisorStateError(path, "evidence is missing") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupervisorStateError(path, f"evidence is unreadable: {error}") from error
    if not isinstance(payload, dict):
        raise SupervisorStateError(path, "evidence is not an object")
    if payload.get("schema") != schema or set(payload) != fields:
        raise SupervisorStateError(path, "evidence schema or fields are not exact")
    return payload


@dataclass(frozen=True)
class ProcessIdentity:
    """PID-reuse-safe identity for one supervisor or runner process."""

    host_boot_identity: str
    pid: int
    process_start_token: str
    action_id: str
    supervisor_owner_token: str
    fencing_epoch: int
    resolved_executable: str | None = None
    invocation_digest: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.host_boot_identity, "host_boot_identity")
        _positive_int(self.pid, "pid")
        _nonempty(self.process_start_token, "process_start_token")
        _nonempty(self.action_id, "action_id")
        _nonempty(self.supervisor_owner_token, "supervisor_owner_token")
        _positive_int(self.fencing_epoch, "fencing_epoch")
        if self.resolved_executable is not None:
            _nonempty(self.resolved_executable, "resolved_executable")
        if self.invocation_digest is not None:
            validate_sha256_digest(self.invocation_digest)
        if self.resolved_executable is None and self.invocation_digest is None:
            raise ValueError(
                "process identity requires resolved_executable or invocation_digest")

    def to_payload(self) -> dict[str, object]:
        return {
            "host_boot_identity": self.host_boot_identity,
            "pid": self.pid,
            "process_start_token": self.process_start_token,
            "action_id": self.action_id,
            "supervisor_owner_token": self.supervisor_owner_token,
            "fencing_epoch": self.fencing_epoch,
            "resolved_executable": self.resolved_executable,
            "invocation_digest": self.invocation_digest,
        }

    @property
    def canonical(self) -> str:
        """Encode the richer identity inside effects.py's string adapter field."""
        return _canonical_text(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> "ProcessIdentity":
        fields = {
            "host_boot_identity", "pid", "process_start_token", "action_id",
            "supervisor_owner_token", "fencing_epoch", "resolved_executable",
            "invocation_digest",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ValueError("process identity fields are not exact")
        return cls(
            host_boot_identity=payload["host_boot_identity"],
            pid=payload["pid"],
            process_start_token=payload["process_start_token"],
            action_id=payload["action_id"],
            supervisor_owner_token=payload["supervisor_owner_token"],
            fencing_epoch=payload["fencing_epoch"],
            resolved_executable=payload["resolved_executable"],
            invocation_digest=payload["invocation_digest"],
        )


@dataclass(frozen=True)
class SupervisorHeartbeat:
    """Mutable engine telemetry in one host monotonic clock domain."""

    action_id: str
    fencing_epoch: int
    host_boot_identity: str
    monotonic_observed_at: float
    wall_observed_at: str
    process_identity: ProcessIdentity

    def __post_init__(self) -> None:
        _nonempty(self.action_id, "heartbeat.action_id")
        _positive_int(self.fencing_epoch, "heartbeat.fencing_epoch")
        _nonempty(self.host_boot_identity, "heartbeat.host_boot_identity")
        if (isinstance(self.monotonic_observed_at, bool)
                or not isinstance(self.monotonic_observed_at, (int, float))
                or not math.isfinite(self.monotonic_observed_at)
                or self.monotonic_observed_at < 0):
            raise ValueError("heartbeat monotonic time must be finite and non-negative")
        _nonempty(self.wall_observed_at, "heartbeat.wall_observed_at")
        if self.action_id != self.process_identity.action_id:
            raise ValueError("heartbeat action does not match process identity")
        if self.fencing_epoch != self.process_identity.fencing_epoch:
            raise ValueError("heartbeat fence does not match process identity")
        if self.host_boot_identity != self.process_identity.host_boot_identity:
            raise ValueError("heartbeat boot identity does not match process identity")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": _HEARTBEAT_SCHEMA,
            "action_id": self.action_id,
            "fencing_epoch": self.fencing_epoch,
            "host_boot_identity": self.host_boot_identity,
            "monotonic_observed_at": self.monotonic_observed_at,
            "wall_observed_at": self.wall_observed_at,
            "process_identity": self.process_identity.to_payload(),
        }


@dataclass(frozen=True)
class LivenessObservation:
    """Tri-state liveness plus separate exact-identity absence evidence."""

    state: LivenessState
    reason: str
    exact_identity_absent: bool = False
    heartbeat: HeartbeatFreshness = HeartbeatFreshness.UNKNOWN

    @property
    def destructive_resolution_allowed(self) -> bool:
        """Only a positive same-identity exit can authorize later cleanup."""
        return self.state is LivenessState.EXITED


@dataclass(frozen=True)
class RunnerCandidateContext:
    """Exact candidate facts required by one read-only stage invocation."""

    candidate_oid: str
    root_fingerprint: str
    run_spec_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.candidate_oid, str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.candidate_oid) is None):
            raise ValueError("candidate_oid must be one full lowercase Git OID")
        validate_sha256_digest(self.root_fingerprint)
        validate_sha256_digest(self.run_spec_digest)

    def to_payload(self) -> dict[str, str]:
        return {
            "candidate_oid": self.candidate_oid,
            "root_fingerprint": self.root_fingerprint,
            "run_spec_digest": self.run_spec_digest,
        }


@dataclass(frozen=True)
class RunnerInvocation:
    """One exact local command selected by an already-frozen invocation digest."""

    argv: tuple[str, ...]
    cwd: Path
    candidate_context: RunnerCandidateContext | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.argv, tuple) or not self.argv
                or any(not isinstance(value, str) for value in self.argv)):
            raise ValueError("argv must be a non-empty tuple of strings")
        _nonempty(self.argv[0], "argv[0]")
        object.__setattr__(self, "cwd", Path(self.cwd))
        if (self.candidate_context is not None
                and not isinstance(self.candidate_context, RunnerCandidateContext)):
            raise TypeError(
                "candidate_context must be a RunnerCandidateContext or None")


@dataclass(frozen=True)
class DetachedSupervisorHandle:
    action_id: str
    pid: int
    launch_path: Path


def host_boot_identity() -> str:
    """Return a fail-loud boot identity; never substitute hostname or wall time."""
    if sys.platform.startswith("linux"):
        path = Path("/proc/sys/kernel/random/boot_id")
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise ProcessIdentityUnavailable(
                f"cannot read Linux boot identity: {error}") from error
        return "linux:" + _nonempty(value, "Linux boot identity")
    if sys.platform == "darwin":
        executable = Path("/usr/sbin/sysctl")
        if not executable.is_file():
            raise ProcessIdentityUnavailable("/usr/sbin/sysctl is unavailable")
        try:
            result = subprocess.run(
                [str(executable), "-n", "kern.boottime"],
                capture_output=True, text=True, check=False,
            )
        except OSError as error:
            raise ProcessIdentityUnavailable(
                f"cannot query Darwin boot identity: {error}") from error
        value = result.stdout.strip()
        if result.returncode != 0 or not value:
            raise ProcessIdentityUnavailable(
                "Darwin kern.boottime observation failed")
        match = re.search(
            r"\bsec\s*=\s*([0-9]+)\s*,\s*usec\s*=\s*([0-9]+)\b", value)
        if match is None:
            raise ProcessIdentityUnavailable(
                "Darwin kern.boottime observation is malformed")
        return f"darwin-boot-time:{match.group(1)}:{match.group(2)}"
    raise ProcessIdentityUnavailable(
        f"process identity is unsupported on platform {sys.platform!r}")


def _process_start_observation(pid: int) -> tuple[str, bool]:
    """Return one start token and whether the same incarnation is a zombie."""
    identity = _positive_int(pid, "pid")
    if sys.platform.startswith("linux"):
        path = Path("/proc") / str(identity) / "stat"
        try:
            value = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ProcessLookupError(identity) from error
        except (OSError, UnicodeError) as error:
            raise ProcessIdentityUnavailable(
                f"cannot read process {identity} start token: {error}") from error
        closing = value.rfind(")")
        fields = value[closing + 2:].split() if closing >= 0 else []
        if len(fields) <= 19 or not fields[19].isdigit():
            raise ProcessIdentityUnavailable(
                f"process {identity} stat record is malformed")
        return "linux-start-ticks:" + fields[19], fields[0] == "Z"
    if sys.platform == "darwin":
        try:
            os.kill(identity, 0)
        except ProcessLookupError:
            raise
        except PermissionError as error:
            raise ProcessIdentityUnavailable(
                f"process {identity} exists but cannot be inspected") from error

        class ProcBsdInfo(ctypes.Structure):
            _fields_ = (
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            )
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            function = library.proc_pidinfo
            function.argtypes = (
                ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_int,
            )
            function.restype = ctypes.c_int
            info = ProcBsdInfo()
            observed = function(
                identity, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        except OSError as error:
            raise ProcessIdentityUnavailable(
                f"cannot inspect process {identity}: {error}") from error
        if observed != ctypes.sizeof(info):
            try:
                os.kill(identity, 0)
            except ProcessLookupError:
                raise
            raise ProcessIdentityUnavailable(
                f"process {identity} start time observation failed")
        if info.pbi_pid != identity or info.pbi_start_tvsec < 1:
            raise ProcessIdentityUnavailable(
                f"process {identity} start identity is malformed")
        token = (
            f"darwin-start-time:{info.pbi_start_tvsec}:"
            f"{info.pbi_start_tvusec}"
        )
        return token, info.pbi_status == 5
    raise ProcessIdentityUnavailable(
        f"process start tokens are unsupported on platform {sys.platform!r}")


def process_start_token(pid: int) -> str:
    """Read a verifiable per-incarnation start token or report positive absence."""
    return _process_start_observation(pid)[0]


def capture_process_identity(
        pid: int, *, action_id: str, owner_token: str, fencing_epoch: int,
        resolved_executable: str | None = None,
        invocation_digest: str | None = None,
        boot_identity: str | None = None) -> ProcessIdentity:
    """Capture every minimum identity axis for a currently observable process."""
    boot = host_boot_identity() if boot_identity is None else boot_identity
    return ProcessIdentity(
        host_boot_identity=boot,
        pid=pid,
        process_start_token=process_start_token(pid),
        action_id=action_id,
        supervisor_owner_token=owner_token,
        fencing_epoch=fencing_epoch,
        resolved_executable=resolved_executable,
        invocation_digest=invocation_digest,
    )


def observe_process_identity(
        identity: ProcessIdentity, *,
        current_boot_identity: str | None = None,
        start_token_reader: Callable[[int], str] = process_start_token,
        heartbeat: HeartbeatFreshness = HeartbeatFreshness.UNKNOWN,
) -> LivenessObservation:
    """Observe exact identity without treating PID reuse as alive or exited."""
    if not isinstance(identity, ProcessIdentity):
        raise TypeError("identity must be a ProcessIdentity")
    current_boot = (
        host_boot_identity() if current_boot_identity is None
        else _nonempty(current_boot_identity, "current_boot_identity")
    )
    if current_boot != identity.host_boot_identity:
        return LivenessObservation(
            LivenessState.UNKNOWN, "identity-mismatch", True, heartbeat)
    try:
        if start_token_reader is process_start_token:
            observed_token, observed_exited = _process_start_observation(identity.pid)
        else:
            observed_token = start_token_reader(identity.pid)
            observed_exited = False
    except ProcessLookupError:
        return LivenessObservation(
            LivenessState.EXITED, "process-absent", True, heartbeat)
    except Exception as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return LivenessObservation(
            LivenessState.UNKNOWN,
            f"process-observation-unavailable:{type(error).__name__}",
            False, heartbeat)
    if observed_token != identity.process_start_token:
        return LivenessObservation(
            LivenessState.UNKNOWN, "identity-mismatch", True, heartbeat)
    if observed_exited:
        return LivenessObservation(
            LivenessState.EXITED, "process-zombie", True, heartbeat)
    return LivenessObservation(
        LivenessState.ALIVE, "process-identity-matched", False, heartbeat)


def heartbeat_freshness(
        heartbeat: SupervisorHeartbeat, *, stale_after: float,
        current_boot_identity: str | None = None,
        monotonic_now: float | None = None) -> HeartbeatFreshness:
    """Compare heartbeat age only within one host boot monotonic domain."""
    if not isinstance(heartbeat, SupervisorHeartbeat):
        raise TypeError("heartbeat must be a SupervisorHeartbeat")
    maximum_age = _positive_finite(stale_after, "stale_after")
    current_boot = (
        host_boot_identity() if current_boot_identity is None
        else _nonempty(current_boot_identity, "current_boot_identity")
    )
    if current_boot != heartbeat.host_boot_identity:
        return HeartbeatFreshness.UNKNOWN
    now = time.monotonic() if monotonic_now is None else monotonic_now
    if (isinstance(now, bool) or not isinstance(now, (int, float))
            or not math.isfinite(now) or now < heartbeat.monotonic_observed_at):
        return HeartbeatFreshness.UNKNOWN
    return (
        HeartbeatFreshness.FRESH
        if now - heartbeat.monotonic_observed_at <= maximum_age
        else HeartbeatFreshness.STALE
    )


def _resolved_executable(argv0: str, cwd: Path) -> Path:
    value = _nonempty(argv0, "argv[0]")
    candidate: Path | None
    if os.sep in value or (os.altsep is not None and os.altsep in value):
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else cwd / supplied
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as error:
            raise SupervisorLaunchRefused(value, f"executable is unavailable: {error}") from error
    else:
        found = shutil.which(value)
        if found is None:
            raise SupervisorLaunchRefused(value, "executable is unavailable on PATH")
        candidate = Path(found).resolve()
    try:
        info = candidate.stat()
    except OSError as error:
        raise SupervisorLaunchRefused(value, f"cannot inspect executable: {error}") from error
    if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
        raise SupervisorLaunchRefused(value, "resolved executable is not an executable file")
    return candidate


def _candidate_context_mismatch(
        cwd: Path, context: RunnerCandidateContext | Mapping[str, object] | None,
) -> str | None:
    if context is None:
        return None
    payload = context.to_payload() if isinstance(context, RunnerCandidateContext) else context
    try:
        candidate_oid = payload["candidate_oid"]
        root_fingerprint = validate_sha256_digest(payload["root_fingerprint"])
        validate_sha256_digest(payload["run_spec_digest"])
        observed_fingerprint = fingerprint_candidate_root(cwd, candidate_oid)
    except (KeyError, TypeError, ValueError, WorkflowError, OSError) as error:
        return f"candidate root cannot be observed: {error}"
    if observed_fingerprint != root_fingerprint:
        return "candidate root fingerprint differs from the frozen materialization"
    return None


def fingerprint_candidate_root(cwd: Path, candidate_oid: str) -> str:
    """Bind one detached candidate checkout while ignoring its reserved result file."""
    from waystone.adapters.git import git_full_sha, git_read_bytes

    root = Path(cwd).resolve(strict=True)
    if git_full_sha(root) != candidate_oid:
        raise ValueError("candidate root HEAD differs from the frozen candidate OID")
    status = git_read_bytes(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = tuple(item for item in status.split(b"\0") if item)
    if any(item != b"?? WAYSTONE_RESULT.yaml" for item in records):
        raise ValueError("candidate root contains non-control-file changes")
    tree_oid = git_read_bytes(
        root, "rev-parse", "--verify", f"{candidate_oid}^{{tree}}").strip()
    return "sha256:" + hashlib.sha256(_canonical_bytes({
        "candidate_oid": candidate_oid,
        "tree_oid": tree_oid.decode("ascii"),
    })).hexdigest()


def _settle_worker_after_failure(
        process: subprocess.Popen, identity: ProcessIdentity | None) -> None:
    """Never abandon a spawned child; signal only a still-matching identity."""
    if process.poll() is None and identity is not None:
        observation = observe_process_identity(identity)
        if observation.state is LivenessState.ALIVE:
            process.terminate()
            try:
                process.communicate(timeout=5)
                return
            except subprocess.TimeoutExpired:
                observation = observe_process_identity(identity)
                if observation.state is LivenessState.ALIVE:
                    process.kill()
    # If identity is unavailable or mismatched, waiting is intentionally safer
    # than sending a PID-only signal to a potentially different process.
    process.communicate()


class Supervisor:
    """Effects adapter and read-side authority for detached runner supervision."""

    def __init__(
            self, store: RunStore, leases: LeaseManager, *,
            invocations: Mapping[str, RunnerInvocation],
            heartbeat_interval: float = 1.0,
            lease_ttl: float = 5.0):
        if not isinstance(store, RunStore):
            raise TypeError("store must be a RunStore")
        if not isinstance(leases, LeaseManager):
            raise TypeError("leases must be a LeaseManager")
        if leases._store is not store:  # noqa: SLF001 - package composition contract
            raise ValueError("leases and supervisor must share one RunStore")
        interval = _positive_finite(heartbeat_interval, "heartbeat_interval")
        ttl = _positive_finite(lease_ttl, "lease_ttl")
        if ttl <= interval:
            raise ValueError("lease_ttl must be greater than heartbeat_interval")
        normalized: dict[str, RunnerInvocation] = {}
        for digest, invocation in invocations.items():
            canonical = validate_sha256_digest(digest)
            if not isinstance(invocation, RunnerInvocation):
                raise TypeError("invocation values must be RunnerInvocation instances")
            normalized[canonical] = invocation
        self._store = store
        self._leases = leases
        self._invocations = normalized
        self._heartbeat_interval = interval
        self._lease_ttl = ttl
        self.project_root = store.project_root
        self.directory = self.project_root / ".waystone" / "supervisors"

    def bind_invocation(self, digest: str, invocation: RunnerInvocation) -> None:
        """Bind one stage-owned invocation before its runner effect is launched."""
        canonical = validate_sha256_digest(digest)
        if not isinstance(invocation, RunnerInvocation):
            raise TypeError("invocation must be a RunnerInvocation")
        existing = self._invocations.get(canonical)
        if existing is not None and existing != invocation:
            raise SupervisorLaunchRefused(
                "invocation-binding", "digest is already bound to a different invocation")
        self._invocations[canonical] = invocation

    def _launch_path(self, action_id: str) -> Path:
        return self.directory / _action_filename(action_id, ".launch.json")

    def _runtime_path(self, action_id: str) -> Path:
        return self.directory / _action_filename(action_id, ".runtime.json")

    def _heartbeat_path(self, action_id: str) -> Path:
        return self.directory / _action_filename(action_id, ".heartbeat.json")

    def _wait_path(self, action_id: str) -> Path:
        return self.directory / _action_filename(action_id, ".wait.json")

    def _marker_path(self, action_id: str) -> Path:
        return (
            self.project_root / ".waystone" / "runner-completions"
            / _action_filename(action_id, ".json")
        )

    def _principal_for_intent(self, intent: RunnerLaunchIntent) -> LeasePrincipal:
        action = self._store.get_entity(EntityKind.ACTION, intent.action_id)
        if (action.run_id != intent.run_id
                or action.parent_job_id != intent.job_id
                or action.state != "effect"):
            raise SupervisorLaunchRefused(
                intent.action_id,
                "current action run/job/state does not match the runner launch intent")
        reference_id = f"effect-intent:{intent.action_id}"
        try:
            reference = self._store.get_artifact_reference(reference_id)
            wai = json.loads(
                ArtifactStore(self.project_root).read_reference(reference).decode("utf-8"))
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SupervisorLaunchRefused(
                intent.action_id, f"durable runner intent is unavailable: {error}") from error
        expected_wai = {
            "schema": "waystone-effect-intent-1",
            "run_id": intent.run_id,
            "job_id": intent.job_id,
            "action_id": intent.action_id,
            "kind": "runner-execution",
            "fencing_epoch": intent.fencing_epoch,
            "launch_token": intent.launch_token,
        }
        if (not isinstance(wai, dict)
                or any(wai.get(key) != value for key, value in expected_wai.items())):
            raise SupervisorLaunchRefused(
                intent.action_id, "runner launch intent does not match its durable WAI")
        with self._store._connection_lock:  # noqa: SLF001 - package adapter boundary
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT t.entity_version, t.next_state, t.reason, t.evidence_digest, "
                "a.digest FROM artifacts a JOIN transitions t "
                "ON t.transition_id = a.transition_id "
                "WHERE a.reference_id = ? AND a.entity_kind = ? AND a.entity_id = ?",
                (reference_id, EntityKind.ACTION.value, intent.action_id),
            ).fetchone()
        if (row is None or row["next_state"] != "effect"
                or row["reason"] != "process-started"
                or row["evidence_digest"] != reference.digest
                or row["digest"] != reference.digest):
            raise SupervisorLaunchRefused(
                intent.action_id, "runner WAI is not bound to its effect transition")
        return LeasePrincipal(
            run_id=intent.run_id,
            action_id=intent.action_id,
            owner_token=intent.owner_token,
            fencing_epoch=intent.fencing_epoch,
            entity_version=row["entity_version"],
            monotonic_deadline=0.0,
        )

    def _prior_incarnation(self, action_id: str) -> str:
        runtime_path = self._runtime_path(action_id)
        if not runtime_path.exists():
            return "reserved-without-observable-identity"
        try:
            runtime = _read_runtime(runtime_path)
            identity = ProcessIdentity.from_payload(runtime["supervisor_identity"])
            observation = observe_process_identity(identity)
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return f"identity-unknown:{type(error).__name__}"
        return observation.state.value + ":" + observation.reason

    def _supervisor_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parents[2])
        existing = environment.get("PYTHONPATH", "")
        entries = [entry for entry in existing.split(os.pathsep) if entry]
        if package_root not in entries:
            entries.insert(0, package_root)
        environment["PYTHONPATH"] = os.pathsep.join(entries)
        return environment

    def launch(self, intent: RunnerLaunchIntent) -> DetachedSupervisorHandle:
        """Fence one detached supervisor at the exact callback-internal spawn edge."""
        if not isinstance(intent, RunnerLaunchIntent):
            raise TypeError("intent must be a RunnerLaunchIntent")
        for value, label in (
                (intent.run_id, "intent.run_id"),
                (intent.job_id, "intent.job_id"),
                (intent.action_id, "intent.action_id"),
                (intent.owner_token, "intent.owner_token"),
                (intent.launch_token, "intent.launch_token")):
            _nonempty(value, label)
        _positive_int(intent.fencing_epoch, "intent.fencing_epoch")
        digest = validate_sha256_digest(intent.invocation_digest)
        invocation = self._invocations.get(digest)
        if invocation is None:
            raise SupervisorLaunchRefused(
                intent.action_id, "no exact invocation is bound to the frozen digest")
        expected_marker = self._marker_path(intent.action_id)
        if Path(intent.completion_marker_path) != expected_marker:
            raise SupervisorLaunchRefused(
                intent.action_id, "completion marker path is outside the engine-owned path")
        try:
            cwd = invocation.cwd.resolve(strict=True)
        except OSError as error:
            raise SupervisorLaunchRefused(
                intent.action_id, f"runner cwd is unavailable: {error}") from error
        if not cwd.is_dir():
            raise SupervisorLaunchRefused(intent.action_id, "runner cwd is not a directory")
        mismatch = _candidate_context_mismatch(cwd, invocation.candidate_context)
        if mismatch is not None:
            raise SupervisorLaunchRefused(intent.action_id, mismatch)
        executable = _resolved_executable(invocation.argv[0], cwd)
        principal = self._principal_for_intent(intent)
        worker_result_binding = self._worker_result_binding(intent.action_id, intent.run_id)
        if worker_result_binding is not None and invocation.candidate_context is not None:
            from waystone.runs.worker_result import capture_result_snapshot

            worker_result_binding = {
                **worker_result_binding,
                "base_snapshot_digest": capture_result_snapshot(cwd).digest,
            }
        launch_path = self._launch_path(intent.action_id)
        payload: dict[str, object] = {
            "schema": _LAUNCH_SCHEMA,
            "project_root": str(self.project_root),
            "run_id": intent.run_id,
            "job_id": intent.job_id,
            "action_id": intent.action_id,
            "owner_token": intent.owner_token,
            "fencing_epoch": intent.fencing_epoch,
            "entity_version": principal.entity_version,
            "invocation_digest": digest,
            "launch_token": intent.launch_token,
            "completion_marker_path": str(expected_marker),
            "argv": [str(executable), *invocation.argv[1:]],
            "cwd": str(cwd),
            "heartbeat_interval": self._heartbeat_interval,
            "lease_ttl": self._lease_ttl,
            "worker_result_binding": worker_result_binding,
            "candidate_context": (
                None if invocation.candidate_context is None
                else invocation.candidate_context.to_payload()
            ),
        }

        def guarded_spawn() -> DetachedSupervisorHandle:
            try:
                _publish_exclusive(launch_path, payload)
            except FileExistsError as error:
                raise SupervisorAlreadyStarted(
                    intent.action_id, self._prior_incarnation(intent.action_id)) from error
            command = (
                sys.executable, "-m", "waystone.runs.supervisor",
                "--supervise", str(launch_path),
            )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    env=self._supervisor_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as error:
                try:
                    launch_path.unlink()
                    _fsync_directory(launch_path.parent)
                except OSError as cleanup_error:
                    raise SupervisorStateError(
                        launch_path,
                        "detached launch failed and its known-unused reservation "
                        f"could not be removed: {cleanup_error}",
                    ) from cleanup_error
                raise SupervisorLaunchRefused(
                    intent.action_id,
                    f"detached supervisor process could not start: {error}") from error
            threading.Thread(
                target=process.wait,
                name=f"waystone-supervisor-reaper-{intent.action_id}",
                daemon=True,
            ).start()
            return DetachedSupervisorHandle(intent.action_id, process.pid, launch_path)

        return self._leases.guard_effect_start(principal, guarded_spawn)

    def _worker_result_binding(
            self, action_id: str, run_id: str) -> dict[str, str] | None:
        """Freeze staged result-adapter inputs; legacy non-RunSpec fixtures remain v1."""
        with self._store._connection_lock:  # noqa: SLF001 - immutable spec presence probe
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT 1 FROM artifacts WHERE reference_id LIKE ? LIMIT 1",
                (f"run-spec:{run_id}:%",),
            ).fetchone()
        if row is None:
            return None
        from waystone.runs.spec import load_run_spec

        spec = load_run_spec(run_id, start=self.project_root)
        action = self._store.get_entity(EntityKind.ACTION, action_id)
        attempt_id = action.parent_attempt_id
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SupervisorLaunchRefused(
                action_id, "staged runner action lacks its exact attempt binding")
        return {
            "attempt_id": attempt_id,
            "run_spec_digest": spec.run_spec_digest,
            "work_brief_digest": spec.work_brief.digest,
            "base_snapshot_digest": spec.base_snapshot.digest,
        }

    def runner_executor(self, intent: RunnerLaunchIntent) -> None:
        """``RunnerExecutor`` adapter: launch detached and return without waiting."""
        self.launch(intent)

    def _load_heartbeat(self, action_id: str) -> SupervisorHeartbeat | None:
        path = self._heartbeat_path(action_id)
        if not path.exists():
            return None
        payload = _read_heartbeat(path)
        try:
            return SupervisorHeartbeat(
                action_id=payload["action_id"],
                fencing_epoch=payload["fencing_epoch"],
                host_boot_identity=payload["host_boot_identity"],
                monotonic_observed_at=payload["monotonic_observed_at"],
                wall_observed_at=payload["wall_observed_at"],
                process_identity=ProcessIdentity.from_payload(payload["process_identity"]),
            )
        except (TypeError, ValueError) as error:
            raise SupervisorStateError(path, f"heartbeat fields are invalid: {error}") from error

    def validate_completion_marker(self, marker: RunnerCompletionMarker) -> None:
        """Reject worker-authored or stale-fence markers against supervisor evidence."""
        if not isinstance(marker, RunnerCompletionMarker):
            raise TypeError("marker must be a RunnerCompletionMarker")
        try:
            launch = _read_launch(self._launch_path(marker.action_id))
            runtime = _read_runtime(self._runtime_path(marker.action_id))
            wait_receipt = _read_wait(self._wait_path(marker.action_id))
            identity = ProcessIdentity.from_payload(runtime["process_identity"])
        except (SupervisorError, TypeError, ValueError) as error:
            raise CompletionMarkerRefused(
                marker.action_id, f"supervisor evidence is unavailable: {error}") from error
        expected = {
            "run_id": marker.run_id,
            "job_id": marker.job_id,
            "action_id": marker.action_id,
            "fencing_epoch": marker.fencing_epoch,
            "launch_token": marker.launch_token,
        }
        if any(launch.get(key) != value for key, value in expected.items()):
            raise CompletionMarkerRefused(
                marker.action_id, "marker identity or fencing does not match the launch")
        if any(runtime.get(key) != value for key, value in expected.items()):
            raise CompletionMarkerRefused(
                marker.action_id, "marker identity or fencing does not match runtime evidence")
        marker_wait_fields = {
            "run_id": marker.run_id,
            "job_id": marker.job_id,
            "action_id": marker.action_id,
            "fencing_epoch": marker.fencing_epoch,
            "launch_token": marker.launch_token,
            "process_identity": marker.process_identity,
            "started_at": marker.started_at,
            "finished_at": marker.finished_at,
            "returncode": marker.returncode,
            "signal": marker.signal,
            "stdout_artifact_digest": marker.stdout_artifact_digest,
            "stderr_artifact_digest": marker.stderr_artifact_digest,
        }
        if marker.worker_result_digest is not None:
            marker_wait_fields["worker_result_digest"] = marker.worker_result_digest
        if any(wait_receipt.get(key) != value for key, value in marker_wait_fields.items()):
            raise CompletionMarkerRefused(
                marker.action_id, "marker does not match supervisor wait evidence")
        if (identity.action_id != marker.action_id
                or identity.fencing_epoch != marker.fencing_epoch
                or identity.supervisor_owner_token != launch["owner_token"]
                or identity.invocation_digest != launch["invocation_digest"]
                or marker.process_identity != identity.canonical
                or wait_receipt["supervisor_identity"]
                != ProcessIdentity.from_payload(runtime["supervisor_identity"]).canonical):
            raise CompletionMarkerRefused(
                marker.action_id, "process identity does not match supervisor authority")

    def runner_identity_verifier(self, marker: RunnerCompletionMarker) -> bool:
        """``RunnerIdentityVerifier`` adapter; typed refusals remain visible upstream."""
        self.validate_completion_marker(marker)
        return True

    def probe_action(self, action_id: str, *, stale_after: float = 5.0) -> LivenessObservation:
        """Return positive alive/exit only; stale silence never becomes exit."""
        identity_text = _nonempty(action_id, "action_id")
        runtime_path = self._runtime_path(identity_text)
        try:
            runtime = _read_runtime(runtime_path)
            identity = ProcessIdentity.from_payload(runtime["process_identity"])
        except (SupervisorError, TypeError, ValueError) as error:
            return LivenessObservation(
                LivenessState.UNKNOWN,
                f"process-identity-unavailable:{type(error).__name__}")
        heartbeat = HeartbeatFreshness.UNKNOWN
        try:
            telemetry = self._load_heartbeat(identity_text)
            if telemetry is not None and telemetry.process_identity == identity:
                heartbeat = heartbeat_freshness(
                    telemetry, stale_after=stale_after)
        except SupervisorError:
            heartbeat = HeartbeatFreshness.UNKNOWN

        marker_path = self._marker_path(identity_text)
        if marker_path.exists():
            try:
                marker = _read_marker(marker_path)
                self.validate_completion_marker(marker)
            except (SupervisorError, TypeError, ValueError):
                return LivenessObservation(
                    LivenessState.UNKNOWN, "completion-marker-invalid", False, heartbeat)
            return LivenessObservation(
                LivenessState.EXITED, "supervisor-wait-status", True, heartbeat)
        return observe_process_identity(identity, heartbeat=heartbeat)

    def positive_absence_probe(self, action_id: str) -> bool:
        """Expose exact-identity absence separately from public tri-state liveness."""
        return self.probe_action(action_id).exact_identity_absent

    def effect_absence_probe(self, plan: EffectPlan) -> bool:
        """Plan-shaped adapter for a runner-specific effects absence seam."""
        if not isinstance(plan, EffectPlan):
            raise TypeError("plan must be an EffectPlan")
        return self.positive_absence_probe(plan.action_id)

    def quiescence_probe(self, plan: EffectPlan) -> bool:
        """Effects adapter: only positive exit, never identity-mismatch, is quiescent."""
        if not isinstance(plan, EffectPlan):
            raise TypeError("plan must be an EffectPlan")
        return self.probe_action(plan.action_id).state is LivenessState.EXITED


_LAUNCH_FIELDS = {
    "schema", "project_root", "run_id", "job_id", "action_id", "owner_token",
    "fencing_epoch", "entity_version", "invocation_digest", "launch_token",
    "completion_marker_path", "argv", "cwd", "heartbeat_interval", "lease_ttl",
    "worker_result_binding", "candidate_context",
}
_RUNTIME_FIELDS = {
    "schema", "run_id", "job_id", "action_id", "owner_token", "fencing_epoch",
    "entity_version", "invocation_digest", "launch_token", "started_at",
    "supervisor_identity", "process_identity",
}
_HEARTBEAT_FIELDS = {
    "schema", "action_id", "fencing_epoch", "host_boot_identity",
    "monotonic_observed_at", "wall_observed_at", "process_identity",
}
_WAIT_FIELDS = {
    "schema", "run_id", "job_id", "action_id", "fencing_epoch", "launch_token",
    "process_identity", "supervisor_identity", "started_at", "finished_at",
    "returncode", "signal", "stdout_artifact_digest", "stderr_artifact_digest",
}
_MARKER_FIELDS = {
    "schema", "run_id", "job_id", "action_id", "fencing_epoch", "launch_token",
    "process_identity", "started_at", "finished_at", "returncode", "signal",
    "stdout_artifact_digest", "stderr_artifact_digest",
}
_MARKER_FIELDS_V2 = _MARKER_FIELDS | {"worker_result_digest"}


def _read_launch(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupervisorStateError(path, f"launch evidence is unreadable: {error}") from error
    optional = {"worker_result_binding", "candidate_context"}
    present_optional = optional & set(raw) if isinstance(raw, dict) else set()
    fields = _LAUNCH_FIELDS - (optional - present_optional)
    payload = _read_object(path, schema=_LAUNCH_SCHEMA, fields=fields)
    payload.setdefault("worker_result_binding", None)
    payload.setdefault("candidate_context", None)
    try:
        for field in (
                "project_root", "run_id", "job_id", "action_id", "owner_token",
                "invocation_digest", "launch_token", "completion_marker_path", "cwd"):
            _nonempty(payload[field], f"launch.{field}")
        _positive_int(payload["fencing_epoch"], "launch.fencing_epoch")
        _nonnegative_int(payload["entity_version"], "launch.entity_version")
        validate_sha256_digest(payload["invocation_digest"])
        _positive_finite(payload["heartbeat_interval"], "launch.heartbeat_interval")
        ttl = _positive_finite(payload["lease_ttl"], "launch.lease_ttl")
        if ttl <= payload["heartbeat_interval"]:
            raise ValueError("launch lease_ttl must exceed heartbeat_interval")
        binding = payload["worker_result_binding"]
        if binding is not None:
            if not isinstance(binding, dict) or set(binding) != {
                    "attempt_id", "run_spec_digest", "work_brief_digest",
                    "base_snapshot_digest"}:
                raise ValueError("launch worker_result_binding fields are invalid")
            _nonempty(binding["attempt_id"], "launch.worker_result_binding.attempt_id")
            for field in ("run_spec_digest", "work_brief_digest", "base_snapshot_digest"):
                validate_sha256_digest(binding[field])
        candidate = payload["candidate_context"]
        if candidate is not None:
            if not isinstance(candidate, dict) or set(candidate) != {
                    "candidate_oid", "root_fingerprint", "run_spec_digest"}:
                raise ValueError("launch candidate_context fields are invalid")
            RunnerCandidateContext(
                candidate["candidate_oid"],
                candidate["root_fingerprint"],
                candidate["run_spec_digest"],
            )
        argv = payload["argv"]
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(value, str) for value in argv)):
            raise ValueError("launch argv must be a non-empty string list")
    except (TypeError, ValueError) as error:
        raise SupervisorStateError(path, f"launch fields are invalid: {error}") from error
    return payload


def _read_runtime(path: Path) -> dict[str, object]:
    payload = _read_object(path, schema=_RUNTIME_SCHEMA, fields=_RUNTIME_FIELDS)
    try:
        for field in (
                "run_id", "job_id", "action_id", "owner_token",
                "invocation_digest", "launch_token", "started_at"):
            _nonempty(payload[field], f"runtime.{field}")
        _positive_int(payload["fencing_epoch"], "runtime.fencing_epoch")
        _nonnegative_int(payload["entity_version"], "runtime.entity_version")
        validate_sha256_digest(payload["invocation_digest"])
        ProcessIdentity.from_payload(payload["supervisor_identity"])
        ProcessIdentity.from_payload(payload["process_identity"])
    except (TypeError, ValueError) as error:
        raise SupervisorStateError(path, f"runtime fields are invalid: {error}") from error
    return payload


def _read_heartbeat(path: Path) -> dict[str, object]:
    return _read_object(path, schema=_HEARTBEAT_SCHEMA, fields=_HEARTBEAT_FIELDS)


def _read_wait(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupervisorStateError(path, f"wait evidence is unreadable: {error}") from error
    fields = _WAIT_FIELDS | ({"worker_result_digest"} if isinstance(raw, dict)
                             and "worker_result_digest" in raw else set())
    payload = _read_object(path, schema=_WAIT_SCHEMA, fields=fields)
    try:
        for field in (
                "run_id", "job_id", "action_id", "launch_token", "process_identity",
                "supervisor_identity", "started_at", "finished_at",
                "stdout_artifact_digest", "stderr_artifact_digest"):
            _nonempty(payload[field], f"wait.{field}")
        _positive_int(payload["fencing_epoch"], "wait.fencing_epoch")
        if sum(payload[field] is not None for field in ("returncode", "signal")) != 1:
            raise ValueError("wait evidence requires exactly one process result")
        for field in ("returncode", "signal"):
            value = payload[field]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"wait.{field} must be an integer or null")
        validate_sha256_digest(payload["stdout_artifact_digest"])
        validate_sha256_digest(payload["stderr_artifact_digest"])
        if "worker_result_digest" in payload:
            validate_sha256_digest(payload["worker_result_digest"])
    except (TypeError, ValueError) as error:
        raise SupervisorStateError(path, f"wait fields are invalid: {error}") from error
    return payload


def _read_marker(path: Path) -> RunnerCompletionMarker:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupervisorStateError(path, f"completion marker is unreadable: {error}") from error
    if not isinstance(raw, dict):
        raise SupervisorStateError(path, "completion marker is not an object")
    schema = raw.get("schema")
    fields = _MARKER_FIELDS_V2 if schema == _MARKER_SCHEMA_V2 else _MARKER_FIELDS
    payload = _read_object(path, schema=schema, fields=fields)
    if schema not in {_MARKER_SCHEMA, _MARKER_SCHEMA_V2}:
        raise SupervisorStateError(path, "completion marker schema is unsupported")
    try:
        marker = RunnerCompletionMarker(
            run_id=payload["run_id"],
            job_id=payload["job_id"],
            action_id=payload["action_id"],
            fencing_epoch=payload["fencing_epoch"],
            launch_token=payload["launch_token"],
            process_identity=payload["process_identity"],
            started_at=payload["started_at"],
            finished_at=payload["finished_at"],
            returncode=payload["returncode"],
            signal=payload["signal"],
            stdout_artifact_digest=payload["stdout_artifact_digest"],
            stderr_artifact_digest=payload["stderr_artifact_digest"],
            worker_result_digest=payload.get("worker_result_digest"),
        )
        # Reuse the public publisher's dataclass validation without mutating state.
        for value, label in (
                (marker.run_id, "marker.run_id"),
                (marker.job_id, "marker.job_id"),
                (marker.action_id, "marker.action_id"),
                (marker.launch_token, "marker.launch_token"),
                (marker.process_identity, "marker.process_identity"),
                (marker.started_at, "marker.started_at"),
                (marker.finished_at, "marker.finished_at")):
            _nonempty(value, label)
        _positive_int(marker.fencing_epoch, "marker.fencing_epoch")
        if sum(value is not None for value in (marker.returncode, marker.signal)) != 1:
            raise ValueError("marker requires exactly one of returncode or signal")
        for value in (marker.returncode, marker.signal):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError("marker result must be an integer")
        validate_sha256_digest(marker.stdout_artifact_digest)
        validate_sha256_digest(marker.stderr_artifact_digest)
        if marker.worker_result_digest is not None:
            validate_sha256_digest(marker.worker_result_digest)
    except (TypeError, ValueError) as error:
        raise SupervisorStateError(path, f"completion marker fields are invalid: {error}") from error
    return marker


def _runtime_path(project_root: Path, action_id: str) -> Path:
    return (
        project_root / ".waystone" / "supervisors"
        / _action_filename(action_id, ".runtime.json")
    )


def _heartbeat_path(project_root: Path, action_id: str) -> Path:
    return (
        project_root / ".waystone" / "supervisors"
        / _action_filename(action_id, ".heartbeat.json")
    )


def _wait_path(project_root: Path, action_id: str) -> Path:
    return (
        project_root / ".waystone" / "supervisors"
        / _action_filename(action_id, ".wait.json")
    )


def _run_detached_supervisor(launch_path: Path) -> int:
    launch = _read_launch(Path(launch_path))
    project_root = Path(launch["project_root"])
    action_id = launch["action_id"]
    expected_launch_path = (
        project_root / ".waystone" / "supervisors"
        / _action_filename(action_id, ".launch.json")
    )
    if Path(launch_path) != expected_launch_path:
        raise SupervisorStateError(
            Path(launch_path), "launch path does not match its action identity")
    marker_path = Path(launch["completion_marker_path"])
    expected_marker = (
        project_root / ".waystone" / "runner-completions"
        / _action_filename(action_id, ".json")
    )
    if marker_path != expected_marker:
        raise SupervisorStateError(marker_path, "marker path is not engine-owned")
    candidate_mismatch = _candidate_context_mismatch(
        Path(launch["cwd"]), launch["candidate_context"])
    if candidate_mismatch is not None:
        raise SupervisorStateError(Path(launch_path), candidate_mismatch)

    store = RunStore.open(project_root)
    try:
        leases = LeaseManager(store)
        principal = LeasePrincipal(
            run_id=launch["run_id"],
            action_id=action_id,
            owner_token=launch["owner_token"],
            fencing_epoch=launch["fencing_epoch"],
            entity_version=launch["entity_version"],
            monotonic_deadline=0.0,
        )
        boot = host_boot_identity()
        supervisor_identity = capture_process_identity(
            os.getpid(), action_id=action_id,
            owner_token=principal.owner_token,
            fencing_epoch=principal.fencing_epoch,
            resolved_executable=str(Path(sys.executable).resolve()),
            boot_identity=boot,
        )

        def guarded_worker_spawn():
            started_at = _utc_now()
            try:
                process = subprocess.Popen(
                    launch["argv"],
                    cwd=launch["cwd"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                )
            except OSError as error:
                raise SupervisorLaunchRefused(
                    action_id, f"runner process could not start: {error}") from error
            identity: ProcessIdentity | None = None
            try:
                identity = capture_process_identity(
                    process.pid,
                    action_id=action_id,
                    owner_token=principal.owner_token,
                    fencing_epoch=principal.fencing_epoch,
                    resolved_executable=launch["argv"][0],
                    invocation_digest=launch["invocation_digest"],
                    boot_identity=boot,
                )
                runtime = {
                    "schema": _RUNTIME_SCHEMA,
                    "run_id": launch["run_id"],
                    "job_id": launch["job_id"],
                    "action_id": action_id,
                    "owner_token": principal.owner_token,
                    "fencing_epoch": principal.fencing_epoch,
                    "entity_version": principal.entity_version,
                    "invocation_digest": launch["invocation_digest"],
                    "launch_token": launch["launch_token"],
                    "started_at": started_at,
                    "supervisor_identity": supervisor_identity.to_payload(),
                    "process_identity": identity.to_payload(),
                }
                _publish_exclusive(_runtime_path(project_root, action_id), runtime)
            except BaseException:
                _settle_worker_after_failure(process, identity)
                raise
            return process, identity, started_at

        process, identity, started_at = leases.guard_effect_start(
            principal, guarded_worker_spawn)

        def renew_heartbeat(current: LeasePrincipal) -> LeasePrincipal:
            renewed = leases.renew(current, ttl_seconds=launch["lease_ttl"])
            heartbeat = SupervisorHeartbeat(
                action_id=action_id,
                fencing_epoch=renewed.fencing_epoch,
                host_boot_identity=boot,
                monotonic_observed_at=time.monotonic(),
                wall_observed_at=_utc_now(),
                process_identity=identity,
            )
            _replace_atomic(
                _heartbeat_path(project_root, action_id), heartbeat.to_payload())
            return renewed

        try:
            principal = renew_heartbeat(principal)
            while True:
                try:
                    stdout, stderr = process.communicate(
                        timeout=launch["heartbeat_interval"])
                    break
                except subprocess.TimeoutExpired:
                    principal = renew_heartbeat(principal)
        except BaseException:
            _settle_worker_after_failure(process, identity)
            raise

        finished_at = _utc_now()
        candidate_mismatch = _candidate_context_mismatch(
            Path(launch["cwd"]), launch["candidate_context"])
        effective_returncode = process.returncode
        if effective_returncode == 0 and candidate_mismatch is not None:
            effective_returncode = 1
            stderr = (
                stderr
                + (b"\n" if stderr else b"")
                + candidate_mismatch.encode("utf-8", errors="backslashreplace")
            )
        artifacts = ArtifactStore(project_root)
        stdout_artifact = artifacts.write(stdout)
        stderr_artifact = artifacts.write(stderr)
        if effective_returncode is None:
            raise SupervisorStateError(
                marker_path, "wait completed without a process return code")
        returncode = effective_returncode if effective_returncode >= 0 else None
        signal = -effective_returncode if effective_returncode < 0 else None
        worker_result_digest = None
        binding = launch["worker_result_binding"]
        if binding is not None and returncode == 0:
            from waystone.runs.worker_result import WorkerResultAdapter

            try:
                adapted = WorkerResultAdapter(Path(launch["cwd"]), artifacts).adapt(
                    run_id=launch["run_id"],
                    job_id=launch["job_id"],
                    attempt_id=binding["attempt_id"],
                    run_spec_digest=binding["run_spec_digest"],
                    work_brief_digest=binding["work_brief_digest"],
                    base_snapshot_digest=binding["base_snapshot_digest"],
                )
            except WorkflowError:
                # The process exit and stream artifacts remain authoritative even when the
                # reserved result is absent or invalid.  A v1 marker lets the engine observe
                # and terminalize that typed stage failure instead of leaving running state.
                pass
            else:
                worker_result_digest = adapted.worker_result_artifact.digest
        marker = RunnerCompletionMarker(
            run_id=launch["run_id"],
            job_id=launch["job_id"],
            action_id=action_id,
            fencing_epoch=principal.fencing_epoch,
            launch_token=launch["launch_token"],
            process_identity=identity.canonical,
            started_at=started_at,
            finished_at=finished_at,
            returncode=returncode,
            signal=signal,
            stdout_artifact_digest=stdout_artifact.digest,
            stderr_artifact_digest=stderr_artifact.digest,
            worker_result_digest=worker_result_digest,
        )
        wait_receipt = {
            "schema": _WAIT_SCHEMA,
            "run_id": marker.run_id,
            "job_id": marker.job_id,
            "action_id": marker.action_id,
            "fencing_epoch": marker.fencing_epoch,
            "launch_token": marker.launch_token,
            "process_identity": marker.process_identity,
            "supervisor_identity": supervisor_identity.canonical,
            "started_at": marker.started_at,
            "finished_at": marker.finished_at,
            "returncode": marker.returncode,
            "signal": marker.signal,
            "stdout_artifact_digest": marker.stdout_artifact_digest,
            "stderr_artifact_digest": marker.stderr_artifact_digest,
        }
        if marker.worker_result_digest is not None:
            wait_receipt["worker_result_digest"] = marker.worker_result_digest

        def publish_completion() -> None:
            _publish_exclusive(_wait_path(project_root, action_id), wait_receipt)
            publish_runner_completion(marker_path, marker)

        leases.guard_completion(principal, publish_completion)
        return 0
    finally:
        store.close()


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--supervise", type=Path, required=True)
    arguments = parser.parse_args(argv)
    return _run_detached_supervisor(arguments.supervise)


if __name__ == "__main__":
    raise SystemExit(_main())
