"""Frozen one-task RunSpec planning and read-only base snapshot capture."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from waystone.adapters.git import GitReadError, git_full_sha, git_read_bytes
from waystone.core import WorkflowError
from waystone.project import find_project_root, load_tasks
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
    validate_sha256_digest,
)
from waystone.runs.store import EntityKind, RunStore, TransitionReason


_RUN_SPEC_SCHEMA = "waystone-run-spec-1"
_SNAPSHOT_SCHEMA = "waystone-run-base-snapshot-1"
_RUN_SPEC_REFERENCE_PREFIX = "run-spec:"
_SNAPSHOT_REFERENCE_PREFIX = "base-snapshot:"
_TIME_UNITS = frozenset({"day"})
_COST_UNITS = frozenset({"attempt"})
_COST_METERS = frozenset({"attempt-start"})
_REVIEW_REQUIRED_REASONS = frozenset({
    "trust-surface-store",
    "trust-surface-review-binding",
    "trust-surface-completion-gate",
    "trust-surface-migration",
    "trust-surface-sandbox",
    "trust-surface-evidence-authority",
    "owner-required",
})
_REVIEW_NONE_REASONS = frozenset({
    "no-review-trigger",
})


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class RunSpecError(WorkflowError):
    """Base class for typed RunSpec planning and integrity failures."""

    code = "run_spec_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class UninitializedRunSpecError(RunSpecError):
    code = "uninitialized_project"

    def __init__(self, start: Path):
        self.start = Path(start)
        super().__init__(
            f"no regular .waystone.yml identifies an initialized project from {start}")


class TaskNotFoundError(RunSpecError):
    code = "task_not_found"

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"task {task_id!r} does not exist in the project registry")


class InvalidTaskInputError(RunSpecError):
    code = "invalid_task_input"

    def __init__(self, task_id: str, detail: str):
        self.task_id = task_id
        self.detail = detail
        super().__init__(f"task {task_id!r}: {detail}")


class AcceptanceReadinessError(RunSpecError):
    code = "criterion-empty"

    def __init__(self, task_id: str, detail: str = "acceptance criteria are absent or empty"):
        self.task_id = task_id
        self.detail = detail
        super().__init__(f"task {task_id!r}: {detail}; refusing run creation")


class DuplicateCriterionError(RunSpecError):
    code = "criterion-duplicate"

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"task {task_id!r} contains duplicate acceptance criteria")


class SnapshotError(RunSpecError):
    code = "snapshot_unavailable"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class RunSpecArtifactError(RunSpecError):
    code = "run_spec_artifact_invalid"

    def __init__(self, run_id: str, detail: str):
        self.run_id = run_id
        self.detail = detail
        super().__init__(f"run {run_id!r}: {detail}")


class RunInputDriftError(RunSpecError):
    code = "run_input_drift"

    def __init__(self, drift: "RunInputDrift"):
        self.drift = drift
        self.run_id = drift.run_id
        self.task_id = drift.task_id
        changed = ", ".join(drift.changed_fields) or "task availability"
        super().__init__(
            f"run {drift.run_id!r} frozen task {drift.task_id!r} drifted in {changed}; "
            "the frozen job input remains authoritative")


class RunInputChangedDuringPlanningError(RunSpecError):
    code = "run_input_changed_during_planning"

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"task {task_id!r} changed while its RunSpec was being planned; refusing run creation")


@dataclass(frozen=True)
class BudgetLimit:
    limit: int
    unit: str

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("budget limit must be a positive integer")
        if self.unit not in _TIME_UNITS:
            raise ValueError(f"time budget unit must be one of {sorted(_TIME_UNITS)}")


@dataclass(frozen=True)
class CostBudget:
    limit: int
    unit: str
    meter: str

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("cost budget limit must be a positive integer")
        if self.unit not in _COST_UNITS:
            raise ValueError(f"cost budget unit must be one of {sorted(_COST_UNITS)}")
        if self.meter not in _COST_METERS:
            raise ValueError(f"cost budget meter must be one of {sorted(_COST_METERS)}")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_job: int
    max_total_attempts: int
    time_budget: BudgetLimit
    cost_budget: CostBudget
    retryable_failure_classes: tuple[str, ...]
    budget_exhaustion_policy: str

    def __post_init__(self) -> None:
        for label, value in (
                ("max_attempts_per_job", self.max_attempts_per_job),
                ("max_total_attempts", self.max_total_attempts)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.max_total_attempts < self.max_attempts_per_job:
            raise ValueError("max_total_attempts cannot be below max_attempts_per_job")
        if self.retryable_failure_classes:
            raise ValueError("M1-B has no registered retryable failure class")
        if self.budget_exhaustion_policy != "stop":
            raise ValueError("budget_exhaustion_policy must be 'stop'")


DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts_per_job=1,
    max_total_attempts=1,
    time_budget=BudgetLimit(limit=1, unit="day"),
    cost_budget=CostBudget(limit=1, unit="attempt", meter="attempt-start"),
    retryable_failure_classes=(),
    budget_exhaustion_policy="stop",
)


class ReviewRequirement(str, Enum):
    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True)
class ReviewDecision:
    requirement: ReviewRequirement
    reason: str
    rule_id: str
    policy_digest: str

    def __post_init__(self) -> None:
        try:
            requirement = ReviewRequirement(self.requirement)
        except (TypeError, ValueError) as error:
            raise ValueError("review requirement must be 'none' or 'required'") from error
        reasons = (
            _REVIEW_REQUIRED_REASONS
            if requirement is ReviewRequirement.REQUIRED else _REVIEW_NONE_REASONS)
        if self.reason not in reasons:
            raise ValueError(
                f"review reason {self.reason!r} is invalid for {requirement.value}")
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("review rule_id must be non-empty")
        object.__setattr__(self, "requirement", requirement)
        object.__setattr__(self, "policy_digest", validate_sha256_digest(self.policy_digest))


@dataclass(frozen=True)
class FrozenJobInput:
    task_id: str
    title: str
    acceptance_criteria: tuple[str, ...]
    scope: tuple[str, ...]
    dependencies: tuple[str, ...]
    input_digest: str

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_job_input_payload(self, include_digest=False))


@dataclass(frozen=True)
class BaseSnapshotReference:
    head: str
    reference_id: str
    digest: str
    size: int


@dataclass(frozen=True)
class SnapshotEntry:
    path: bytes
    state: str
    mode: str | None
    content: bytes | None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("snapshot path must be non-empty")
        if self.state not in ("present", "deleted"):
            raise ValueError("snapshot entry state must be present or deleted")
        if self.state == "deleted":
            if self.mode is not None or self.content is not None:
                raise ValueError("deleted snapshot entries cannot have mode or content")
        elif self.mode not in ("100644", "100755", "120000") or self.content is None:
            raise ValueError("present snapshot entries require Git mode and content")


@dataclass(frozen=True)
class BaseSnapshot:
    head: str
    entries: tuple[SnapshotEntry, ...]

    def canonical_bytes(self) -> bytes:
        return _snapshot_bytes(self.head, self.entries)


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    job_id: str
    revision: int
    readiness: str
    critic_disposition: str
    job_input: FrozenJobInput
    base_snapshot: BaseSnapshotReference
    retry: RetryPolicy
    review_decision: ReviewDecision | None
    run_spec_digest: str

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_run_spec_payload(self))


@dataclass(frozen=True)
class RunInputDrift:
    run_id: str
    task_id: str
    frozen_digest: str
    current_digest: str | None
    changed_fields: tuple[str, ...]


def _find_root(start: Path | None) -> Path:
    requested = Path.cwd() if start is None else Path(start)
    root = find_project_root(requested)
    if root is None:
        raise UninitializedRunSpecError(requested)
    marker = root / ".waystone.yml"
    try:
        marker_mode = marker.lstat().st_mode
    except OSError as error:
        raise UninitializedRunSpecError(requested) from error
    if stat.S_ISLNK(marker_mode) or not stat.S_ISREG(marker_mode):
        raise UninitializedRunSpecError(requested)
    return root


def _task_rows(root: Path) -> list[dict]:
    try:
        data = load_tasks(root)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        raise InvalidTaskInputError("<registry>", f"cannot read tasks.yaml: {error}") from error
    rows = data.get("tasks", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise InvalidTaskInputError("<registry>", "tasks must be a list of mappings")
    return rows


def _selected_task(root: Path, task_id: str) -> dict:
    if not isinstance(task_id, str) or not task_id.strip():
        raise InvalidTaskInputError(str(task_id), "task_id must be non-empty")
    matches = [row for row in _task_rows(root) if row.get("id") == task_id]
    if not matches:
        raise TaskNotFoundError(task_id)
    if len(matches) != 1:
        raise InvalidTaskInputError(task_id, "task id is duplicated")
    return matches[0]


def _string_tuple(task_id: str, value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value):
        raise InvalidTaskInputError(task_id, f"{field} must be a list of non-empty strings")
    return tuple(value)


def _freeze_task(task_id: str, task: dict) -> FrozenJobInput:
    title = task.get("title")
    if not isinstance(title, str) or not title.strip():
        raise InvalidTaskInputError(task_id, "title must be a non-empty string")
    acceptance = _string_tuple(task_id, task.get("accept"), "accept")
    if not acceptance:
        raise AcceptanceReadinessError(task_id)
    if len(set(acceptance)) != len(acceptance):
        raise DuplicateCriterionError(task_id)
    scope = _string_tuple(task_id, task.get("scope"), "scope")
    dependencies = _string_tuple(task_id, task.get("deps"), "deps")
    candidate = FrozenJobInput(
        task_id=task_id,
        title=title,
        acceptance_criteria=acceptance,
        scope=scope,
        dependencies=dependencies,
        input_digest="sha256:" + "0" * 64,
    )
    return replace(candidate, input_digest=_digest(candidate.canonical_bytes()))


def _job_input_payload(job_input: FrozenJobInput, *, include_digest: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "acceptance_criteria": list(job_input.acceptance_criteria),
        "dependencies": list(job_input.dependencies),
        "scope": list(job_input.scope),
        "task_id": job_input.task_id,
        "title": job_input.title,
    }
    if include_digest:
        payload["input_digest"] = job_input.input_digest
    return payload


def _parse_nul_paths(payload: bytes, command: str) -> set[bytes]:
    if not payload:
        return set()
    if not payload.endswith(b"\0"):
        raise SnapshotError(f"git {command} returned a non-NUL-terminated path list")
    paths = payload[:-1].split(b"\0")
    for path in paths:
        parts = path.split(b"/")
        if (not path or path.startswith(b"/") or any(
                part in (b"", b".", b"..") for part in parts)):
            raise SnapshotError(f"git {command} returned an unsafe repository path")
    return set(paths)


def _parse_index_flags(payload: bytes) -> dict[bytes, bytes]:
    if not payload:
        return {}
    if not payload.endswith(b"\0"):
        raise SnapshotError("git ls-files -v returned a non-NUL-terminated path list")
    flags: dict[bytes, bytes] = {}
    for entry in payload[:-1].split(b"\0"):
        if len(entry) < 3 or entry[1:2] != b" ":
            raise SnapshotError("git ls-files -v returned a malformed entry")
        path = entry[2:]
        _parse_nul_paths(path + b"\0", "ls-files -v")
        flags[path] = entry[:1]
    return flags


def _read_regular_file(path: Path, initial: os.stat_result) -> bytes:
    try:
        payload = path.read_bytes()
        final = path.lstat()
    except OSError as error:
        raise SnapshotError(f"snapshot path {path} changed or became unreadable: {error}") from error
    identity_before = (
        initial.st_dev, initial.st_ino, initial.st_mode, initial.st_size, initial.st_mtime_ns)
    identity_after = (
        final.st_dev, final.st_ino, final.st_mode, final.st_size, final.st_mtime_ns)
    if identity_before != identity_after or not stat.S_ISREG(final.st_mode):
        raise SnapshotError(f"snapshot path {path} changed while it was read")
    return payload


def _snapshot_entry(root: Path, raw_path: bytes, *, must_exist: bool) -> SnapshotEntry:
    path = root / os.fsdecode(raw_path)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        if must_exist:
            raise SnapshotError(f"required snapshot path {path} disappeared") from error
        return SnapshotEntry(raw_path, "deleted", None, None)
    except OSError as error:
        raise SnapshotError(f"cannot inspect snapshot path {path}: {error}") from error
    if stat.S_ISREG(info.st_mode):
        mode = "100755" if info.st_mode & 0o111 else "100644"
        return SnapshotEntry(raw_path, "present", mode, _read_regular_file(path, info))
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.fsencode(os.readlink(path))
            final = path.lstat()
        except OSError as error:
            raise SnapshotError(f"cannot read snapshot symlink {path}: {error}") from error
        if (not stat.S_ISLNK(final.st_mode)
                or (info.st_dev, info.st_ino, info.st_mtime_ns)
                != (final.st_dev, final.st_ino, final.st_mtime_ns)):
            raise SnapshotError(f"snapshot symlink {path} changed while it was read")
        return SnapshotEntry(raw_path, "present", "120000", target)
    raise SnapshotError(f"snapshot path {path} is neither a regular file nor a symlink")


def _snapshot_bytes(head: str, entries: tuple[SnapshotEntry, ...]) -> bytes:
    payload_entries = []
    for entry in entries:
        payload_entries.append({
            "content": (
                None if entry.content is None
                else base64.b64encode(entry.content).decode("ascii")),
            "mode": entry.mode,
            "path": base64.b64encode(entry.path).decode("ascii"),
            "state": entry.state,
        })
    return _canonical_json({
        "entries": payload_entries,
        "head": head,
        "schema": _SNAPSHOT_SCHEMA,
    })


@dataclass(frozen=True)
class _SnapshotGuard:
    head: str
    status: bytes
    index: bytes


def _snapshot_guard(root: Path) -> _SnapshotGuard:
    head = git_full_sha(root, "HEAD")
    if head is None:
        raise SnapshotError("repository HEAD is absent or unreadable")
    try:
        status_bytes = git_read_bytes(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        raw_index_path = git_read_bytes(root, "rev-parse", "--git-path", "index")
    except GitReadError as error:
        raise SnapshotError(str(error)) from error
    index_name = raw_index_path.rstrip(b"\r\n")
    if not index_name or b"\0" in index_name:
        raise SnapshotError("git rev-parse returned an invalid index path")
    index_path = Path(os.fsdecode(index_name))
    if not index_path.is_absolute():
        index_path = root / index_path
    try:
        index_bytes = index_path.read_bytes()
    except OSError as error:
        raise SnapshotError(f"cannot read live Git index {index_path}: {error}") from error
    return _SnapshotGuard(head=head, status=status_bytes, index=index_bytes)


def _snapshot_entries(root: Path) -> tuple[SnapshotEntry, ...]:
    try:
        unmerged = git_read_bytes(root, "ls-files", "-u", "-z")
        if unmerged:
            raise SnapshotError("repository index has unmerged entries")
        index_paths = _parse_nul_paths(
            git_read_bytes(root, "ls-files", "-z", "--cached"),
            "ls-files --cached",
        )
        head_paths = _parse_nul_paths(
            git_read_bytes(root, "ls-tree", "-r", "-z", "--name-only", "HEAD"),
            "ls-tree",
        )
        untracked = _parse_nul_paths(
            git_read_bytes(root, "ls-files", "-z", "--others", "--exclude-standard"),
            "ls-files",
        )
        index_flags = _parse_index_flags(
            git_read_bytes(root, "ls-files", "-v", "-z", "--cached"))
    except GitReadError as error:
        raise SnapshotError(str(error)) from error
    return tuple(
        _snapshot_entry(
            root,
            path,
            must_exist=(path in untracked or index_flags.get(path) in (b"S", b"s")),
        )
        for path in sorted(head_paths | index_paths | untracked)
    )


def _capture_snapshot(root: Path) -> BaseSnapshot:
    before = _snapshot_guard(root)
    first = _snapshot_entries(root)
    middle = _snapshot_guard(root)
    second = _snapshot_entries(root)
    after = _snapshot_guard(root)
    if before != middle or middle != after or first != second:
        raise SnapshotError(
            "repository HEAD, status, index, or captured content changed during snapshot")
    return BaseSnapshot(head=before.head, entries=first)


def _review_payload(decision: ReviewDecision | None) -> dict[str, str] | None:
    if decision is None:
        return None
    return {
        "policy_digest": decision.policy_digest,
        "reason": decision.reason,
        "requirement": decision.requirement.value,
        "rule_id": decision.rule_id,
    }


def _retry_payload(retry: RetryPolicy) -> dict[str, object]:
    return {
        "budget_exhaustion_policy": retry.budget_exhaustion_policy,
        "cost_budget": {
            "limit": retry.cost_budget.limit,
            "meter": retry.cost_budget.meter,
            "unit": retry.cost_budget.unit,
        },
        "max_attempts_per_job": retry.max_attempts_per_job,
        "max_total_attempts": retry.max_total_attempts,
        "retryable_failure_classes": list(retry.retryable_failure_classes),
        "time_budget": {
            "limit": retry.time_budget.limit,
            "unit": retry.time_budget.unit,
        },
    }


def _run_spec_payload(spec: RunSpec) -> dict[str, object]:
    return {
        "base_snapshot": {
            "digest": spec.base_snapshot.digest,
            "head": spec.base_snapshot.head,
            "reference_id": spec.base_snapshot.reference_id,
            "size": spec.base_snapshot.size,
        },
        "critic_disposition": spec.critic_disposition,
        "job_id": spec.job_id,
        "job_input": _job_input_payload(spec.job_input, include_digest=True),
        "readiness": spec.readiness,
        "review_decision": _review_payload(spec.review_decision),
        "retry": _retry_payload(spec.retry),
        "revision": spec.revision,
        "run_id": spec.run_id,
        "schema": _RUN_SPEC_SCHEMA,
    }


def _new_spec(
        run_id: str, job_id: str, job_input: FrozenJobInput,
        snapshot: BaseSnapshotReference, review_decision: ReviewDecision | None) -> RunSpec:
    candidate = RunSpec(
        run_id=run_id,
        job_id=job_id,
        revision=1,
        readiness="frozen-ready",
        critic_disposition="critic-not-required",
        job_input=job_input,
        base_snapshot=snapshot,
        retry=DEFAULT_RETRY_POLICY,
        review_decision=review_decision,
        run_spec_digest="sha256:" + "0" * 64,
    )
    return replace(candidate, run_spec_digest=_digest(candidate.canonical_bytes()))


def plan_one_task_run(
        task_id: str, *, start: Path | None = None,
        review_decision: ReviewDecision | None = None) -> RunSpec:
    """Freeze one registry task and its read-only Git snapshot into one run and one job."""
    root = _find_root(start)
    job_input = _freeze_task(task_id, _selected_task(root, task_id))
    snapshot_content = _capture_snapshot(root)
    confirmed_input = _freeze_task(task_id, _selected_task(root, task_id))
    if confirmed_input.input_digest != job_input.input_digest:
        raise RunInputChangedDuringPlanningError(task_id)

    with RunStore.open(root) as store:
        run = store.create_run(initial_state="candidate")
        job_id = f"{run.run_id}:job"
        store.create_job(run.run_id, job_id, initial_state="planned")
        artifact_store = ArtifactStore(root)
        stored_snapshot = artifact_store.write(snapshot_content.canonical_bytes())
        snapshot_reference = BaseSnapshotReference(
            head=snapshot_content.head,
            reference_id=f"{_SNAPSHOT_REFERENCE_PREFIX}{run.run_id}",
            digest=stored_snapshot.digest,
            size=stored_snapshot.size,
        )
        spec = _new_spec(
            run.run_id, job_id, job_input, snapshot_reference, review_decision)
        stored_spec = artifact_store.write(spec.canonical_bytes())
        if stored_spec.digest != spec.run_spec_digest:
            raise RunSpecArtifactError(run.run_id, "stored RunSpec digest changed")
        store.record_transition(
            EntityKind.RUN,
            run.run_id,
            expected_version=run.version,
            next_state="frozen-ready",
            reason=TransitionReason.PLANNED,
            evidence_digest=stored_spec.digest,
            artifact_references=(
                ArtifactReference(
                    reference_id=f"{_RUN_SPEC_REFERENCE_PREFIX}{run.run_id}",
                    kind=ArtifactReferenceKind.EVIDENCE,
                    digest=stored_spec.digest,
                    size=stored_spec.size,
                ),
                ArtifactReference(
                    reference_id=snapshot_reference.reference_id,
                    kind=ArtifactReferenceKind.EVIDENCE,
                    digest=stored_snapshot.digest,
                    size=stored_snapshot.size,
                ),
            ),
        )
        return spec


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(payload: dict[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"{label} fields are not canonical")


def _parse_string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string list")
    return tuple(value)


def _parse_run_spec(payload: bytes, expected_run_id: str, digest: str) -> RunSpec:
    try:
        decoded = json.loads(payload.decode("utf-8"))
        root = _require_mapping(decoded, "RunSpec")
        _exact_keys(root, {
            "base_snapshot", "critic_disposition", "job_id", "job_input", "readiness",
            "review_decision", "retry", "revision", "run_id", "schema",
        }, "RunSpec")
        if root["schema"] != _RUN_SPEC_SCHEMA or root["run_id"] != expected_run_id:
            raise ValueError("RunSpec schema or run identity does not match its reference")
        job = _require_mapping(root["job_input"], "job_input")
        _exact_keys(job, {
            "acceptance_criteria", "dependencies", "input_digest", "scope", "task_id", "title",
        }, "job_input")
        job_input = FrozenJobInput(
            task_id=job["task_id"],
            title=job["title"],
            acceptance_criteria=_parse_string_list(
                job["acceptance_criteria"], "acceptance_criteria"),
            scope=_parse_string_list(job["scope"], "scope"),
            dependencies=_parse_string_list(job["dependencies"], "dependencies"),
            input_digest=validate_sha256_digest(job["input_digest"]),
        )
        if _digest(job_input.canonical_bytes()) != job_input.input_digest:
            raise ValueError("job input digest does not match canonical owner fields")

        snapshot = _require_mapping(root["base_snapshot"], "base_snapshot")
        _exact_keys(snapshot, {"digest", "head", "reference_id", "size"}, "base_snapshot")
        base_snapshot = BaseSnapshotReference(
            head=snapshot["head"],
            reference_id=snapshot["reference_id"],
            digest=validate_sha256_digest(snapshot["digest"]),
            size=snapshot["size"],
        )
        retry_payload = _require_mapping(root["retry"], "retry")
        _exact_keys(retry_payload, {
            "budget_exhaustion_policy", "cost_budget", "max_attempts_per_job",
            "max_total_attempts", "retryable_failure_classes", "time_budget",
        }, "retry")
        time_budget = _require_mapping(retry_payload["time_budget"], "time_budget")
        cost_budget = _require_mapping(retry_payload["cost_budget"], "cost_budget")
        retry = RetryPolicy(
            max_attempts_per_job=retry_payload["max_attempts_per_job"],
            max_total_attempts=retry_payload["max_total_attempts"],
            time_budget=BudgetLimit(time_budget["limit"], time_budget["unit"]),
            cost_budget=CostBudget(
                cost_budget["limit"], cost_budget["unit"], cost_budget["meter"]),
            retryable_failure_classes=_parse_string_list(
                retry_payload["retryable_failure_classes"], "retryable_failure_classes"),
            budget_exhaustion_policy=retry_payload["budget_exhaustion_policy"],
        )
        review_payload = root["review_decision"]
        review = None
        if review_payload is not None:
            review_row = _require_mapping(review_payload, "review_decision")
            _exact_keys(
                review_row, {"policy_digest", "reason", "requirement", "rule_id"},
                "review_decision")
            review = ReviewDecision(
                requirement=ReviewRequirement(review_row["requirement"]),
                reason=review_row["reason"],
                rule_id=review_row["rule_id"],
                policy_digest=review_row["policy_digest"],
            )
        spec = RunSpec(
            run_id=root["run_id"],
            job_id=root["job_id"],
            revision=root["revision"],
            readiness=root["readiness"],
            critic_disposition=root["critic_disposition"],
            job_input=job_input,
            base_snapshot=base_snapshot,
            retry=retry,
            review_decision=review,
            run_spec_digest=validate_sha256_digest(digest),
        )
        if (not isinstance(spec.revision, int) or isinstance(spec.revision, bool)
                or spec.revision != 1 or spec.readiness != "frozen-ready"
                or spec.critic_disposition != "critic-not-required"):
            raise ValueError("RunSpec readiness metadata is invalid")
        if spec.canonical_bytes() != payload or _digest(payload) != spec.run_spec_digest:
            raise ValueError("RunSpec bytes are not canonical or do not match their digest")
        return spec
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise RunSpecArtifactError(expected_run_id, str(error)) from error


def load_run_spec(run_id: str, *, start: Path | None = None) -> RunSpec:
    """Load and revalidate one immutable RunSpec from its durable store reference."""
    root = _find_root(start)
    with RunStore.open(root) as store:
        store.get_run(run_id)
        reference = store.get_artifact_reference(f"{_RUN_SPEC_REFERENCE_PREFIX}{run_id}")
        payload = ArtifactStore(root).read_reference(reference)
    return _parse_run_spec(payload, run_id, reference.digest)


def _parse_snapshot(payload: bytes, expected_head: str) -> BaseSnapshot:
    try:
        decoded = json.loads(payload.decode("utf-8"))
        root = _require_mapping(decoded, "base snapshot")
        _exact_keys(root, {"entries", "head", "schema"}, "base snapshot")
        if root["schema"] != _SNAPSHOT_SCHEMA or root["head"] != expected_head:
            raise ValueError("base snapshot schema or HEAD is invalid")
        raw_entries = root["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("base snapshot entries must be a list")
        entries: list[SnapshotEntry] = []
        for raw in raw_entries:
            row = _require_mapping(raw, "snapshot entry")
            _exact_keys(row, {"content", "mode", "path", "state"}, "snapshot entry")
            path = base64.b64decode(row["path"], validate=True)
            content = (
                None if row["content"] is None
                else base64.b64decode(row["content"], validate=True))
            entries.append(SnapshotEntry(path, row["state"], row["mode"], content))
        snapshot = BaseSnapshot(head=root["head"], entries=tuple(entries))
        if tuple(sorted(entry.path for entry in snapshot.entries)) != tuple(
                entry.path for entry in snapshot.entries):
            raise ValueError("base snapshot entries are not path-sorted")
        if snapshot.canonical_bytes() != payload:
            raise ValueError("base snapshot bytes are not canonical")
        return snapshot
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotError(f"stored base snapshot is invalid: {error}") from error


def read_base_snapshot(run_id: str, *, start: Path | None = None) -> BaseSnapshot:
    """Read and verify the canonical HEAD-rooted snapshot overlay for one run."""
    root = _find_root(start)
    spec = load_run_spec(run_id, start=root)
    with RunStore.open(root) as store:
        reference = store.get_artifact_reference(spec.base_snapshot.reference_id)
        if (reference.digest != spec.base_snapshot.digest
                or reference.size != spec.base_snapshot.size):
            raise RunSpecArtifactError(run_id, "base snapshot reference disagrees with RunSpec")
        payload = ArtifactStore(root).read_reference(reference)
    return _parse_snapshot(payload, spec.base_snapshot.head)


def _changed_fields(frozen: FrozenJobInput, current: FrozenJobInput) -> tuple[str, ...]:
    fields = (
        "acceptance_criteria", "dependencies", "scope", "task_id", "title",
    )
    return tuple(field for field in fields if getattr(frozen, field) != getattr(current, field))


def detect_task_input_drift(
        run_id: str, *, start: Path | None = None) -> RunInputDrift | None:
    """Compare current owner fields with a run's frozen input without mutating either authority."""
    root = _find_root(start)
    spec = load_run_spec(run_id, start=root)
    try:
        current = _freeze_task(
            spec.job_input.task_id,
            _selected_task(root, spec.job_input.task_id),
        )
    except (TaskNotFoundError, InvalidTaskInputError, AcceptanceReadinessError,
            DuplicateCriterionError):
        return RunInputDrift(
            run_id=run_id,
            task_id=spec.job_input.task_id,
            frozen_digest=spec.job_input.input_digest,
            current_digest=None,
            changed_fields=("task_availability",),
        )
    if current.input_digest == spec.job_input.input_digest:
        return None
    return RunInputDrift(
        run_id=run_id,
        task_id=spec.job_input.task_id,
        frozen_digest=spec.job_input.input_digest,
        current_digest=current.input_digest,
        changed_fields=_changed_fields(spec.job_input, current),
    )


def assert_task_input_current(run_id: str, *, start: Path | None = None) -> RunSpec:
    """Return the frozen spec only when the owner registry still names the same input bytes."""
    drift = detect_task_input_drift(run_id, start=start)
    if drift is not None:
        raise RunInputDriftError(drift)
    return load_run_spec(run_id, start=start)


__all__ = [
    "AcceptanceReadinessError",
    "BaseSnapshot",
    "BaseSnapshotReference",
    "BudgetLimit",
    "CostBudget",
    "DEFAULT_RETRY_POLICY",
    "DuplicateCriterionError",
    "FrozenJobInput",
    "InvalidTaskInputError",
    "ReviewDecision",
    "ReviewRequirement",
    "RetryPolicy",
    "RunInputDrift",
    "RunInputDriftError",
    "RunInputChangedDuringPlanningError",
    "RunSpec",
    "RunSpecArtifactError",
    "RunSpecError",
    "SnapshotEntry",
    "SnapshotError",
    "TaskNotFoundError",
    "UninitializedRunSpecError",
    "assert_task_input_current",
    "detect_task_input_drift",
    "load_run_spec",
    "plan_one_task_run",
    "read_base_snapshot",
]
