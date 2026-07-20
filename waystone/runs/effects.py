"""Crash-reconcilable commit protocol for engine-owned external effects."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping

from waystone.adapters import git as git_adapter
from waystone.core import WorkflowError
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
    validate_sha256_digest,
)
from waystone.runs.lease import LeaseManager, LeasePrincipal
from waystone.runs.store import (
    EntityKind,
    EntityRecord,
    RecordNotFoundError,
    RunStore,
    RunnerInvocationConflict,
    TransitionReason,
)


_OID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PLAN_SCHEMA = "waystone-effect-plan-1"
_INTENT_SCHEMA = "waystone-effect-intent-1"
_OBSERVATION_SCHEMA = "waystone-effect-observation-1"
_RUNNER_MARKER_SCHEMA = "waystone-runner-completion-1"


class EffectError(WorkflowError):
    """Base class for typed external-effect failures."""

    code = "effect_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class UnsupportedEffectKind(EffectError):
    """The requested kind has no registered implementation."""

    code = "unsupported_effect_kind"

    def __init__(self, kind: object):
        self.kind = kind
        super().__init__(f"effect kind {kind!r} is not registered")


class InvalidEffectPlan(EffectError):
    """A plan or its immutable evidence cannot be validated."""

    code = "invalid_effect_plan"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class EffectStateRefusal(EffectError):
    """An operation is not legal from the action's current protocol state."""

    code = "effect_state_refusal"

    def __init__(self, action_id: str, operation: str, state: str):
        self.action_id = action_id
        self.operation = operation
        self.state = state
        super().__init__(
            f"cannot {operation} action {action_id!r} from protocol state {state!r}")


class EffectAlreadyExecuted(EffectStateRefusal):
    """Direct execution cannot repeat an existing effect intent or result."""

    code = "effect_already_executed"

    def __init__(self, action_id: str, state: str):
        EffectError.__init__(
            self, f"action {action_id!r} already reached effect state {state!r}; reconcile it")
        self.action_id = action_id
        self.operation = "execute"
        self.state = state


class EffectRetryRefused(EffectError):
    """A retry did not allocate both a new attempt and a new action identity."""

    code = "effect_retry_refused"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"retry of action {action_id!r} refused: {detail}")


class EffectExecutionFailed(EffectError):
    """The executor returned without establishing the desired authority state."""

    code = "effect_execution_failed"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class RunnerMarkerError(EffectError):
    """A completion marker cannot be published without overwriting or ambiguity."""

    code = "runner_marker_error"

    def __init__(self, path: Path, detail: str):
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"runner marker {path}: {detail}")


class EffectKind(str, Enum):
    GIT_REF = "git-ref"
    WORKTREE = "worktree"
    ARTIFACT_WRITE = "artifact-write"
    RUNNER_EXECUTION = "runner-execution"
    PATCH_INTEGRATION = "patch-integration"


class ObservationDisposition(str, Enum):
    ABSENT = "absent"
    DESIRED = "desired"
    IN_FLIGHT = "in-flight"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class EffectResultState(str, Enum):
    COMPLETED = "completed"
    NOOP = "no-op"
    UNKNOWN_EFFECT = "unknown-effect"
    IN_FLIGHT = "in-flight"
    CONFLICT = "conflict"
    EXITED_UNRECONCILED = "exited-unreconciled"


@dataclass(frozen=True)
class GitRefEffect:
    repository: Path
    ref: str
    expected_old_oid: str | None
    desired_oid: str


@dataclass(frozen=True)
class WorktreeEffect:
    repository: Path
    path: Path
    dedicated_ref: str
    expected_head_oid: str


@dataclass(frozen=True)
class ArtifactWriteEffect:
    content: bytes


@dataclass(frozen=True)
class RunnerExecutionEffect:
    invocation_digest: str


@dataclass(frozen=True)
class PatchIntegrationEffect:
    repository: Path
    target_ref: str
    expected_parent_oid: str
    expected_parent_tree_oid: str
    integration_commit_oid: str
    integration_tree_oid: str


EffectSpec = (
    GitRefEffect | WorktreeEffect | ArtifactWriteEffect | RunnerExecutionEffect
    | PatchIntegrationEffect
)

_EFFECT_REGISTRY: dict[type[object], EffectKind] = {
    GitRefEffect: EffectKind.GIT_REF,
    WorktreeEffect: EffectKind.WORKTREE,
    ArtifactWriteEffect: EffectKind.ARTIFACT_WRITE,
    RunnerExecutionEffect: EffectKind.RUNNER_EXECUTION,
    PatchIntegrationEffect: EffectKind.PATCH_INTEGRATION,
}


@dataclass(frozen=True)
class EffectObservation:
    disposition: ObservationDisposition
    evidence: Mapping[str, object]
    observed_digest: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class EffectPlan:
    run_id: str
    job_id: str
    attempt_id: str
    action_id: str
    kind: EffectKind
    spec: Mapping[str, object]
    input_digest: str
    idempotency_key: str
    plan_digest: str
    retry_of: str | None = None


@dataclass(frozen=True)
class ClaimedEffect:
    plan: EffectPlan
    principal: LeasePrincipal


@dataclass(frozen=True)
class EffectResult:
    action_id: str
    state: EffectResultState
    principal: LeasePrincipal | None = None
    observed_digest: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunnerLaunchIntent:
    run_id: str
    job_id: str
    action_id: str
    owner_token: str
    fencing_epoch: int
    invocation_digest: str
    launch_token: str
    completion_marker_path: Path


@dataclass(frozen=True)
class RunnerCompletionMarker:
    run_id: str
    job_id: str
    action_id: str
    fencing_epoch: int
    launch_token: str
    process_identity: str
    started_at: str
    finished_at: str
    returncode: int | None
    signal: int | None
    stdout_artifact_digest: str
    stderr_artifact_digest: str


RunnerExecutor = Callable[[RunnerLaunchIntent], None]
RunnerIdentityVerifier = Callable[[RunnerCompletionMarker], bool]
QuiescenceProbe = Callable[[EffectPlan], bool]


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("effect evidence must be canonical-JSON serializable") from error


def _bytes_digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _payload_digest(payload: object) -> str:
    return _bytes_digest(_canonical_bytes(payload))


def _git_rc(repository: Path, *args: str) -> tuple[int, str, str]:
    try:
        return git_adapter.git_rc(repository, *args)
    except Exception as error:
        return 127, "", f"git adapter observation failed: {error}"


def _validate_oid(value: str, label: str) -> str:
    if not isinstance(value, str) or _OID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase 40- or 64-hex Git OID")
    return value


def _resolved_repository(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, TypeError) as error:
        raise ValueError(f"{label} must name an existing repository directory") from error
    if not resolved.is_dir():
        raise ValueError(f"{label} must name an existing repository directory")
    return resolved


def _marker_payload(marker: RunnerCompletionMarker) -> dict[str, object]:
    if not isinstance(marker, RunnerCompletionMarker):
        raise TypeError("marker must be a RunnerCompletionMarker")
    for value, label in (
            (marker.run_id, "marker.run_id"),
            (marker.job_id, "marker.job_id"),
            (marker.action_id, "marker.action_id"),
            (marker.launch_token, "marker.launch_token"),
            (marker.process_identity, "marker.process_identity"),
            (marker.started_at, "marker.started_at"),
            (marker.finished_at, "marker.finished_at")):
        _nonempty(value, label)
    if (isinstance(marker.fencing_epoch, bool)
            or not isinstance(marker.fencing_epoch, int) or marker.fencing_epoch < 1):
        raise ValueError("marker.fencing_epoch must be a positive integer")
    values = (marker.returncode, marker.signal)
    if sum(value is not None for value in values) != 1:
        raise ValueError("marker must contain exactly one of returncode or signal")
    for value, label in zip(values, ("returncode", "signal")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"marker.{label} must be an integer or null")
    stdout_digest = validate_sha256_digest(marker.stdout_artifact_digest)
    stderr_digest = validate_sha256_digest(marker.stderr_artifact_digest)
    return {
        "schema": _RUNNER_MARKER_SCHEMA,
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
        "stdout_artifact_digest": stdout_digest,
        "stderr_artifact_digest": stderr_digest,
    }


def publish_runner_completion(path: Path, marker: RunnerCompletionMarker) -> None:
    """Publish one immutable completion marker using a same-directory atomic link."""
    marker_path = Path(path)
    payload = _canonical_bytes(_marker_payload(marker))
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        parent_info = marker_path.parent.lstat()
    except OSError as error:
        raise RunnerMarkerError(marker_path, f"cannot create marker directory: {error}") from error
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise RunnerMarkerError(marker_path, "marker directory must be a real directory")

    try:
        existing = marker_path.read_bytes()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise RunnerMarkerError(marker_path, f"cannot read existing marker: {error}") from error
    if existing is not None:
        if existing == payload:
            return
        raise RunnerMarkerError(marker_path, "different marker bytes already exist")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "wb", dir=marker_path.parent, prefix=".runner-marker-", suffix=".tmp",
                delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, marker_path)
        except FileExistsError:
            try:
                raced = marker_path.read_bytes()
            except OSError as error:
                raise RunnerMarkerError(
                    marker_path, f"cannot verify raced marker: {error}") from error
            if raced != payload:
                raise RunnerMarkerError(marker_path, "different marker won publication race")
        descriptor = os.open(marker_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except RunnerMarkerError:
        raise
    except OSError as error:
        raise RunnerMarkerError(marker_path, f"atomic publication failed: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class EffectEngine:
    """Own the durable intent, authority observation, and fenced DB commit lifecycle."""

    def __init__(
            self, store: RunStore, leases: LeaseManager, *,
            runner_executor: RunnerExecutor | None = None,
            runner_identity_verifier: RunnerIdentityVerifier | None = None):
        if not isinstance(store, RunStore):
            raise TypeError("store must be a RunStore")
        if not isinstance(leases, LeaseManager):
            raise TypeError("leases must be a LeaseManager")
        if leases._store is not store:  # noqa: SLF001 - package-internal composition contract
            raise ValueError("leases and effects must share one RunStore")
        if runner_executor is not None and not callable(runner_executor):
            raise TypeError("runner_executor must be callable")
        if runner_identity_verifier is not None and not callable(runner_identity_verifier):
            raise TypeError("runner_identity_verifier must be callable")
        self._store = store
        self._leases = leases
        self._artifacts = ArtifactStore(store.project_root)
        self._runner_executor = runner_executor
        self._runner_identity_verifier = runner_identity_verifier

    def _effect_fault_point(self, stage: str, plan: EffectPlan) -> None:
        """Private deterministic crash-injection seam; production performs no action."""
        del stage, plan

    def _runner_marker_path(self, action_id: str) -> Path:
        filename = hashlib.sha256(action_id.encode("utf-8")).hexdigest() + ".json"
        return self._store.project_root / ".waystone" / "runner-completions" / filename

    def _normalize_effect(
            self, effect: EffectSpec | str, *, action_id: str,
            stored: bool = False,
    ) -> tuple[EffectKind, dict[str, object], object, object]:
        if isinstance(effect, str):
            try:
                kind = EffectKind(effect)
            except ValueError as error:
                raise UnsupportedEffectKind(effect) from error
            raise InvalidEffectPlan(
                action_id, f"registered kind {kind.value!r} requires its typed spec")

        registered_kind = _EFFECT_REGISTRY.get(type(effect))
        if registered_kind is None:
            kind = getattr(effect, "kind", type(effect).__name__)
            raise UnsupportedEffectKind(kind)

        if isinstance(effect, GitRefEffect):
            kind = EffectKind.GIT_REF
            repository = (
                Path(effect.repository)
                if stored else _resolved_repository(effect.repository, "git-ref repository"))
            if not repository.is_absolute():
                raise ValueError("git-ref repository must be an absolute path")
            ref = _nonempty(effect.ref, "git-ref ref")
            if not ref.startswith("refs/") or ref == "refs/":
                raise InvalidEffectPlan(
                    action_id, "git-ref ref must be a full refs/* name")
            expected = (
                None if effect.expected_old_oid is None
                else _validate_oid(effect.expected_old_oid, "expected_old_oid"))
            desired = _validate_oid(effect.desired_oid, "desired_oid")
            spec = {
                "repository": str(repository), "ref": ref,
                "expected_old_oid": expected, "desired_oid": desired,
            }
            return kind, spec, {"repository": str(repository), "ref": ref}, expected

        if isinstance(effect, WorktreeEffect):
            kind = EffectKind.WORKTREE
            repository = (
                Path(effect.repository)
                if stored else _resolved_repository(effect.repository, "worktree repository"))
            if not repository.is_absolute():
                raise ValueError("worktree repository must be an absolute path")
            try:
                path = Path(effect.path).resolve(strict=False)
            except (OSError, TypeError) as error:
                raise ValueError("worktree path cannot be resolved") from error
            ref = _nonempty(effect.dedicated_ref, "worktree dedicated_ref")
            if not ref.startswith("refs/heads/") or ref == "refs/heads/":
                raise ValueError("worktree dedicated_ref must be a full refs/heads/* ref")
            head = _validate_oid(effect.expected_head_oid, "expected_head_oid")
            spec = {
                "repository": str(repository), "path": str(path),
                "dedicated_ref": ref, "expected_head_oid": head,
            }
            target = {"repository": str(repository), "path": str(path), "ref": ref}
            return kind, spec, target, head

        if isinstance(effect, ArtifactWriteEffect):
            kind = EffectKind.ARTIFACT_WRITE
            if not isinstance(effect.content, bytes):
                raise TypeError("artifact effect content must be bytes")
            digest = _bytes_digest(effect.content)
            spec = {
                "content_base64": base64.b64encode(effect.content).decode("ascii"),
                "content_digest": digest,
                "size": len(effect.content),
            }
            return kind, spec, {"digest_path": str(self._artifacts.path_for(digest))}, digest

        if isinstance(effect, RunnerExecutionEffect):
            kind = EffectKind.RUNNER_EXECUTION
            invocation = validate_sha256_digest(effect.invocation_digest)
            marker = self._runner_marker_path(action_id)
            spec = {
                "invocation_digest": invocation,
                "completion_marker": str(marker),
            }
            return kind, spec, {"completion_marker": str(marker)}, "not-launched"

        if isinstance(effect, PatchIntegrationEffect):
            kind = EffectKind.PATCH_INTEGRATION
            repository = (
                Path(effect.repository)
                if stored else _resolved_repository(effect.repository, "patch repository"))
            if not repository.is_absolute():
                raise ValueError("patch repository must be an absolute path")
            target_ref = _nonempty(effect.target_ref, "patch target_ref")
            if not target_ref.startswith("refs/") or target_ref == "refs/":
                raise InvalidEffectPlan(
                    action_id, "patch target_ref must be a full refs/* name")
            parent = _validate_oid(effect.expected_parent_oid, "expected_parent_oid")
            parent_tree = _validate_oid(
                effect.expected_parent_tree_oid, "expected_parent_tree_oid")
            commit = _validate_oid(effect.integration_commit_oid, "integration_commit_oid")
            tree = _validate_oid(effect.integration_tree_oid, "integration_tree_oid")
            spec = {
                "repository": str(repository), "target_ref": target_ref,
                "expected_parent_oid": parent,
                "expected_parent_tree_oid": parent_tree,
                "integration_commit_oid": commit,
                "integration_tree_oid": tree,
            }
            target = {"repository": str(repository), "ref": target_ref, "commit": commit}
            expected = {"parent": parent, "tree": parent_tree}
            return kind, spec, target, expected

        raise UnsupportedEffectKind(registered_kind)

    def _effect_from_stored(
            self, kind: EffectKind, spec: Mapping[str, object], action_id: str) -> EffectSpec:
        try:
            if kind is EffectKind.GIT_REF:
                return GitRefEffect(
                    Path(spec["repository"]), str(spec["ref"]),
                    spec["expected_old_oid"], str(spec["desired_oid"]))
            if kind is EffectKind.WORKTREE:
                return WorktreeEffect(
                    Path(spec["repository"]), Path(spec["path"]),
                    str(spec["dedicated_ref"]), str(spec["expected_head_oid"]))
            if kind is EffectKind.ARTIFACT_WRITE:
                encoded = spec["content_base64"]
                if not isinstance(encoded, str):
                    raise TypeError("content_base64 is not text")
                content = base64.b64decode(encoded, validate=True)
                return ArtifactWriteEffect(content)
            if kind is EffectKind.RUNNER_EXECUTION:
                return RunnerExecutionEffect(str(spec["invocation_digest"]))
            return PatchIntegrationEffect(
                Path(spec["repository"]), str(spec["target_ref"]),
                str(spec["expected_parent_oid"]), str(spec["expected_parent_tree_oid"]),
                str(spec["integration_commit_oid"]), str(spec["integration_tree_oid"]))
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidEffectPlan(action_id, f"stored {kind.value} spec is malformed") from error

    def _validate_retry_identity(
            self, retry_of: str, *, run_id: str, job_id: str,
            attempt_id: str, action_id: str) -> None:
        previous_id = _nonempty(retry_of, "retry_of")
        try:
            previous = self._store.get_entity(EntityKind.ACTION, previous_id)
        except RecordNotFoundError as error:
            raise EffectRetryRefused(previous_id, "previous action does not exist") from error
        if previous.state != "completed":
            raise EffectRetryRefused(previous_id, "previous action is not positively terminal")
        if action_id == previous.entity_id:
            raise EffectRetryRefused(previous_id, "retry must allocate a new action_id")
        if attempt_id == previous.parent_attempt_id:
            raise EffectRetryRefused(previous_id, "retry must allocate a new attempt_id")
        if previous.run_id != run_id or previous.parent_job_id != job_id:
            raise EffectRetryRefused(previous_id, "retry changed the run or job identity")
        try:
            attempt = self._store.get_entity(EntityKind.ATTEMPT, attempt_id)
        except RecordNotFoundError as error:
            raise EffectRetryRefused(previous_id, "new attempt does not exist") from error
        if attempt.run_id != run_id or attempt.parent_job_id != job_id:
            raise EffectRetryRefused(previous_id, "new attempt belongs to a different run or job")

    def _runner_invocation_actions(
            self, run_id: str, job_id: str,
            invocation_digest: str) -> tuple[tuple[EffectPlan, str], ...]:
        lineage_key = self._runner_lineage_key(
            run_id, job_id, invocation_digest)
        lineage_prefix = f"runner-invocation:{lineage_key}:"
        with self._store._connection_lock:  # noqa: SLF001 - package-internal lineage query
            rows = self._store._connection.execute(  # noqa: SLF001
                "SELECT x.action_id, x.state FROM actions x JOIN artifacts a "
                "ON a.entity_kind = ? AND a.entity_id = x.action_id "
                "WHERE substr(a.reference_id, 1, length(?)) = ? "
                "ORDER BY a.transition_id",
                (EntityKind.ACTION.value, lineage_prefix, lineage_prefix),
            ).fetchall()
        matches: list[tuple[EffectPlan, str]] = []
        for row in rows:
            plan = self._load_plan(row["action_id"])
            if (plan.kind is EffectKind.RUNNER_EXECUTION
                    and plan.spec["invocation_digest"] == invocation_digest):
                matches.append((plan, row["state"]))
        return tuple(matches)

    @staticmethod
    def _runner_lineage_key(
            run_id: str, job_id: str, invocation_digest: str) -> str:
        return _payload_digest({
            "kind": EffectKind.RUNNER_EXECUTION.value,
            "run_id": run_id,
            "job_id": job_id,
            "invocation_digest": invocation_digest,
        })

    def _validate_runner_retry_lineage(
            self, *, run_id: str, job_id: str, invocation_digest: str,
            retry_of: str | None) -> None:
        matches = self._runner_invocation_actions(
            run_id, job_id, invocation_digest)
        if not matches:
            if retry_of is not None:
                raise EffectRetryRefused(
                    retry_of, "retry lineage does not name the same runner invocation")
            return
        if retry_of is None:
            raise EffectRetryRefused(
                matches[-1][0].action_id,
                "a repeated runner invocation requires explicit retry lineage")
        matching_ids = {plan.action_id for plan, _ in matches}
        if retry_of not in matching_ids:
            raise EffectRetryRefused(
                retry_of, "retry lineage does not name the same runner invocation")
        nonterminal = [
            plan.action_id for plan, state in matches if state != "completed"
        ]
        if nonterminal:
            raise EffectRetryRefused(
                nonterminal[-1],
                "the runner invocation has a nonterminal or uncertain action")

    def plan_effect(
            self, run_id: str, job_id: str, attempt_id: str, action_id: str,
            effect: EffectSpec | str, *, retry_of: str | None = None) -> EffectPlan:
        """Persist an immutable input digest and semantic idempotency key before claiming."""
        run_identity = _nonempty(run_id, "run_id")
        job_identity = _nonempty(job_id, "job_id")
        attempt_identity = _nonempty(attempt_id, "attempt_id")
        action_identity = _nonempty(action_id, "action_id")
        kind, spec, target, expected = self._normalize_effect(
            effect, action_id=action_identity)
        if kind is EffectKind.RUNNER_EXECUTION:
            self._validate_runner_retry_lineage(
                run_id=run_identity, job_id=job_identity,
                invocation_digest=str(spec["invocation_digest"]),
                retry_of=retry_of,
            )
        if retry_of is not None:
            self._validate_retry_identity(
                retry_of, run_id=run_identity, job_id=job_identity,
                attempt_id=attempt_identity, action_id=action_identity)
        try:
            existing = self._store.get_entity(EntityKind.ACTION, action_identity)
        except RecordNotFoundError:
            existing = None
        if existing is not None:
            raise EffectStateRefusal(action_identity, "plan", existing.state)

        input_digest = _payload_digest({"kind": kind.value, "spec": spec})
        key_basis = {
            "action_id": action_identity,
            "kind": kind.value,
            "input_digest": input_digest,
            "target": target,
            "expected": expected,
        }
        idempotency_key = _payload_digest(key_basis)
        envelope = {
            "schema": _PLAN_SCHEMA,
            "run_id": run_identity,
            "job_id": job_identity,
            "attempt_id": attempt_identity,
            "action_id": action_identity,
            "kind": kind.value,
            "spec": spec,
            "input_digest": input_digest,
            "idempotency_basis": key_basis,
            "idempotency_key": idempotency_key,
            "retry_of": retry_of,
        }
        stored = self._artifacts.write(_canonical_bytes(envelope))
        reference = ArtifactReference(
            reference_id=f"effect-plan:{action_identity}",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=stored.digest,
            size=stored.size,
        )
        references = [reference]
        runner_lineage_key = None
        if kind is EffectKind.RUNNER_EXECUTION:
            runner_lineage_key = self._runner_lineage_key(
                run_identity, job_identity, str(spec["invocation_digest"]))
            references.append(ArtifactReference(
                reference_id=(
                    f"runner-invocation:{runner_lineage_key}:{action_identity}"),
                kind=ArtifactReferenceKind.EVIDENCE,
                digest=stored.digest,
                size=stored.size,
            ))
        try:
            self._store._create_planned_effect_action(  # noqa: SLF001 - D9 composition surface
                run_identity, job_identity, attempt_identity, action_identity,
                evidence_digest=stored.digest,
                artifact_references=tuple(references),
                runner_lineage_key=runner_lineage_key,
                runner_retry_of=retry_of if runner_lineage_key is not None else None,
            )
        except RunnerInvocationConflict as error:
            raise EffectRetryRefused(error.action_id, error.detail) from error
        return self._load_plan(action_identity)

    def plan_retry_effect(
            self, retry_of: str, *, run_id: str, job_id: str, attempt_id: str,
            action_id: str, effect: EffectSpec | str) -> EffectPlan:
        return self.plan_effect(
            run_id, job_id, attempt_id, action_id, effect, retry_of=retry_of)

    def _load_plan(self, action_id: str) -> EffectPlan:
        identity = _nonempty(action_id, "action_id")
        action = self._store.get_entity(EntityKind.ACTION, identity)
        reference_id = f"effect-plan:{identity}"
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload = self._artifacts.read_reference(reference)
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise InvalidEffectPlan(
                identity, f"cannot read immutable plan evidence: {error}") from error
        try:
            envelope = json.loads(payload.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("plan envelope is not an object")
            kind = EffectKind(envelope["kind"])
            spec = envelope["spec"]
            if not isinstance(spec, dict):
                raise ValueError("plan spec is not an object")
            if envelope.get("schema") != _PLAN_SCHEMA:
                raise ValueError("plan schema is unsupported")
            fields = {
                "run_id": action.run_id,
                "job_id": action.parent_job_id,
                "attempt_id": action.parent_attempt_id,
                "action_id": action.entity_id,
            }
            if any(envelope.get(label) != value for label, value in fields.items()):
                raise ValueError("plan identity does not match the action record")
            typed = self._effect_from_stored(kind, spec, identity)
            normalized_kind, normalized_spec, target, expected = self._normalize_effect(
                typed, action_id=identity, stored=True)
            if normalized_kind is not kind or normalized_spec != spec:
                raise ValueError("stored effect spec is not canonical")
            input_digest = _payload_digest({"kind": kind.value, "spec": spec})
            key_basis = {
                "action_id": identity, "kind": kind.value,
                "input_digest": input_digest, "target": target, "expected": expected,
            }
            idempotency_key = _payload_digest(key_basis)
            if (envelope.get("input_digest") != input_digest
                    or envelope.get("idempotency_basis") != key_basis
                    or envelope.get("idempotency_key") != idempotency_key):
                raise ValueError("plan digest or idempotency key does not rederive")
            retry_of = envelope.get("retry_of")
            if retry_of is not None and (not isinstance(retry_of, str) or not retry_of):
                raise ValueError("retry_of is malformed")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidEffectPlan(identity, str(error)) from error

        with self._store._connection_lock:  # noqa: SLF001 - verify reference attribution
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT t.next_state, t.reason, t.evidence_digest, a.digest "
                "FROM artifacts a JOIN transitions t ON t.transition_id = a.transition_id "
                "WHERE a.reference_id = ? AND a.entity_kind = ? AND a.entity_id = ?",
                (reference_id, EntityKind.ACTION.value, identity),
            ).fetchone()
        if (reference.kind is not ArtifactReferenceKind.EVIDENCE
                or row is None or row["next_state"] != "planned"
                or row["reason"] != TransitionReason.PLANNED.value
                or row["evidence_digest"] != reference.digest
                or row["digest"] != reference.digest):
            raise InvalidEffectPlan(
                identity, "plan evidence is not bound to the planned transition")
        if kind is EffectKind.RUNNER_EXECUTION:
            lineage_key = self._runner_lineage_key(
                action.run_id, action.parent_job_id or "",
                str(normalized_spec["invocation_digest"]))
            lineage_id = f"runner-invocation:{lineage_key}:{identity}"
            try:
                lineage_reference = self._store.get_artifact_reference(lineage_id)
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise InvalidEffectPlan(
                    identity, f"runner lineage reservation is unavailable: {error}") from error
            if (lineage_reference.kind is not ArtifactReferenceKind.EVIDENCE
                    or lineage_reference.digest != reference.digest
                    or lineage_reference.size != reference.size):
                raise InvalidEffectPlan(
                    identity, "runner lineage reservation differs from plan evidence")
        return EffectPlan(
            action.run_id, action.parent_job_id or "", action.parent_attempt_id or "",
            identity, kind, normalized_spec, input_digest, idempotency_key,
            reference.digest, retry_of,
        )

    @staticmethod
    def _same_plan(expected: EffectPlan, actual: EffectPlan) -> bool:
        return (
            expected.action_id == actual.action_id
            and expected.plan_digest == actual.plan_digest
            and expected.input_digest == actual.input_digest
            and expected.idempotency_key == actual.idempotency_key
        )

    def _transition(
            self, principal: LeasePrincipal, guard: Callable,
            *, next_state: str, reason: TransitionReason,
            evidence_digest: str | None = None,
            references: tuple[ArtifactReference, ...] = ()) -> tuple[LeasePrincipal, EntityRecord]:
        updated = guard(
            principal,
            lambda: self._store._record_guarded_action_transition(  # noqa: SLF001
                principal.action_id,
                expected_version=principal.entity_version,
                owner_token=principal.owner_token,
                fencing_epoch=principal.fencing_epoch,
                next_state=next_state,
                reason=reason,
                evidence_digest=evidence_digest,
                artifact_references=references,
            ),
        )
        return replace(principal, entity_version=updated.version), updated

    def claim_effect(self, plan: EffectPlan, *, ttl_seconds: float) -> ClaimedEffect:
        """Acquire a fenced principal and durably record the claimed lifecycle stage."""
        if not isinstance(plan, EffectPlan):
            raise TypeError("plan must be an EffectPlan")
        durable = self._load_plan(plan.action_id)
        if not self._same_plan(plan, durable):
            raise InvalidEffectPlan(plan.action_id, "supplied plan differs from durable evidence")
        action = self._store.get_entity(EntityKind.ACTION, plan.action_id)
        if action.state != "planned":
            raise EffectStateRefusal(plan.action_id, "claim", action.state)
        principal = self._leases.claim(
            action.entity_id, expected_entity_version=action.version,
            ttl_seconds=ttl_seconds)
        principal, _ = self._transition(
            principal, self._leases.guard_effect_start,
            next_state="claimed", reason=TransitionReason.CLAIMED,
            evidence_digest=durable.input_digest,
        )
        return ClaimedEffect(durable, principal)

    def _make_intent(
            self, plan: EffectPlan,
            principal: LeasePrincipal) -> tuple[dict[str, object], ArtifactReference]:
        launch_token = (
            secrets.token_urlsafe(32)
            if plan.kind is EffectKind.RUNNER_EXECUTION else None)
        payload = {
            "schema": _INTENT_SCHEMA,
            "run_id": plan.run_id,
            "job_id": plan.job_id,
            "action_id": plan.action_id,
            "kind": plan.kind.value,
            "input_digest": plan.input_digest,
            "idempotency_key": plan.idempotency_key,
            "fencing_epoch": principal.fencing_epoch,
            "launch_token": launch_token,
        }
        stored = self._artifacts.write(_canonical_bytes(payload))
        reference = ArtifactReference(
            reference_id=f"effect-intent:{plan.action_id}",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=stored.digest,
            size=stored.size,
        )
        return payload, reference

    def _load_intent(self, plan: EffectPlan) -> dict[str, object]:
        reference_id = f"effect-intent:{plan.action_id}"
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload = json.loads(self._artifacts.read_reference(reference).decode("utf-8"))
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise InvalidEffectPlan(
                plan.action_id, f"cannot read effect intent: {error}") from error
        expected = {
            "schema": _INTENT_SCHEMA,
            "run_id": plan.run_id,
            "job_id": plan.job_id,
            "action_id": plan.action_id,
            "kind": plan.kind.value,
            "input_digest": plan.input_digest,
            "idempotency_key": plan.idempotency_key,
        }
        if (not isinstance(payload, dict)
                or any(payload.get(key) != value for key, value in expected.items())
                or isinstance(payload.get("fencing_epoch"), bool)
                or not isinstance(payload.get("fencing_epoch"), int)
                or payload["fencing_epoch"] < 1):
            raise InvalidEffectPlan(plan.action_id, "effect intent is malformed or misattributed")
        token = payload.get("launch_token")
        if plan.kind is EffectKind.RUNNER_EXECUTION:
            if not isinstance(token, str) or not token:
                raise InvalidEffectPlan(plan.action_id, "runner intent has no launch token")
        elif token is not None:
            raise InvalidEffectPlan(plan.action_id, "non-runner intent has a launch token")
        normalized = {
            **expected,
            "fencing_epoch": payload["fencing_epoch"],
            "launch_token": token,
        }
        if payload != normalized:
            raise InvalidEffectPlan(
                plan.action_id, "effect intent contains unsupported or missing fields")
        with self._store._connection_lock:  # noqa: SLF001 - verify WAI attribution
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT t.next_state, t.reason, t.evidence_digest, a.digest "
                "FROM artifacts a JOIN transitions t ON t.transition_id = a.transition_id "
                "WHERE a.reference_id = ? AND a.entity_kind = ? AND a.entity_id = ?",
                (reference_id, EntityKind.ACTION.value, plan.action_id),
            ).fetchone()
        if (reference.kind is not ArtifactReferenceKind.EVIDENCE
                or row is None or row["next_state"] != "effect"
                or row["reason"] != TransitionReason.PROCESS_STARTED.value
                or row["evidence_digest"] != reference.digest
                or row["digest"] != reference.digest):
            raise InvalidEffectPlan(
                plan.action_id, "effect intent is not bound to the effect transition")
        return payload

    @staticmethod
    def _observation(
            plan: EffectPlan, disposition: ObservationDisposition,
            evidence: Mapping[str, object], reason: str | None = None) -> EffectObservation:
        observed_digest = None
        if disposition is ObservationDisposition.DESIRED:
            observed_digest = _payload_digest({
                "action_id": plan.action_id,
                "kind": plan.kind.value,
                "evidence": evidence,
            })
        return EffectObservation(disposition, dict(evidence), observed_digest, reason)

    @staticmethod
    def _read_ref(repository: Path, ref: str) -> tuple[str, str | None, str | None]:
        rc, git_dir, error = _git_rc(repository, "rev-parse", "--git-dir")
        if rc != 0 or error or not git_dir:
            detail = error or (
                f"git rev-parse exited {rc}"
                if rc != 0 else "git repository authority output is empty")
            return "unknown", None, detail
        rc, output, error = _git_rc(
            repository, "for-each-ref", "--format=%(refname)%00%(objectname)", ref)
        if rc != 0 or error:
            return "unknown", None, error or f"git for-each-ref exited {rc}"
        if not output:
            return "absent", None, None
        rows = output.splitlines()
        fields = rows[0].split("\0") if len(rows) == 1 else []
        if len(fields) != 2 or fields[0] != ref or _OID_PATTERN.fullmatch(fields[1]) is None:
            return "unknown", None, "exact ref observation returned malformed or ambiguous output"
        return "present", fields[1], None

    def _observe_git_ref(self, plan: EffectPlan) -> EffectObservation:
        repository = Path(plan.spec["repository"])
        ref = str(plan.spec["ref"])
        expected = plan.spec["expected_old_oid"]
        desired = str(plan.spec["desired_oid"])
        status, oid, reason = self._read_ref(repository, ref)
        evidence = {"repository": str(repository), "ref": ref, "oid": oid}
        if status == "unknown":
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence, reason)
        if status == "absent":
            if expected is None:
                return self._observation(plan, ObservationDisposition.ABSENT, evidence)
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "ref is absent instead of the expected old OID")
        if oid == desired:
            return self._observation(plan, ObservationDisposition.DESIRED, evidence)
        if oid == expected:
            return self._observation(plan, ObservationDisposition.ABSENT, evidence)
        return self._observation(
            plan, ObservationDisposition.CONFLICT, evidence,
            "ref contains neither expected nor desired OID")

    @staticmethod
    def _worktree_records(output: str) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for field in output.split("\0"):
            if not field:
                if current:
                    records.append(current)
                    current = {}
                continue
            key, separator, value = field.partition(" ")
            if key in current:
                return []
            current[key] = value if separator else ""
        if current:
            records.append(current)
        return records

    def _observe_worktree(
            self, plan: EffectPlan,
            intent: Mapping[str, object] | None) -> EffectObservation:
        repository = Path(plan.spec["repository"])
        target = Path(plan.spec["path"])
        dedicated_ref = str(plan.spec["dedicated_ref"])
        expected_head = str(plan.spec["expected_head_oid"])
        ref_status, ref_oid, ref_reason = self._read_ref(repository, dedicated_ref)
        evidence: dict[str, object] = {
            "repository": str(repository), "path": str(target),
            "ref": dedicated_ref, "ref_oid": ref_oid,
        }
        if ref_status == "unknown":
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence, ref_reason)
        if ref_status == "present" and ref_oid != expected_head:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "dedicated worktree ref has the wrong HEAD")
        rc, output, error = _git_rc(
            repository, "worktree", "list", "--porcelain", "-z")
        if rc != 0 or error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                error or f"git worktree list exited {rc}")
        records = self._worktree_records(output)
        if not output.endswith("\0\0") or not records:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "git worktree list output is malformed")
        for record in records:
            if (not record.get("worktree")
                    or _OID_PATTERN.fullmatch(record.get("HEAD", "")) is None):
                return self._observation(
                    plan, ObservationDisposition.UNKNOWN, evidence,
                    "git worktree list record lacks a valid path or HEAD")
        matches = []
        ref_registrations = []
        for record in records:
            try:
                observed_path = Path(record["worktree"]).resolve(strict=False)
            except (OSError, RuntimeError) as error:
                return self._observation(
                    plan, ObservationDisposition.UNKNOWN, evidence,
                    f"cannot resolve registered worktree path: {error}")
            if observed_path == target:
                matches.append(record)
            if record.get("branch") == dedicated_ref:
                ref_registrations.append(record)
        if any(record not in matches for record in ref_registrations):
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "dedicated ref is registered at a different worktree path")
        if not matches:
            try:
                target.lstat()
            except FileNotFoundError:
                if ref_status == "present" and intent is not None:
                    return self._observation(
                        plan, ObservationDisposition.CONFLICT, evidence,
                        "effect intent has only the dedicated ref without its worktree")
                return self._observation(plan, ObservationDisposition.ABSENT, evidence)
            except OSError as error:
                return self._observation(
                    plan, ObservationDisposition.UNKNOWN, evidence,
                    f"cannot inspect worktree path: {error}")
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "worktree path exists without matching Git registration")
        if len(matches) != 1:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "worktree path has ambiguous registrations")
        if ref_status != "present":
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "worktree is registered but its dedicated ref is absent")
        record = matches[0]
        evidence.update({"registered_head": record.get("HEAD"), "branch": record.get("branch")})
        if record.get("HEAD") != expected_head or record.get("branch") != dedicated_ref:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "worktree registration does not match its dedicated ref and HEAD")
        head_rc, head, head_error = _git_rc(target, "rev-parse", "--verify", "HEAD")
        ref_rc, checked_out_ref, checked_error = _git_rc(
            target, "symbolic-ref", "--quiet", "HEAD")
        if head_rc != 0 or ref_rc != 0 or head_error or checked_error:
            reason = head_error or checked_error or "worktree HEAD cannot be observed"
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence, reason)
        if (_OID_PATTERN.fullmatch(head) is None
                or not checked_out_ref.startswith("refs/")):
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "worktree HEAD or symbolic ref observation is malformed")
        evidence.update({"head": head, "checked_out_ref": checked_out_ref})
        if head != expected_head or checked_out_ref != dedicated_ref:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "registered worktree HEAD or ref differs from the plan")
        return self._observation(plan, ObservationDisposition.DESIRED, evidence)

    def _artifact_content(self, plan: EffectPlan) -> bytes:
        encoded = plan.spec["content_base64"]
        if not isinstance(encoded, str):
            raise InvalidEffectPlan(plan.action_id, "artifact content_base64 is malformed")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise InvalidEffectPlan(plan.action_id, "artifact content_base64 is invalid") from error
        digest = _bytes_digest(content)
        if (plan.spec.get("content_digest") != digest
                or plan.spec.get("size") != len(content)):
            raise InvalidEffectPlan(
                plan.action_id, "artifact content digest or size does not rederive")
        return content

    def _observe_artifact(self, plan: EffectPlan) -> EffectObservation:
        content = self._artifact_content(plan)
        digest = _bytes_digest(content)
        path = self._artifacts.path_for(digest)
        evidence: dict[str, object] = {
            "path": str(path), "expected_digest": digest, "size": len(content),
        }
        try:
            info = path.lstat()
        except FileNotFoundError:
            return self._observation(plan, ObservationDisposition.ABSENT, evidence)
        except OSError as error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                f"cannot inspect artifact path: {error}")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "artifact target exists but is not a regular file")
        try:
            observed = path.read_bytes()
        except OSError as error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                f"artifact bytes are unreadable: {error}")
        actual = _bytes_digest(observed)
        evidence.update({"observed_digest": actual, "observed_size": len(observed)})
        if actual != digest:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "digest path contains different bytes")
        return self._observation(plan, ObservationDisposition.DESIRED, evidence)

    def _observe_runner(
            self, plan: EffectPlan, intent: Mapping[str, object] | None) -> EffectObservation:
        marker_path = Path(plan.spec["completion_marker"])
        evidence: dict[str, object] = {"completion_marker": str(marker_path)}
        try:
            info = marker_path.lstat()
        except FileNotFoundError:
            if intent is None:
                return self._observation(plan, ObservationDisposition.ABSENT, evidence)
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner launch intent exists but no completion marker is observable")
        except OSError as error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                f"cannot inspect runner completion marker: {error}")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner completion marker is not a regular file")
        if intent is None:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner marker exists without a durable launch intent")
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                f"runner completion marker is unreadable: {error}")
        if not isinstance(marker, dict):
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner completion marker is not an object")
        required = {
            "schema": _RUNNER_MARKER_SCHEMA,
            "run_id": plan.run_id,
            "job_id": plan.job_id,
            "action_id": plan.action_id,
            "fencing_epoch": intent.get("fencing_epoch"),
            "launch_token": intent.get("launch_token"),
        }
        if any(marker.get(key) != value for key, value in required.items()):
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner marker identity does not match the launch intent")
        try:
            typed_marker = RunnerCompletionMarker(
                run_id=marker["run_id"], job_id=marker["job_id"],
                action_id=marker["action_id"], fencing_epoch=marker["fencing_epoch"],
                launch_token=marker["launch_token"],
                process_identity=marker["process_identity"],
                started_at=marker["started_at"], finished_at=marker["finished_at"],
                returncode=marker.get("returncode"), signal=marker.get("signal"),
                stdout_artifact_digest=marker["stdout_artifact_digest"],
                stderr_artifact_digest=marker["stderr_artifact_digest"],
            )
            normalized = _marker_payload(typed_marker)
        except (KeyError, TypeError, ValueError) as error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                f"runner completion marker fields are invalid: {error}")
        if marker != normalized:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner completion marker contains unsupported fields")
        if self._runner_identity_verifier is None:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner process identity verifier is unavailable")
        try:
            identity_matches = self._runner_identity_verifier(typed_marker)
        except Exception as error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                f"runner process identity verification failed: {error}")
        if identity_matches is not True:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "runner process identity does not match supervisor authority")
        output_sizes: dict[str, int] = {}
        for stream, digest in (
                ("stdout", typed_marker.stdout_artifact_digest),
                ("stderr", typed_marker.stderr_artifact_digest)):
            try:
                output_sizes[f"{stream}_size"] = len(self._artifacts.read(digest))
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                return self._observation(
                    plan, ObservationDisposition.UNKNOWN, evidence,
                    f"runner {stream} artifact cannot be verified: {error}")
        evidence["marker"] = normalized
        evidence.update(output_sizes)
        return self._observation(plan, ObservationDisposition.DESIRED, evidence)

    @staticmethod
    def _commit_shape(
            repository: Path, commit: str) -> tuple[str, tuple[str, ...] | None, str | None]:
        rc, output, error = _git_rc(
            repository, "show", "-s", "--format=%P%n%T", commit)
        if rc != 0 or error:
            return "unknown", None, error or f"git show exited {rc}"
        lines = output.splitlines()
        if len(lines) != 2 or _OID_PATTERN.fullmatch(lines[1]) is None:
            return "unknown", None, "integration commit observation is malformed"
        parents = tuple(lines[0].split()) if lines[0] else ()
        if any(_OID_PATTERN.fullmatch(parent) is None for parent in parents):
            return "unknown", None, "integration parent observation is malformed"
        return "present", parents, lines[1]

    def _observe_patch(self, plan: EffectPlan) -> EffectObservation:
        repository = Path(plan.spec["repository"])
        ref = str(plan.spec["target_ref"])
        expected_parent = str(plan.spec["expected_parent_oid"])
        expected_tree = str(plan.spec["expected_parent_tree_oid"])
        desired_commit = str(plan.spec["integration_commit_oid"])
        desired_tree = str(plan.spec["integration_tree_oid"])
        status, oid, reason = self._read_ref(repository, ref)
        evidence: dict[str, object] = {
            "repository": str(repository), "ref": ref, "ref_oid": oid,
        }
        if status == "unknown":
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence, reason)
        if status == "absent":
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "integration target ref is absent")
        tree_rc, tree, tree_error = _git_rc(
            repository, "rev-parse", "--verify", f"{expected_parent}^{{tree}}")
        if tree_rc != 0 or tree_error:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                tree_error or f"git rev-parse tree exited {tree_rc}")
        if _OID_PATTERN.fullmatch(tree) is None:
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence,
                "expected parent tree observation is malformed")
        evidence["expected_parent_tree"] = tree
        if tree != expected_tree:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "expected parent tree differs from the plan")
        if oid == expected_parent:
            return self._observation(plan, ObservationDisposition.ABSENT, evidence)
        if oid != desired_commit:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "integration ref contains neither expected parent nor desired commit")
        shape_status, parents, tree_or_reason = self._commit_shape(repository, desired_commit)
        if shape_status == "unknown":
            return self._observation(
                plan, ObservationDisposition.UNKNOWN, evidence, tree_or_reason)
        evidence.update({"parents": list(parents or ()), "tree": tree_or_reason})
        if parents != (expected_parent,) or tree_or_reason != desired_tree:
            return self._observation(
                plan, ObservationDisposition.CONFLICT, evidence,
                "integration commit parent or tree differs from the plan")
        return self._observation(plan, ObservationDisposition.DESIRED, evidence)

    def _observe(
            self, plan: EffectPlan,
            intent: Mapping[str, object] | None = None) -> EffectObservation:
        if plan.kind is EffectKind.GIT_REF:
            return self._observe_git_ref(plan)
        if plan.kind is EffectKind.WORKTREE:
            return self._observe_worktree(plan, intent)
        if plan.kind is EffectKind.ARTIFACT_WRITE:
            return self._observe_artifact(plan)
        if plan.kind is EffectKind.RUNNER_EXECUTION:
            return self._observe_runner(plan, intent)
        if plan.kind is EffectKind.PATCH_INTEGRATION:
            return self._observe_patch(plan)
        raise UnsupportedEffectKind(plan.kind)

    def _execute_driver(
            self, plan: EffectPlan, principal: LeasePrincipal,
            intent: Mapping[str, object]) -> None:
        if plan.kind is EffectKind.GIT_REF:
            expected = plan.spec["expected_old_oid"]
            desired = str(plan.spec["desired_oid"])
            zero = "0" * len(desired)
            rc, _, error = _git_rc(
                Path(plan.spec["repository"]), "update-ref", str(plan.spec["ref"]),
                desired, zero if expected is None else str(expected))
            if rc != 0:
                raise EffectExecutionFailed(
                    plan.action_id, error or f"git update-ref exited {rc}")
            return
        if plan.kind is EffectKind.WORKTREE:
            target = Path(plan.spec["path"])
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise EffectExecutionFailed(
                    plan.action_id, f"cannot create worktree parent: {error}") from error
            repository = Path(plan.spec["repository"])
            dedicated_ref = str(plan.spec["dedicated_ref"])
            expected_head = str(plan.spec["expected_head_oid"])
            ref_status, ref_oid, ref_error = self._read_ref(
                repository, dedicated_ref)
            if ref_status == "unknown":
                raise EffectExecutionFailed(
                    plan.action_id,
                    ref_error or "dedicated worktree ref cannot be observed")
            if ref_status == "present" and ref_oid != expected_head:
                raise EffectExecutionFailed(
                    plan.action_id, "dedicated worktree ref changed before creation")
            branch = dedicated_ref[len("refs/heads/"):]
            args = (
                ("worktree", "add", "--quiet", "-b", branch,
                 str(target), expected_head)
                if ref_status == "absent" else
                ("worktree", "add", "--quiet", str(target), branch)
            )
            rc, _, error = _git_rc(repository, *args)
            if rc != 0:
                raise EffectExecutionFailed(
                    plan.action_id, error or f"git worktree add exited {rc}")
            return
        if plan.kind is EffectKind.ARTIFACT_WRITE:
            stored = self._artifacts.write(self._artifact_content(plan))
            if stored.digest != plan.spec["content_digest"]:
                raise EffectExecutionFailed(
                    plan.action_id, "artifact publisher returned a different digest")
            return
        if plan.kind is EffectKind.RUNNER_EXECUTION:
            if self._runner_executor is None:
                raise EffectExecutionFailed(plan.action_id, "runner executor is unavailable")
            launch_token = intent.get("launch_token")
            if not isinstance(launch_token, str) or not launch_token:
                raise InvalidEffectPlan(plan.action_id, "runner intent launch token is invalid")
            self._runner_executor(RunnerLaunchIntent(
                run_id=plan.run_id,
                job_id=plan.job_id,
                action_id=plan.action_id,
                owner_token=principal.owner_token,
                fencing_epoch=principal.fencing_epoch,
                invocation_digest=str(plan.spec["invocation_digest"]),
                launch_token=launch_token,
                completion_marker_path=Path(plan.spec["completion_marker"]),
            ))
            return
        if plan.kind is EffectKind.PATCH_INTEGRATION:
            shape, parents, tree = self._commit_shape(
                Path(plan.spec["repository"]), str(plan.spec["integration_commit_oid"]))
            if (shape != "present"
                    or parents != (str(plan.spec["expected_parent_oid"]),)
                    or tree != plan.spec["integration_tree_oid"]):
                raise EffectExecutionFailed(
                    plan.action_id, "integration commit parent/tree precondition failed")
            rc, _, error = _git_rc(
                Path(plan.spec["repository"]), "update-ref", str(plan.spec["target_ref"]),
                str(plan.spec["integration_commit_oid"]),
                str(plan.spec["expected_parent_oid"]))
            if rc != 0:
                raise EffectExecutionFailed(
                    plan.action_id, error or f"integration update-ref exited {rc}")
            return
        raise UnsupportedEffectKind(plan.kind)

    @staticmethod
    def _result_from_observation(
            plan: EffectPlan, observation: EffectObservation) -> EffectResult:
        if observation.disposition is ObservationDisposition.UNKNOWN:
            state = EffectResultState.UNKNOWN_EFFECT
        elif observation.disposition is ObservationDisposition.IN_FLIGHT:
            state = EffectResultState.IN_FLIGHT
        elif observation.disposition is ObservationDisposition.CONFLICT:
            state = EffectResultState.CONFLICT
        else:
            raise ValueError("observation does not represent a waiting result")
        return EffectResult(plan.action_id, state, reason=observation.reason)

    def _commit_observation(
            self, plan: EffectPlan, principal: LeasePrincipal,
            observation: EffectObservation) -> EffectResult:
        if (observation.disposition is not ObservationDisposition.DESIRED
                or observation.observed_digest is None):
            raise ValueError("only a desired observation can be committed")
        self._leases.guard_submit(principal, lambda: None)
        receipt_payload = self._observation_receipt_payload(plan, observation)
        stored = self._artifacts.write(_canonical_bytes(receipt_payload))
        digest_suffix = observation.observed_digest.split(":", 1)[1]
        reference = ArtifactReference(
            reference_id=f"effect-observation:{plan.action_id}:{digest_suffix}",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=stored.digest,
            size=stored.size,
        )
        principal, _ = self._transition(
            principal, self._leases.guard_submit,
            next_state="observed", reason=TransitionReason.EFFECT_OBSERVED,
            evidence_digest=observation.observed_digest,
            references=(reference,),
        )
        self._effect_fault_point("after-observed", plan)
        receipt_error = self._observation_receipt_error(plan, observation)
        if receipt_error is not None:
            return EffectResult(
                plan.action_id, EffectResultState.UNKNOWN_EFFECT,
                principal=principal, observed_digest=observation.observed_digest,
                reason=receipt_error,
            )
        principal, _ = self._transition(
            principal, self._leases.guard_completion,
            next_state="completed", reason=TransitionReason.COMPLETED,
            evidence_digest=observation.observed_digest,
        )
        return EffectResult(
            plan.action_id, EffectResultState.COMPLETED, principal,
            observation.observed_digest)

    @staticmethod
    def _observation_receipt_payload(
            plan: EffectPlan, observation: EffectObservation) -> dict[str, object]:
        return {
            "schema": _OBSERVATION_SCHEMA,
            "run_id": plan.run_id,
            "job_id": plan.job_id,
            "action_id": plan.action_id,
            "kind": plan.kind.value,
            "observed_digest": observation.observed_digest,
            "evidence": observation.evidence,
        }

    def _observation_receipt_error(
            self, plan: EffectPlan, observation: EffectObservation) -> str | None:
        if observation.observed_digest is None:
            return "fresh observation has no digest"
        digest_suffix = observation.observed_digest.split(":", 1)[1]
        reference_id = f"effect-observation:{plan.action_id}:{digest_suffix}"
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload = self._artifacts.read_reference(reference)
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return f"stored observation receipt cannot be verified: {error}"
        expected_payload = _canonical_bytes(
            self._observation_receipt_payload(plan, observation))
        if reference.kind is not ArtifactReferenceKind.EVIDENCE:
            return "stored observation receipt has the wrong reference kind"
        if payload != expected_payload:
            return "stored observation receipt differs from fresh authority evidence"
        with self._store._connection_lock:  # noqa: SLF001 - audit receipt binding
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT a.run_id, a.entity_kind, a.entity_id, a.entity_version, "
                "a.digest, a.size, t.next_state, t.reason, t.evidence_digest, "
                "t.entity_version AS transition_version FROM artifacts a "
                "JOIN transitions t ON t.transition_id = a.transition_id "
                "WHERE a.reference_id = ?",
                (reference_id,),
            ).fetchone()
        if (row is None
                or row["run_id"] != plan.run_id
                or row["entity_kind"] != EntityKind.ACTION.value
                or row["entity_id"] != plan.action_id
                or row["entity_version"] != row["transition_version"]
                or row["next_state"] != "observed"
                or row["reason"] != TransitionReason.EFFECT_OBSERVED.value
                or row["evidence_digest"] != observation.observed_digest
                or row["digest"] != reference.digest
                or row["size"] != reference.size):
            return "stored observation receipt is not bound to the observed transition"
        return None

    def _complete_observed(
            self, plan: EffectPlan, principal: LeasePrincipal,
            observation: EffectObservation) -> EffectResult:
        if (observation.disposition is not ObservationDisposition.DESIRED
                or observation.observed_digest is None):
            return self._result_from_observation(plan, observation)
        with self._store._connection_lock:  # noqa: SLF001 - read latest audited observation
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT evidence_digest FROM transitions WHERE entity_kind = ? AND entity_id = ? "
                "AND next_state = 'observed' ORDER BY entity_version DESC LIMIT 1",
                (EntityKind.ACTION.value, plan.action_id),
            ).fetchone()
        if row is None or row["evidence_digest"] != observation.observed_digest:
            return EffectResult(
                plan.action_id, EffectResultState.CONFLICT,
                reason="fresh observation differs from the stored observed digest")
        receipt_error = self._observation_receipt_error(plan, observation)
        if receipt_error is not None:
            return EffectResult(
                plan.action_id, EffectResultState.UNKNOWN_EFFECT,
                principal=principal, observed_digest=observation.observed_digest,
                reason=receipt_error,
            )
        principal, _ = self._transition(
            principal, self._leases.guard_completion,
            next_state="completed", reason=TransitionReason.COMPLETED,
            evidence_digest=observation.observed_digest,
        )
        return EffectResult(
            plan.action_id, EffectResultState.COMPLETED, principal,
            observation.observed_digest)

    def execute_effect(self, claimed: ClaimedEffect) -> EffectResult:
        """Write intent, perform at most one external effect, reobserve, and commit."""
        if not isinstance(claimed, ClaimedEffect):
            raise TypeError("claimed must be a ClaimedEffect")
        plan = self._load_plan(claimed.plan.action_id)
        if not self._same_plan(claimed.plan, plan):
            raise InvalidEffectPlan(plan.action_id, "claimed plan differs from durable evidence")
        action = self._store.get_entity(EntityKind.ACTION, plan.action_id)
        if action.state in {"effect", "observed", "completed"}:
            raise EffectAlreadyExecuted(plan.action_id, action.state)
        if action.state != "claimed":
            raise EffectStateRefusal(plan.action_id, "execute", action.state)
        if plan.kind is EffectKind.RUNNER_EXECUTION:
            if self._runner_executor is None:
                raise EffectExecutionFailed(plan.action_id, "runner executor is unavailable")
            if self._runner_identity_verifier is None:
                raise EffectExecutionFailed(
                    plan.action_id, "runner process identity verifier is unavailable")

        self._leases.guard_effect_start(claimed.principal, lambda: None)
        before = self._observe(plan)
        if before.disposition in {
                ObservationDisposition.UNKNOWN, ObservationDisposition.IN_FLIGHT,
                ObservationDisposition.CONFLICT}:
            return self._result_from_observation(plan, before)
        self._effect_fault_point("before-effect-intent", plan)
        self._leases.guard_effect_start(claimed.principal, lambda: None)
        intent, reference = self._make_intent(plan, claimed.principal)
        principal, _ = self._transition(
            claimed.principal, self._leases.guard_effect_start,
            next_state="effect", reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=reference.digest, references=(reference,),
        )
        self._effect_fault_point("after-effect-intent", plan)

        execution_error: Exception | None = None
        if before.disposition is ObservationDisposition.ABSENT:
            self._leases.guard_effect_start(principal, lambda: None)
            try:
                self._execute_driver(plan, principal, intent)
            except Exception as error:
                execution_error = error
        self._effect_fault_point("after-external-effect", plan)
        observation = self._observe(plan, intent)
        if observation.disposition is ObservationDisposition.DESIRED:
            return self._commit_observation(plan, principal, observation)
        if observation.disposition is ObservationDisposition.ABSENT:
            detail = "external authority still proves the effect absent"
            if execution_error is not None:
                detail = f"{execution_error}; {detail}"
            raise EffectExecutionFailed(plan.action_id, detail) from execution_error
        return self._result_from_observation(plan, observation)

    def _current_principal(self, action: EntityRecord) -> LeasePrincipal:
        with self._store._connection_lock:  # noqa: SLF001 - package-internal resume boundary
            rows = self._store._connection.execute(  # noqa: SLF001
                "SELECT run_id, entity_version, owner_token, fencing_epoch FROM leases "
                "WHERE lease_id = ? OR (entity_kind = ? AND entity_id = ?)",
                (action.entity_id, EntityKind.ACTION.value, action.entity_id),
            ).fetchall()
        if len(rows) != 1:
            raise InvalidEffectPlan(action.entity_id, "current lease is missing or ambiguous")
        row = rows[0]
        owner = row["owner_token"]
        epoch = row["fencing_epoch"]
        version = row["entity_version"]
        if (row["run_id"] != action.run_id or not isinstance(owner, str) or not owner
                or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1
                or isinstance(version, bool) or not isinstance(version, int)
                or version != action.version):
            raise InvalidEffectPlan(action.entity_id, "current lease tuple is incoherent")
        return LeasePrincipal(action.run_id, action.entity_id, owner, epoch, version, 0.0)

    def _maybe_current_principal(self, action: EntityRecord) -> LeasePrincipal | None:
        with self._store._connection_lock:  # noqa: SLF001
            row = self._store._connection.execute(  # noqa: SLF001
                "SELECT owner_token FROM leases WHERE lease_id = ?",
                (action.entity_id,),
            ).fetchone()
        if row is None or row["owner_token"] is None:
            return None
        return self._current_principal(action)

    @staticmethod
    def _quiescent(
            plan: EffectPlan, probe: QuiescenceProbe | None) -> tuple[bool, str | None]:
        if probe is None:
            return False, "positive quiescence observation is unavailable"
        try:
            observed = probe(plan)
        except Exception as error:
            return False, f"quiescence observation failed: {error}"
        if observed is not True:
            return False, "positive quiescence was not established"
        return True, None

    def _execute_existing_intent(
            self, plan: EffectPlan, principal: LeasePrincipal,
            intent: Mapping[str, object]) -> EffectResult:
        if plan.kind is EffectKind.RUNNER_EXECUTION:
            return EffectResult(
                plan.action_id, EffectResultState.UNKNOWN_EFFECT,
                reason="runner launch intent forbids same-action relaunch")
        self._leases.guard_effect_start(principal, lambda: None)
        execution_error: Exception | None = None
        try:
            self._execute_driver(plan, principal, intent)
        except Exception as error:
            execution_error = error
        self._effect_fault_point("after-external-effect", plan)
        observation = self._observe(plan, intent)
        if observation.disposition is ObservationDisposition.DESIRED:
            return self._commit_observation(plan, principal, observation)
        if observation.disposition is ObservationDisposition.ABSENT:
            detail = "reconciled executor left the effect positively absent"
            if execution_error is not None:
                detail = f"{execution_error}; {detail}"
            raise EffectExecutionFailed(plan.action_id, detail) from execution_error
        return self._result_from_observation(plan, observation)

    def _reconcile_one(
            self, action_id: str, *, ttl_seconds: float,
            quiescence_probe: QuiescenceProbe | None) -> EffectResult:
        plan = self._load_plan(action_id)
        action = self._store.get_entity(EntityKind.ACTION, action_id)
        if action.state == "completed":
            return EffectResult(action_id, EffectResultState.NOOP)
        if action.state == "planned":
            principal = self._maybe_current_principal(action)
            if principal is None:
                claimed = self.claim_effect(plan, ttl_seconds=ttl_seconds)
            else:
                principal, _ = self._transition(
                    principal, self._leases.guard_effect_start,
                    next_state="claimed", reason=TransitionReason.CLAIMED,
                    evidence_digest=plan.input_digest,
                )
                claimed = ClaimedEffect(plan, principal)
            return self.execute_effect(claimed)
        if action.state == "claimed":
            principal = self._current_principal(action)
            observation = self._observe(plan)
            if observation.disposition in {
                    ObservationDisposition.UNKNOWN, ObservationDisposition.IN_FLIGHT,
                    ObservationDisposition.CONFLICT}:
                return self._result_from_observation(plan, observation)
            quiescent, reason = self._quiescent(plan, quiescence_probe)
            if not quiescent:
                return EffectResult(
                    action_id, EffectResultState.UNKNOWN_EFFECT, reason=reason)
            if observation.disposition is ObservationDisposition.ABSENT:
                principal = self._leases.reclaim(
                    principal, quiescence_probe=lambda: True,
                    effect_absence_probe=lambda: (
                        self._observe(plan).disposition is ObservationDisposition.ABSENT),
                    ttl_seconds=ttl_seconds,
                )
            return self.execute_effect(ClaimedEffect(plan, principal))
        if action.state == "effect":
            principal = self._current_principal(action)
            intent = self._load_intent(plan)
            observation = self._observe(plan, intent)
            if observation.disposition is ObservationDisposition.DESIRED:
                return self._commit_observation(plan, principal, observation)
            if observation.disposition in {
                    ObservationDisposition.UNKNOWN, ObservationDisposition.IN_FLIGHT,
                    ObservationDisposition.CONFLICT}:
                return self._result_from_observation(plan, observation)
            if plan.kind is EffectKind.RUNNER_EXECUTION:
                return EffectResult(
                    action_id, EffectResultState.UNKNOWN_EFFECT,
                    reason="runner intent exists without positive completion evidence")
            quiescent, reason = self._quiescent(plan, quiescence_probe)
            if not quiescent:
                return EffectResult(
                    action_id, EffectResultState.UNKNOWN_EFFECT, reason=reason)
            principal = self._leases.reclaim(
                principal, quiescence_probe=lambda: True,
                effect_absence_probe=lambda: (
                    self._observe(plan, intent).disposition is ObservationDisposition.ABSENT),
                ttl_seconds=ttl_seconds,
            )
            return self._execute_existing_intent(plan, principal, intent)
        if action.state == "observed":
            principal = self._current_principal(action)
            intent = self._load_intent(plan)
            observation = self._observe(plan, intent)
            if observation.disposition is ObservationDisposition.ABSENT:
                return EffectResult(
                    action_id, EffectResultState.CONFLICT,
                    reason="a stored observation is now positively absent")
            if observation.disposition is not ObservationDisposition.DESIRED:
                return self._result_from_observation(plan, observation)
            return self._complete_observed(plan, principal, observation)
        raise EffectStateRefusal(action_id, "reconcile", action.state)

    def reconcile_actions(
            self, action_ids: Iterable[str], *, ttl_seconds: float = 30,
            quiescence_probe: QuiescenceProbe | None = None) -> tuple[EffectResult, ...]:
        """Reobserve authority and converge actions without replaying uncertain effects."""
        identities = tuple(_nonempty(action_id, "action_id") for action_id in action_ids)
        return tuple(
            self._reconcile_one(
                action_id, ttl_seconds=ttl_seconds,
                quiescence_probe=quiescence_probe)
            for action_id in identities
        )

    def inspect_effect(self, action_id: str) -> EffectResult:
        """Return an honest read-only effect classification without reconciling or writing."""
        plan = self._load_plan(action_id)
        action = self._store.get_entity(EntityKind.ACTION, action_id)
        if action.state == "completed":
            return EffectResult(action_id, EffectResultState.NOOP)
        intent = self._load_intent(plan) if action.state in {"effect", "observed"} else None
        observation = self._observe(plan, intent)
        if (plan.kind is EffectKind.RUNNER_EXECUTION
                and action.state in {"effect", "observed"}
                and observation.disposition is ObservationDisposition.DESIRED):
            return EffectResult(
                action_id, EffectResultState.EXITED_UNRECONCILED,
                observed_digest=observation.observed_digest)
        if observation.disposition is ObservationDisposition.DESIRED:
            return EffectResult(
                action_id, EffectResultState.EXITED_UNRECONCILED,
                observed_digest=observation.observed_digest)
        if observation.disposition is ObservationDisposition.ABSENT:
            return EffectResult(
                action_id, EffectResultState.UNKNOWN_EFFECT,
                reason="effect is positively absent but has not been reconciled")
        return self._result_from_observation(plan, observation)
