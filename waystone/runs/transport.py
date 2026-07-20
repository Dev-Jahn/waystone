"""Carrier/user action transport and typed failure envelopes.

The transport never infers an executor from a generic action row.  Outward actions are
identified by an immutable, content-bound transport plan; effect-plan actions remain owned by
``EffectEngine`` and are driven internally.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from pathlib import Path
from typing import Mapping, Sequence

from waystone.adapters import git as git_adapter
from waystone.core import WorkflowError
from waystone.jobs.domain import ExecutorKind
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
    validate_sha256_digest,
)
from waystone.runs.effects import (
    EffectEngine,
    EffectKind,
    EffectResultState,
    ObservationDisposition,
)
from waystone.runs.lease import (
    LeaseManager,
    LeasePrincipal,
    LeasePrincipalMismatch,
    LeasePrincipalUnknown,
)
from waystone.runs.store import (
    EntityKind,
    EntityRecord,
    RecordNotFoundError,
    RunStore,
    TransitionReason,
)


_ACTION_PLAN_SCHEMA = "waystone-transport-action-plan-1"
_RESULT_SCHEMA = "waystone-transport-result-1"
_PLAN_REFERENCE_PREFIX = "transport-action-plan:"
_RESULT_REFERENCE_PREFIX = "transport-result:"
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


class ResultValueKind(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_LIST = "string-list"
    OBJECT = "object"
    ARRAY = "array"
    DIGEST = "digest"


@dataclass(frozen=True, order=True)
class ResultField:
    name: str
    kind: ResultValueKind

    def __post_init__(self) -> None:
        _nonempty(self.name, "result field name")
        object.__setattr__(self, "kind", ResultValueKind(self.kind))


@dataclass(frozen=True)
class ActionResultSchema:
    """Closed, deliberately small result schema used by one outward action."""

    fields: tuple[ResultField, ...]
    artifact_names: tuple[str, ...] = ()
    requires_git_facts: bool = False

    def __post_init__(self) -> None:
        fields = tuple(sorted(self.fields))
        artifacts = tuple(sorted(_nonempty(item, "artifact name") for item in self.artifact_names))
        if len({field.name for field in fields}) != len(fields):
            raise ValueError("result field names must be unique")
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("artifact names must be unique")
        if not isinstance(self.requires_git_facts, bool):
            raise TypeError("requires_git_facts must be a bool")
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "artifact_names", artifacts)

    def payload(self) -> dict[str, object]:
        return {
            "artifact_names": list(self.artifact_names),
            "fields": [
                {"kind": field.kind.value, "name": field.name} for field in self.fields
            ],
            "requires_git_facts": self.requires_git_facts,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ActionResultSchema":
        if not isinstance(value, dict) or set(value) != {
                "artifact_names", "fields", "requires_git_facts"}:
            raise ValueError("result schema fields are not canonical")
        raw_fields = value["fields"]
        raw_artifacts = value["artifact_names"]
        if not isinstance(raw_fields, list) or not isinstance(raw_artifacts, list):
            raise ValueError("result schema lists are malformed")
        fields = []
        for item in raw_fields:
            if not isinstance(item, dict) or set(item) != {"kind", "name"}:
                raise ValueError("result field is malformed")
            fields.append(ResultField(item["name"], ResultValueKind(item["kind"])))
        schema = cls(tuple(fields), tuple(raw_artifacts), value["requires_git_facts"])
        if schema.payload() != value:
            raise ValueError("result schema is not canonically ordered")
        return schema


@dataclass(frozen=True)
class GitFactContract:
    repository: Path
    base_sha: str


@dataclass(frozen=True, order=True)
class TestResultAuthority:
    action_id: str
    invocation_digest: str

    def payload(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "invocation_digest": self.invocation_digest,
        }


@dataclass(frozen=True)
class OutwardActionPlan:
    run_id: str
    job_id: str
    attempt_id: str
    action_id: str
    action_kind: str
    executor_kind: ExecutorKind
    input_payload: Mapping[str, object]
    input_digest: str
    result_schema: ActionResultSchema
    git_contract: GitFactContract | None
    test_authorities: tuple[TestResultAuthority, ...]
    plan_digest: str


class IdleReason(str, Enum):
    RUN_COMPLETED = "run_completed"
    RUN_WAITING_USER = "run_waiting_user"
    RUN_BLOCKED = "run_blocked"
    EFFECT_UNKNOWN = "effect_unknown"
    EFFECT_CONFLICT = "effect_conflict"


class TransportFailureCode(str, Enum):
    TRANSPORT_ERROR = "transport_error"
    ACTION_NOT_CURRENT = "action_not_current"
    INPUT_DIGEST_MISMATCH = "input_digest_mismatch"
    FENCING_EPOCH_MISMATCH = "fencing_epoch_mismatch"
    RESULT_SCHEMA_MISMATCH = "result_schema_mismatch"
    ARTIFACT_DIGEST_MISMATCH = "artifact_digest_mismatch"
    GIT_FACTS_MISMATCH = "git_facts_mismatch"
    ACTION_PLAN_INVALID = "action_plan_invalid"
    RUN_NOT_ACTIONABLE = "run_not_actionable"
    ENGINE_EXECUTOR_UNAVAILABLE = "engine_executor_unavailable"
    ENGINE_TEST_EVIDENCE_INVALID = "engine_test_evidence_invalid"
    TRANSIENT_TRANSPORT_FAILURE = "transient_transport_failure"
    UNCLASSIFIED = "unclassified"


class TransportExitCode(IntEnum):
    OK = 0
    UNCLASSIFIED = 1
    REFUSED = 2
    TEMPORARY_FAILURE = 75


class TransportError(WorkflowError):
    code = TransportFailureCode.TRANSPORT_ERROR.value
    recoverable: bool = False
    next_actions: tuple[str, ...] = ()

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")

    @property
    def exit_code(self) -> TransportExitCode:
        if self.recoverable is True:
            return TransportExitCode.TEMPORARY_FAILURE
        if self.recoverable is False:
            return TransportExitCode.REFUSED
        return TransportExitCode.UNCLASSIFIED


class ActionNotCurrent(TransportError):
    code = TransportFailureCode.ACTION_NOT_CURRENT.value


class InputDigestMismatch(TransportError):
    code = TransportFailureCode.INPUT_DIGEST_MISMATCH.value


class FencingEpochMismatch(TransportError):
    code = TransportFailureCode.FENCING_EPOCH_MISMATCH.value


class ResultSchemaMismatch(TransportError):
    code = TransportFailureCode.RESULT_SCHEMA_MISMATCH.value


class ArtifactDigestMismatch(TransportError):
    code = TransportFailureCode.ARTIFACT_DIGEST_MISMATCH.value


class GitFactsMismatch(TransportError):
    code = TransportFailureCode.GIT_FACTS_MISMATCH.value


class ActionPlanRefusal(TransportError):
    code = TransportFailureCode.ACTION_PLAN_INVALID.value


class RunNotActionable(TransportError):
    code = TransportFailureCode.RUN_NOT_ACTIONABLE.value


class EngineExecutorUnavailable(TransportError):
    code = TransportFailureCode.ENGINE_EXECUTOR_UNAVAILABLE.value


class EngineTestEvidenceRefusal(TransportError):
    code = TransportFailureCode.ENGINE_TEST_EVIDENCE_INVALID.value


class TransientTransportFailure(TransportError):
    code = TransportFailureCode.TRANSIENT_TRANSPORT_FAILURE.value
    recoverable = True


class UnclassifiedTransportFailure(TransportError):
    code = TransportFailureCode.UNCLASSIFIED.value
    # False authorizes no retry policy; the separate code preserves unknown classification.
    recoverable = False

    @property
    def exit_code(self) -> TransportExitCode:
        return TransportExitCode.UNCLASSIFIED


def _bounded_failure_chain(error: BaseException, limit: int = 8):
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < limit:
        current = pending.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yield current
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append(linked)


def classify_transport_failure(error: BaseException) -> TransportError:
    """Classify only facts that are explicit; unknown exceptions remain unclassified."""
    if isinstance(error, TransportError):
        return error
    for candidate in _bounded_failure_chain(error):
        if isinstance(candidate, (LeasePrincipalMismatch, LeasePrincipalUnknown)):
            return ActionNotCurrent(str(candidate))
        if isinstance(candidate, (ConnectionError, TimeoutError)):
            return TransientTransportFailure(str(candidate) or type(candidate).__name__)
        try:
            status = getattr(candidate, "status_code", getattr(candidate, "status", None))
        except Exception:
            status = None
        if isinstance(status, int) and not isinstance(status, bool) and 500 <= status <= 599:
            return TransientTransportFailure(f"backend returned HTTP {status}")
    return UnclassifiedTransportFailure(str(error) or type(error).__name__)


def failure_envelope(error: BaseException) -> tuple[TransportExitCode, dict[str, object]]:
    failure = classify_transport_failure(error)
    return failure.exit_code, {
        "ok": False,
        "code": failure.code,
        "recoverable": failure.recoverable,
        "next_actions": list(failure.next_actions),
    }


def _validate_envelope(value: dict[str, object]) -> None:
    if value.get("ok") is False:
        if set(value) != {"ok", "code", "recoverable", "next_actions"}:
            raise ValueError("failure envelope fields are not canonical")
        try:
            code = TransportFailureCode(value["code"])
        except (TypeError, ValueError) as error:
            raise ValueError("failure envelope code is not registered") from error
        if (not isinstance(value["recoverable"], bool)
                or value["next_actions"] != []):
            raise ValueError("failure envelope values are malformed")
        expected_recoverable = code is TransportFailureCode.TRANSIENT_TRANSPORT_FAILURE
        if value["recoverable"] is not expected_recoverable:
            raise ValueError("failure recoverability disagrees with its registered code")
        return
    if set(value) == {"action"}:
        action = value["action"]
        if not isinstance(action, dict) or set(action) != {
                "action_id", "action_kind", "entity_version", "executor_kind",
                "fencing_epoch", "input", "input_digest", "ownership", "result_schema"}:
            raise ValueError("outward action envelope is malformed")
        if (not isinstance(action["action_id"], str)
                or not isinstance(action["action_kind"], str)
                or action["executor_kind"] not in {
                    ExecutorKind.CARRIER.value, ExecutorKind.USER.value}
                or isinstance(action["entity_version"], bool)
                or not isinstance(action["entity_version"], int)
                or isinstance(action["fencing_epoch"], bool)
                or not isinstance(action["fencing_epoch"], int)
                or not isinstance(action["input"], dict)):
            raise ValueError("outward action values are malformed")
        validate_sha256_digest(action["input_digest"])
        ownership = action["ownership"]
        if (not isinstance(ownership, dict)
                or set(ownership) != {"expires_at", "kind"}
                or ownership["kind"] != "engine-claim"
                or not isinstance(ownership["expires_at"], str)):
            raise ValueError("outward action ownership is malformed")
        ActionResultSchema.from_payload(action["result_schema"])
        return
    if value.get("engine") == "busy":
        if set(value) != {"action", "engine", "poll_after_s", "run_state"}:
            raise ValueError("busy envelope fields are malformed")
        if (value["action"] is not None
                or isinstance(value["poll_after_s"], bool)
                or not isinstance(value["poll_after_s"], int)
                or value["poll_after_s"] <= 0
                or not isinstance(value["run_state"], str)
                or not value["run_state"]):
            raise ValueError("busy envelope values are malformed")
        return
    if value.get("engine") == "idle":
        if set(value) != {"action", "engine", "reason", "run_state"}:
            raise ValueError("idle envelope fields are malformed")
        try:
            reason = IdleReason(value["reason"])
        except (TypeError, ValueError) as error:
            raise ValueError("idle reason is not registered") from error
        expected = {
            IdleReason.RUN_COMPLETED: "completed",
            IdleReason.RUN_WAITING_USER: "waiting_user",
            IdleReason.RUN_BLOCKED: "blocked",
            IdleReason.EFFECT_UNKNOWN: "blocked",
            IdleReason.EFFECT_CONFLICT: "blocked",
        }[reason]
        if value["action"] is not None or value["run_state"] != expected:
            raise ValueError("idle envelope state disagrees with its reason")
        return
    if value.get("ok") is True:
        if set(value) != {"action_id", "ok", "result_digest", "state"}:
            raise ValueError("submit success envelope fields are malformed")
        if (not isinstance(value["action_id"], str) or not value["action_id"]
                or value["state"] != "completed"):
            raise ValueError("submit success envelope values are malformed")
        validate_sha256_digest(value["result_digest"])
        return
    raise ValueError("transport envelope does not match a registered branch")


def encode_envelope(payload: Mapping[str, object]) -> bytes:
    """Encode one transport payload as canonical compact UTF-8 JSON."""
    if not isinstance(payload, Mapping):
        raise TypeError("transport envelope must be a mapping")
    value = dict(payload)
    _validate_envelope(value)
    return _canonical_bytes(value)


def decode_envelope(payload: bytes) -> dict[str, object]:
    """Decode canonical transport JSON without normalizing noncanonical input."""
    if not isinstance(payload, bytes):
        raise TypeError("transport envelope bytes are required")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("transport envelope is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or _canonical_bytes(value) != payload:
        raise ValueError("transport envelope is not a canonical object")
    _validate_envelope(value)
    return value


class ActionTransport:
    """Drive engine effects internally and expose only claimed carrier/user actions."""

    def __init__(
            self, store: RunStore, effects: EffectEngine, *,
            max_engine_actions: int = 8, work_budget_s: float = 0.25,
            poll_after_s: int = 1):
        if not isinstance(store, RunStore) or not isinstance(effects, EffectEngine):
            raise TypeError("store and effects must be RunStore and EffectEngine instances")
        if effects._store is not store:  # noqa: SLF001 - package composition boundary
            raise ValueError("transport and effects must share one RunStore")
        if (isinstance(max_engine_actions, bool) or not isinstance(max_engine_actions, int)
                or max_engine_actions < 1):
            raise ValueError("max_engine_actions must be positive")
        if (isinstance(work_budget_s, bool) or not isinstance(work_budget_s, (int, float))
                or not math.isfinite(work_budget_s) or work_budget_s <= 0):
            raise ValueError("work_budget_s must be positive and finite")
        if isinstance(poll_after_s, bool) or not isinstance(poll_after_s, int) or poll_after_s < 1:
            raise ValueError("poll_after_s must be a positive integer")
        self._store = store
        self._effects = effects
        self._leases: LeaseManager = effects._leases  # noqa: SLF001
        self._artifacts = ArtifactStore(store.project_root)
        self._max_engine_actions = max_engine_actions
        self._work_budget_s = float(work_budget_s)
        self._poll_after_s = poll_after_s

    def _git_sha(self, repository: Path, ref: str) -> str:
        try:
            payload = git_adapter.git_read_bytes(
                repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
        except Exception as error:
            raise UnclassifiedTransportFailure(
                f"Git authority cannot derive {ref!r}: {error}") from error
        sha = payload.decode("ascii", errors="strict").strip()
        if _OID.fullmatch(sha) is None:
            raise UnclassifiedTransportFailure(f"Git returned a malformed commit for {ref!r}")
        return sha

    def _validated_test_authorities(
            self, run_id: str, job_id: str,
            action_ids: Sequence[str],
            expected_digests: Sequence[str] | None = None,
            ) -> tuple[TestResultAuthority, ...]:
        if isinstance(action_ids, (str, bytes)) or isinstance(expected_digests, (str, bytes)):
            raise TypeError("test authority inputs must be sequences")
        identities = tuple(sorted(_nonempty(item, "test action_id") for item in action_ids))
        if len(set(identities)) != len(identities):
            raise ActionPlanRefusal("test action IDs must be unique")
        expected = None
        if expected_digests is not None:
            expected = tuple(
                validate_sha256_digest(item) for item in expected_digests)
            if len(expected) != len(identities):
                raise ActionPlanRefusal("test authority ID/digest cardinality differs")
        authorities: list[TestResultAuthority] = []
        for index, identity in enumerate(identities):
            try:
                effect_plan = self._effects._load_plan(identity)  # noqa: SLF001
                action = self._store.get_entity(EntityKind.ACTION, identity)
                if (effect_plan.kind is not EffectKind.RUNNER_EXECUTION
                        or action.run_id != run_id or action.parent_job_id != job_id):
                    raise ValueError("not a same-run/job runner effect")
                invocation_digest = validate_sha256_digest(
                    effect_plan.spec["invocation_digest"])
                if expected is not None and invocation_digest != expected[index]:
                    raise ValueError("invocation digest changed")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                raise ActionPlanRefusal(
                    f"test action {identity!r} is not bound runner evidence: {error}") from error
            authorities.append(TestResultAuthority(identity, invocation_digest))
        return tuple(authorities)

    def _plan_outward_action(
            self, run_id: str, job_id: str, attempt_id: str, action_id: str, *,
            action_kind: str, executor_kind: ExecutorKind,
            input_payload: Mapping[str, object], result_schema: ActionResultSchema,
            git_repository: Path | None = None,
            test_action_ids: Sequence[str] = (),
            ttl_seconds: float = 300) -> dict[str, object]:
        """Create and claim one explicit carrier/user action with immutable metadata."""
        executor = ExecutorKind(executor_kind)
        if executor not in {ExecutorKind.CARRIER, ExecutorKind.USER}:
            raise ActionPlanRefusal("outward actions must be carrier or user owned")
        if not isinstance(input_payload, Mapping) or not isinstance(result_schema, ActionResultSchema):
            raise TypeError("input_payload and result_schema have invalid types")
        if any(field.name == "test_results" for field in result_schema.fields):
            raise ActionPlanRefusal(
                "test_results are engine-observed and cannot be carrier result fields")
        test_authorities = self._validated_test_authorities(
            run_id, job_id, test_action_ids)
        canonical_input = json.loads(_canonical_bytes(dict(input_payload)).decode("utf-8"))
        input_digest = _digest(_canonical_bytes(canonical_input))
        git_payload: dict[str, object] | None = None
        if result_schema.requires_git_facts:
            if git_repository is None:
                raise ActionPlanRefusal("Git fact schema requires an engine-selected repository")
            repository = Path(git_repository).resolve(strict=True)
            git_payload = {"base_sha": self._git_sha(repository, "HEAD"),
                           "repository": str(repository)}
        elif git_repository is not None:
            raise ActionPlanRefusal("Git repository supplied for a schema without Git facts")
        plan_payload = {
            "action_id": _nonempty(action_id, "action_id"),
            "action_kind": _nonempty(action_kind, "action_kind"),
            "attempt_id": _nonempty(attempt_id, "attempt_id"),
            "executor_kind": executor.value,
            "git_contract": git_payload,
            "input": canonical_input,
            "input_digest": input_digest,
            "job_id": _nonempty(job_id, "job_id"),
            "result_schema": result_schema.payload(),
            "run_id": _nonempty(run_id, "run_id"),
            "schema": _ACTION_PLAN_SCHEMA,
            "test_authorities": [authority.payload() for authority in test_authorities],
        }
        stored = self._artifacts.write(_canonical_bytes(plan_payload))
        reference = ArtifactReference(
            f"{_PLAN_REFERENCE_PREFIX}{action_id}", ArtifactReferenceKind.EVIDENCE,
            stored.digest, stored.size)
        action = self._store._create_planned_effect_action(  # noqa: SLF001
            run_id, job_id, attempt_id, action_id,
            evidence_digest=stored.digest, artifact_references=(reference,))
        claimed, principal = self._claim_outward_action(action, ttl_seconds=ttl_seconds)
        return self._outward_payload(
            self._load_outward_plan(claimed), claimed, principal)

    def _claim_outward_action(
            self, action: EntityRecord, *, ttl_seconds: float = 300,
            ) -> tuple[EntityRecord, LeasePrincipal]:
        if action.state != "planned":
            raise ActionNotCurrent(
                f"outward action {action.entity_id!r} cannot be claimed from {action.state!r}")
        principal = self._effects._maybe_current_principal(action)  # noqa: SLF001
        if principal is None:
            principal = self._leases.claim(
                action.entity_id, expected_entity_version=action.version,
                ttl_seconds=ttl_seconds)

        def claim_transition() -> EntityRecord:
            return self._store._record_guarded_action_transition(  # noqa: SLF001
                action.entity_id, expected_version=principal.entity_version,
                owner_token=principal.owner_token, fencing_epoch=principal.fencing_epoch,
                next_state="claimed", reason=TransitionReason.CLAIMED,
                evidence_digest=None)

        try:
            claimed = self._leases.guard_effect_start(principal, claim_transition)
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise ActionNotCurrent(
                f"outward action {action.entity_id!r} changed before claim") from error
        return claimed, replace(principal, entity_version=claimed.version)

    def _load_outward_plan(self, action: EntityRecord) -> OutwardActionPlan:
        reference_id = f"{_PLAN_REFERENCE_PREFIX}{action.entity_id}"
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload = self._artifacts.read_reference(reference)
            row = json.loads(payload.decode("utf-8"))
            if not isinstance(row, dict) or payload != _canonical_bytes(row):
                raise ValueError("plan bytes are not canonical")
            if set(row) != {
                    "action_id", "action_kind", "attempt_id", "executor_kind",
                    "git_contract", "input", "input_digest", "job_id", "result_schema",
                    "run_id", "schema", "test_authorities"}:
                raise ValueError("plan fields are not canonical")
            executor = ExecutorKind(row["executor_kind"])
            if executor not in {ExecutorKind.CARRIER, ExecutorKind.USER}:
                raise ValueError("transport plan does not own an outward executor")
            input_payload = row["input"]
            if not isinstance(input_payload, dict):
                raise ValueError("action input is not an object")
            input_digest = validate_sha256_digest(row["input_digest"])
            if input_digest != _digest(_canonical_bytes(input_payload)):
                raise ValueError("action input digest does not rederive")
            schema = ActionResultSchema.from_payload(row["result_schema"])
            raw_git = row["git_contract"]
            git_contract = None
            if raw_git is not None:
                if (not isinstance(raw_git, dict)
                        or set(raw_git) != {"base_sha", "repository"}
                        or _OID.fullmatch(str(raw_git["base_sha"])) is None):
                    raise ValueError("Git fact contract is malformed")
                repository = Path(raw_git["repository"])
                if (not repository.is_absolute()
                        or repository.resolve(strict=True) != repository):
                    raise ValueError("Git fact repository is not a stable absolute path")
                git_contract = GitFactContract(repository, raw_git["base_sha"])
            if schema.requires_git_facts != (git_contract is not None):
                raise ValueError("result schema and Git fact contract disagree")
            raw_authorities = row["test_authorities"]
            if not isinstance(raw_authorities, list) or any(
                    not isinstance(item, dict)
                    or set(item) != {"action_id", "invocation_digest"}
                    for item in raw_authorities):
                raise ValueError("test authorities are malformed")
            test_authorities = self._validated_test_authorities(
                action.run_id, action.parent_job_id or "",
                [item["action_id"] for item in raw_authorities],
                [item["invocation_digest"] for item in raw_authorities])
            if [authority.payload() for authority in test_authorities] != raw_authorities:
                raise ValueError("test authorities are not canonically ordered")
            identities = {
                "action_id": action.entity_id, "attempt_id": action.parent_attempt_id,
                "job_id": action.parent_job_id, "run_id": action.run_id,
            }
            if row["schema"] != _ACTION_PLAN_SCHEMA or any(
                    row[key] != value for key, value in identities.items()):
                raise ValueError("plan identity does not match current action")
            with self._store._connection_lock:  # noqa: SLF001
                binding = self._store._connection.execute(  # noqa: SLF001
                    "SELECT a.entity_id, a.entity_version, t.next_state, t.evidence_digest "
                    "FROM artifacts a JOIN transitions t ON t.transition_id = a.transition_id "
                    "WHERE a.reference_id = ?", (reference_id,)).fetchone()
            if (reference.kind is not ArtifactReferenceKind.EVIDENCE or binding is None
                    or binding["entity_id"] != action.entity_id
                    or binding["entity_version"] != 1
                    or binding["entity_version"] > action.version
                    or binding["next_state"] != "planned"
                    or binding["evidence_digest"] != reference.digest):
                raise ValueError("plan reference is not bound to the planned action")
        except (ActionPlanRefusal, KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            raise ActionPlanRefusal(
                f"action {action.entity_id!r} has no valid outward plan: {error}") from error
        return OutwardActionPlan(
            action.run_id, action.parent_job_id or "", action.parent_attempt_id or "",
            action.entity_id, row["action_kind"], executor, input_payload, input_digest,
            schema, git_contract, test_authorities, reference.digest)

    def _current_principal(self, action: EntityRecord) -> tuple[LeasePrincipal, str]:
        with self._store._connection_lock:  # noqa: SLF001
            rows = self._store._connection.execute(  # noqa: SLF001
                "SELECT run_id, entity_kind, entity_id, entity_version, owner_token, "
                "fencing_epoch, expires_at FROM leases WHERE lease_id = ? OR "
                "(entity_kind = ? AND entity_id = ?)",
                (action.entity_id, EntityKind.ACTION.value, action.entity_id),
            ).fetchall()
        if len(rows) != 1:
            raise LeasePrincipalUnknown(
                action.entity_id, "transport-read", "lease is missing or ambiguous")
        row = rows[0]
        owner = row["owner_token"]
        epoch = row["fencing_epoch"]
        expiry = row["expires_at"]
        if (row["run_id"] != action.run_id
                or row["entity_kind"] != EntityKind.ACTION.value
                or row["entity_id"] != action.entity_id
                or row["entity_version"] != action.version
                or not isinstance(owner, str) or not owner
                or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1
                or not isinstance(expiry, str) or not expiry):
            raise LeasePrincipalUnknown(
                action.entity_id, "transport-read", "lease/action tuple is incoherent")
        return LeasePrincipal(
            action.run_id, action.entity_id, owner, epoch, action.version, 0.0), expiry

    def _outward_payload(
            self, plan: OutwardActionPlan, action: EntityRecord,
            principal: LeasePrincipal | None = None) -> dict[str, object]:
        try:
            current, expiry = self._current_principal(action)
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise ActionNotCurrent(
                f"outward action {action.entity_id!r} has no current principal") from error
        if principal is not None and current.cas_tuple != principal.cas_tuple:
            raise ActionPlanRefusal("claimed principal changed while rendering action")
        return {
            "action_id": plan.action_id,
            "action_kind": plan.action_kind,
            "entity_version": action.version,
            "executor_kind": plan.executor_kind.value,
            "fencing_epoch": current.fencing_epoch,
            "input": dict(plan.input_payload),
            "input_digest": plan.input_digest,
            "ownership": {"expires_at": expiry, "kind": "engine-claim"},
            "result_schema": plan.result_schema.payload(),
        }

    def _action_ids(self, run_id: str) -> tuple[str, ...]:
        with self._store._connection_lock:  # noqa: SLF001
            rows = self._store._connection.execute(  # noqa: SLF001
                "SELECT action_id FROM actions WHERE run_id = ? AND state != 'completed' "
                "ORDER BY action_id", (run_id,)).fetchall()
        return tuple(row["action_id"] for row in rows)

    def _plan_kinds(self, action_id: str) -> tuple[bool, bool]:
        with self._store._connection_lock:  # noqa: SLF001
            rows = self._store._connection.execute(  # noqa: SLF001
                "SELECT reference_id FROM artifacts WHERE entity_kind = ? AND entity_id = ? "
                "AND reference_id IN (?, ?)",
                (EntityKind.ACTION.value, action_id, f"effect-plan:{action_id}",
                 f"{_PLAN_REFERENCE_PREFIX}{action_id}"),
            ).fetchall()
        refs = {row["reference_id"] for row in rows}
        return f"effect-plan:{action_id}" in refs, f"{_PLAN_REFERENCE_PREFIX}{action_id}" in refs

    def _busy(self, run_state: str) -> dict[str, object]:
        return {"action": None, "engine": "busy", "poll_after_s": self._poll_after_s,
                "run_state": run_state}

    @staticmethod
    def _idle(reason: IdleReason, run_state: str) -> dict[str, object]:
        expected = {
            IdleReason.RUN_COMPLETED: "completed",
            IdleReason.RUN_WAITING_USER: "waiting_user",
            IdleReason.RUN_BLOCKED: "blocked",
            IdleReason.EFFECT_UNKNOWN: "blocked",
            IdleReason.EFFECT_CONFLICT: "blocked",
        }[reason]
        if run_state != expected:
            raise RunNotActionable(
                f"idle reason {reason.value!r} requires run state {expected!r}, "
                f"not {run_state!r}")
        return {"action": None, "engine": "idle", "run_state": run_state,
                "reason": reason.value}

    def actions_next(self, run_id: str) -> dict[str, object]:
        """Drive bounded engine work, returning exactly one ADR-0004 branch."""
        started = time.monotonic()
        progressed = 0
        while True:
            run = self._store.get_run(run_id)
            engine_actions: list[tuple[EntityRecord, object]] = []
            for action_id in self._action_ids(run_id):
                action = self._store.get_entity(EntityKind.ACTION, action_id)
                effect_plan, outward_plan = self._plan_kinds(action_id)
                if effect_plan and outward_plan:
                    raise ActionPlanRefusal(f"action {action_id!r} has conflicting executor plans")
                if outward_plan:
                    plan = self._load_outward_plan(action)
                    if action.state == "planned":
                        action, _principal = self._claim_outward_action(action)
                    elif action.state != "claimed":
                        raise ActionNotCurrent(
                            f"outward action {action_id!r} is not currently claimed")
                    return {"action": self._outward_payload(plan, action)}
                if effect_plan:
                    engine_actions.append((action, self._effects._load_plan(action_id)))  # noqa: SLF001
                else:
                    raise ActionPlanRefusal(
                        f"action {action_id!r} has no explicit executor plan")
            if not engine_actions:
                if run.state == "completed":
                    return self._idle(IdleReason.RUN_COMPLETED, run.state)
                if run.state == "waiting_user":
                    return self._idle(IdleReason.RUN_WAITING_USER, run.state)
                if run.state == "blocked":
                    return self._idle(IdleReason.RUN_BLOCKED, run.state)
                raise RunNotActionable(
                    f"run {run_id!r} state {run.state!r} has no ready or active action")
            if (progressed >= self._max_engine_actions
                    or time.monotonic() - started >= self._work_budget_s):
                return self._busy(run.state)

            action, plan = engine_actions[0]
            if plan.kind is EffectKind.RUNNER_EXECUTION:
                if action.state == "planned":
                    raise EngineExecutorUnavailable(
                        "planned runner effects require the nonblocking supervisor boundary")
                if action.state in {"effect", "observed"}:
                    inspected = self._effects.inspect_effect(action.entity_id)
                    if inspected.state is EffectResultState.IN_FLIGHT:
                        return self._busy(run.state)
                    if inspected.state is EffectResultState.UNKNOWN_EFFECT:
                        return self._idle(IdleReason.EFFECT_UNKNOWN, run.state)
            result = self._effects.reconcile_actions((action.entity_id,))[0]
            progressed += 1
            if result.state in {EffectResultState.COMPLETED, EffectResultState.NOOP}:
                continue
            if result.state is EffectResultState.IN_FLIGHT:
                return self._busy(run.state)
            if result.state is EffectResultState.CONFLICT:
                return self._idle(IdleReason.EFFECT_CONFLICT, run.state)
            if result.state is EffectResultState.UNKNOWN_EFFECT:
                return self._idle(IdleReason.EFFECT_UNKNOWN, run.state)
            # EXITED_UNRECONCILED is positive work still owned by the engine.
            return self._busy(run.state)

    @staticmethod
    def _valid_field(kind: ResultValueKind, value: object) -> bool:
        if kind is ResultValueKind.STRING:
            return isinstance(value, str)
        if kind is ResultValueKind.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if kind is ResultValueKind.NUMBER:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            try:
                return math.isfinite(value)
            except OverflowError:
                return False
        if kind is ResultValueKind.BOOLEAN:
            return isinstance(value, bool)
        if kind is ResultValueKind.STRING_LIST:
            return isinstance(value, list) and all(isinstance(item, str) for item in value)
        if kind is ResultValueKind.OBJECT:
            return isinstance(value, dict)
        if kind is ResultValueKind.ARRAY:
            return isinstance(value, list)
        if kind is ResultValueKind.DIGEST:
            try:
                validate_sha256_digest(value)
            except (TypeError, ValueError):
                return False
            return True
        raise AssertionError(kind)

    def _validate_result(self, schema: ActionResultSchema, value: object) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {field.name for field in schema.fields}:
            raise ResultSchemaMismatch("result fields do not match the action schema")
        for field in schema.fields:
            if not self._valid_field(field.kind, value[field.name]):
                raise ResultSchemaMismatch(
                    f"result field {field.name!r} is not {field.kind.value}")
        try:
            if json.loads(_canonical_bytes(value).decode("utf-8")) != value:
                raise ValueError("result changes under canonical JSON round-trip")
        except (TypeError, ValueError) as error:
            raise ResultSchemaMismatch("result is not canonical-JSON serializable") from error
        return value

    def _validate_artifacts(
            self, schema: ActionResultSchema, raw: object,
            ) -> tuple[tuple[str, str, bytes], ...]:
        if not isinstance(raw, list):
            raise ResultSchemaMismatch("artifacts must be a list")
        decoded: list[tuple[str, str, bytes]] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"content_base64", "digest", "name"}:
                raise ResultSchemaMismatch("artifact fields are malformed")
            name = item["name"]
            if not isinstance(name, str):
                raise ResultSchemaMismatch("artifact name is malformed")
            try:
                digest = validate_sha256_digest(item["digest"])
                if not isinstance(item["content_base64"], str):
                    raise TypeError("content_base64 is not text")
                content = base64.b64decode(item["content_base64"], validate=True)
            except (TypeError, ValueError) as error:
                raise ArtifactDigestMismatch(f"artifact {name!r} cannot be decoded") from error
            if _digest(content) != digest:
                raise ArtifactDigestMismatch(
                    f"artifact {name!r} digest differs from its actual bytes")
            decoded.append((name, digest, content))
        if tuple(sorted(name for name, _digest_value, _content in decoded)) != schema.artifact_names:
            raise ResultSchemaMismatch("artifact names do not match the action schema")
        if len({name for name, _digest_value, _content in decoded}) != len(decoded):
            raise ResultSchemaMismatch("artifact names are duplicated")
        return tuple(sorted(decoded))

    def _derive_git_facts(self, contract: GitFactContract) -> dict[str, object]:
        result_sha = self._git_sha(contract.repository, "HEAD")
        try:
            raw_paths = git_adapter.git_read_bytes(
                contract.repository, "diff", "--name-only", "-z",
                contract.base_sha, result_sha, "--")
        except Exception as error:
            raise UnclassifiedTransportFailure(
                f"Git authority cannot derive changed files: {error}") from error
        changed = sorted(os.fsdecode(item) for item in raw_paths.split(b"\0") if item)
        return {"changed_files": changed, "result_sha": result_sha}

    def _derive_test_results(
            self, outward: OutwardActionPlan) -> list[dict[str, object]]:
        """Freshly derive runner facts from effect-owned observation evidence."""
        results: list[dict[str, object]] = []
        for authority in outward.test_authorities:
            try:
                action = self._store.get_entity(EntityKind.ACTION, authority.action_id)
                effect_plan = self._effects._load_plan(authority.action_id)  # noqa: SLF001
                if (action.state != "completed"
                        or action.run_id != outward.run_id
                        or action.parent_job_id != outward.job_id
                        or effect_plan.kind is not EffectKind.RUNNER_EXECUTION
                        or effect_plan.spec["invocation_digest"] != authority.invocation_digest):
                    raise ValueError("runner action is not the bound completed test invocation")
                intent = self._effects._load_intent(effect_plan)  # noqa: SLF001
                observation = self._effects._observe(effect_plan, intent)  # noqa: SLF001
                if (observation.disposition is not ObservationDisposition.DESIRED
                        or observation.observed_digest is None):
                    raise ValueError("runner authority is not freshly desired")
                receipt_error = self._effects._observation_receipt_error(  # noqa: SLF001
                    effect_plan, observation)
                if receipt_error is not None:
                    raise ValueError(receipt_error)
                marker = observation.evidence.get("marker")
                if not isinstance(marker, dict):
                    raise ValueError("fresh runner observation has no normalized marker")
                stdout_digest = validate_sha256_digest(
                    marker["stdout_artifact_digest"])
                stderr_digest = validate_sha256_digest(
                    marker["stderr_artifact_digest"])
                self._artifacts.read(stdout_digest)
                self._artifacts.read(stderr_digest)
                results.append({
                    "invocation_digest": authority.invocation_digest,
                    "returncode": marker["returncode"],
                    "runner_action_id": authority.action_id,
                    "signal": marker["signal"],
                    "stderr_artifact_digest": stderr_digest,
                    "stdout_artifact_digest": stdout_digest,
                })
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                raise EngineTestEvidenceRefusal(
                    f"runner test authority {authority.action_id!r} is invalid: {error}") from error
        return results

    @staticmethod
    def _reference_component(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def submit(self, action_id: str, result_payload: Mapping[str, object]) -> dict[str, object]:
        """Validate one current outward result and let the engine commit its transition."""
        try:
            action = self._store.get_entity(EntityKind.ACTION, action_id)
        except RecordNotFoundError as error:
            raise ActionNotCurrent(f"action {action_id!r} is not a current outward claim") from error
        if action.state != "claimed":
            raise ActionNotCurrent(f"action {action_id!r} is not currently claimed")
        plan = self._load_outward_plan(action)
        try:
            principal, _expiry = self._current_principal(action)
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise ActionNotCurrent(
                f"action {action_id!r} has no verifiable current principal") from error
        if not isinstance(result_payload, Mapping):
            raise ActionNotCurrent("result payload cannot identify the current claim")
        supplied = dict(result_payload)
        entity_version = supplied.get("entity_version")
        if (action.state != "claimed"
                or isinstance(entity_version, bool) or not isinstance(entity_version, int)
                or entity_version != action.version):
            raise ActionNotCurrent("entity version or claimed state is not current")
        if supplied.get("input_digest") != plan.input_digest:
            raise InputDigestMismatch("submitted input digest differs from the immutable action")
        fencing_epoch = supplied.get("fencing_epoch")
        if (isinstance(fencing_epoch, bool) or not isinstance(fencing_epoch, int)
                or fencing_epoch != principal.fencing_epoch):
            raise FencingEpochMismatch("submitted fencing epoch is not current")
        expected_keys = {
            "artifacts", "entity_version", "fencing_epoch", "input_digest",
            "result",
        }
        if plan.git_contract is not None:
            expected_keys.add("git_facts")
        if set(supplied) != expected_keys:
            raise ResultSchemaMismatch("submit envelope fields do not match the action schema")
        result = self._validate_result(plan.result_schema, supplied["result"])
        artifacts = self._validate_artifacts(plan.result_schema, supplied["artifacts"])
        if plan.git_contract is not None:
            if (not isinstance(supplied["git_facts"], dict)
                    or set(supplied["git_facts"]) != {"changed_files", "result_sha"}):
                raise GitFactsMismatch(
                    "carrier Git facts must contain only result_sha and changed_files")
        # Keep authority observation and CAS publication outside LeaseManager's short DB guard.
        # The second guard is the sole point that promotes those immutable bytes to references.
        try:
            self._leases.guard_submit(principal, lambda: None)
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise ActionNotCurrent("action changed before result validation") from error

        git_facts = None
        if plan.git_contract is not None:
            git_facts = self._derive_git_facts(plan.git_contract)
            if supplied["git_facts"] != git_facts:
                raise GitFactsMismatch("carrier Git facts differ from engine observations")
        test_results = self._derive_test_results(plan)
        artifact_references: list[ArtifactReference] = []
        artifact_manifest = []
        action_component = self._reference_component(action_id)
        for name, digest, content in artifacts:
            stored = self._artifacts.write(content)
            if stored.digest != digest:
                raise ArtifactDigestMismatch(f"published artifact {name!r} changed")
            artifact_manifest.append({"digest": digest, "name": name, "size": stored.size})
            artifact_references.append(ArtifactReference(
                f"{_RESULT_REFERENCE_PREFIX}{action_component}:artifact:"
                f"{self._reference_component(name)}",
                ArtifactReferenceKind.EVIDENCE, digest, stored.size))
        result_evidence = {
            "action_id": action_id,
            "artifacts": artifact_manifest,
            "fencing_epoch": principal.fencing_epoch,
            "git_facts": git_facts,
            "input_digest": plan.input_digest,
            "result": result,
            "schema": _RESULT_SCHEMA,
            "test_results": test_results,
        }
        result_bytes = _canonical_bytes(result_evidence)
        stored_result = self._artifacts.write(result_bytes)
        result_digest = stored_result.digest
        artifact_references.insert(0, ArtifactReference(
            f"{_RESULT_REFERENCE_PREFIX}{action_component}",
            ArtifactReferenceKind.EVIDENCE,
            result_digest, stored_result.size))

        def complete() -> EntityRecord:
            current = self._store._load_record(EntityKind.ACTION, action_id)  # noqa: SLF001
            if current.state != "claimed" or current.version != principal.entity_version:
                raise ActionNotCurrent("action changed before result commit")
            return self._store._record_guarded_action_transition(  # noqa: SLF001
                action_id, expected_version=principal.entity_version,
                owner_token=principal.owner_token, fencing_epoch=principal.fencing_epoch,
                next_state="completed", reason=TransitionReason.COMPLETED,
                evidence_digest=result_digest,
                artifact_references=tuple(artifact_references))

        try:
            completed = self._leases.guard_submit(principal, complete)
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise ActionNotCurrent("action changed before result commit") from error
        return {"action_id": action_id, "ok": True, "result_digest": result_digest,
                "state": completed.state}


__all__ = [
    "ActionNotCurrent", "ActionPlanRefusal", "ActionResultSchema", "ActionTransport",
    "ArtifactDigestMismatch", "EngineExecutorUnavailable", "EngineTestEvidenceRefusal",
    "FencingEpochMismatch", "GitFactsMismatch", "IdleReason", "InputDigestMismatch",
    "ResultField", "ResultSchemaMismatch", "ResultValueKind", "RunNotActionable",
    "TestResultAuthority", "TransientTransportFailure", "TransportError",
    "TransportExitCode", "TransportFailureCode",
    "UnclassifiedTransportFailure", "classify_transport_failure", "decode_envelope",
    "encode_envelope", "failure_envelope",
]
