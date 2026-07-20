"""Cancellation, quiescence, and destructive-cleanup coordination."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from waystone.core import WorkflowError
from waystone.runs import store as store_module
from waystone.runs import supervisor as supervisor_module
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
)
from waystone.runs.effects import (
    EffectEngine,
    EffectKind,
    EffectPlan,
    EffectResult,
    EffectResultState,
)
from waystone.runs.lease import (
    LeaseAlreadyClaimed,
    LeaseManager,
    LeasePrincipal,
    LeasePrincipalMismatch,
    LeasePrincipalUnknown,
)
from waystone.runs.store import (
    AppendOnlyConflict,
    EntityKind,
    EntityRecord,
    EntityVersionConflict,
    RecordNotFoundError,
    RunStore,
    TransitionReason,
)
from waystone.runs.supervisor import (
    LivenessObservation,
    LivenessState,
    ProcessIdentity,
    Supervisor,
    observe_process_identity,
)


class CancellationError(WorkflowError):
    """Base class for fail-loud cancellation failures."""

    code = "cancellation_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class CancellationStateRefusal(CancellationError):
    """The current durable state does not admit the requested cancellation step."""

    code = "cancellation_state_refusal"

    def __init__(self, run_id: str, operation: str, state: str):
        self.run_id = run_id
        self.operation = operation
        self.state = state
        super().__init__(
            f"cannot {operation} run {run_id!r} from state {state!r}")


class CancellationStateError(CancellationError):
    """Cancellation authority state is missing, corrupt, or changed concurrently."""

    code = "cancellation_state_error"

    def __init__(self, identity: str, detail: str):
        self.identity = identity
        self.detail = detail
        super().__init__(f"{identity}: {detail}")


class CancellationIdentityRefusal(CancellationError):
    """The supervisor identity is not bound to the current action authority."""

    code = "cancellation_identity_refusal"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class SignalDeliveryError(CancellationError):
    """The engine-owned signal adapter failed after stopping was recorded."""

    code = "signal_delivery_error"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class SignalCapabilityUnavailable(CancellationError):
    """The engine has no configured signal-delivery capability."""

    code = "signal_capability_unavailable"

    def __init__(self, action_id: str):
        self.action_id = action_id
        super().__init__(f"action {action_id!r}: engine-owned signal adapter is unavailable")


class CancellationNotQuiescent(CancellationError):
    """Terminal cancellation lacks one of the required positive observations."""

    code = "cancellation_not_quiescent"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class CleanupRefused(CancellationError):
    """Destructive cleanup is not authorized by the current observed facts."""

    code = "cleanup_refused"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"action {action_id!r}: {detail}")


class CleanupExecutionError(CancellationError):
    """An idempotent cleanup adapter failed after its durable WAI."""

    code = "cleanup_execution_error"

    def __init__(self, action_id: str, detail: str):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"cleanup action {action_id!r}: {detail}")


class CancellationScopeRefusal(CancellationError):
    """M1-B cancellation refuses a run with another source action."""

    code = "cancellation_scope_refusal"

    def __init__(self, run_id: str, action_ids: tuple[str, ...]):
        self.run_id = run_id
        self.action_ids = action_ids
        super().__init__(
            f"run {run_id!r} is outside the single-source-action cancellation scope: "
            f"{action_ids!r}")


class CancelPendingReason(str, Enum):
    IDENTITY_UNKNOWN = "identity-unknown"
    LIVENESS_UNKNOWN = "liveness-unknown"
    UNKNOWN_EFFECT = "unknown-effect"


class CleanupDisposition(str, Enum):
    CLEANED = "cleaned"
    NOOP = "no-op"


@dataclass(frozen=True)
class CancellationIntent:
    run_id: str
    action_id: str
    reason: str


@dataclass(frozen=True)
class CleanupPlan:
    """Durable identity passed to one fixed, engine-owned idempotent adapter."""

    cleanup_id: str
    run_id: str
    action_id: str
    principal: LeasePrincipal
    cleanup_action_id: str
    cleanup_principal: LeasePrincipal
    executor_id: str


@dataclass(frozen=True)
class CancellationResult:
    run_id: str
    action_id: str
    state: str
    pending_reason: CancelPendingReason | None
    liveness: LivenessObservation | None
    effect: EffectResult | None
    signal_sent: bool = False
    principal: LeasePrincipal | None = None


@dataclass(frozen=True)
class CleanupResult:
    run_id: str
    action_id: str
    cleanup_action_id: str
    disposition: CleanupDisposition


SignalSender = Callable[[ProcessIdentity], None]
CleanupExecutor = Callable[[CleanupPlan], None]

_INTENT_SCHEMA = "waystone-cancellation-intent-1"
_TERMINAL_SCHEMA = "waystone-cancellation-terminal-1"
_CLEANUP_PLAN_SCHEMA = "waystone-cancellation-cleanup-plan-1"
_CANCEL_REQUESTED = "cancel-requested"
_STOPPING = "stopping"
_CANCELED = "canceled"
_CLEANUP_READY = "cleanup-ready"
_CLEANUP_EXECUTING = "cleanup-executing"
_CLEANUP_COMPLETED = "cleanup-completed"
_PENDING_PREFIX = "cancel-pending(reason="
_RECONCILED_EFFECT_STATES = {
    EffectResultState.COMPLETED,
    EffectResultState.NOOP,
}
_UNCERTAIN_EFFECT_STATES = {
    EffectResultState.UNKNOWN_EFFECT,
    EffectResultState.IN_FLIGHT,
    EffectResultState.CONFLICT,
}


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
        raise ValueError("cancellation evidence must be canonical-JSON serializable") from error


def _pending_state(reason: CancelPendingReason) -> str:
    return f"{_PENDING_PREFIX}{reason.value})"


def _pending_reason(state: str) -> CancelPendingReason | None:
    if not state.startswith(_PENDING_PREFIX) or not state.endswith(")"):
        return None
    try:
        return CancelPendingReason(state[len(_PENDING_PREFIX):-1])
    except ValueError:
        return None


def _intent_reference_id(run_id: str) -> str:
    return f"cancellation-intent:{run_id}"


def _intent_payload(intent: CancellationIntent) -> dict[str, str]:
    return {
        "schema": _INTENT_SCHEMA,
        "run_id": intent.run_id,
        "action_id": intent.action_id,
        "reason": intent.reason,
    }


def _terminal_reference_id(run_id: str) -> str:
    return f"cancellation-terminal:{run_id}"


def _cleanup_action_id(intent_digest: str) -> str:
    return "cancel-cleanup:" + intent_digest.split(":", 1)[1]


def _cleanup_reference_id(cleanup_action_id: str) -> str:
    return f"cancellation-cleanup-plan:{cleanup_action_id}"


class CancellationEngine:
    """Compose store, supervisor, effects, and lease authority for one-task runs."""

    def __init__(
            self, store: RunStore, effects: EffectEngine,
            leases: LeaseManager, supervisor: Supervisor, *,
            signal_sender: SignalSender | None = None,
            cleanup_executor: CleanupExecutor | None = None,
            cleanup_executor_id: str | None = None):
        if not isinstance(store, RunStore):
            raise TypeError("store must be a RunStore")
        if not isinstance(effects, EffectEngine):
            raise TypeError("effects must be an EffectEngine")
        if not isinstance(leases, LeaseManager):
            raise TypeError("leases must be a LeaseManager")
        if not isinstance(supervisor, Supervisor):
            raise TypeError("supervisor must be a Supervisor")
        if signal_sender is not None and not callable(signal_sender):
            raise TypeError("signal_sender must be callable")
        if cleanup_executor is not None and not callable(cleanup_executor):
            raise TypeError("cleanup_executor must be callable")
        if cleanup_executor is None and cleanup_executor_id is not None:
            raise ValueError("cleanup_executor_id requires a cleanup_executor")
        if cleanup_executor is not None:
            cleanup_executor_id = _nonempty(
                cleanup_executor_id, "cleanup_executor_id")
        if (effects._store is not store  # noqa: SLF001 - package composition contract
                or leases._store is not store  # noqa: SLF001
                or supervisor._store is not store):  # noqa: SLF001
            raise ValueError("cancellation collaborators must share one RunStore")
        self._store = store
        self._effects = effects
        self._leases = leases
        self._supervisor = supervisor
        self._artifacts = ArtifactStore(store.project_root)
        self._signal_sender = signal_sender
        self._cleanup_executor = cleanup_executor
        self._cleanup_executor_id = cleanup_executor_id

    def _records(self, run_id: str, action_id: str) -> tuple[EntityRecord, EntityRecord]:
        run = self._store.get_run(_nonempty(run_id, "run_id"))
        action = self._store.get_entity(
            EntityKind.ACTION, _nonempty(action_id, "action_id"))
        if action.run_id != run.entity_id:
            raise CancellationIdentityRefusal(
                action.entity_id, "action belongs to a different run")
        return run, action

    def _action_ids_locked(self, run_id: str) -> tuple[str, ...]:
        try:
            rows = self._store._connection.execute(  # noqa: SLF001
                "SELECT action_id FROM actions WHERE run_id = ? ORDER BY action_id",
                (run_id,),
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise CancellationStateError(
                run_id, f"cannot inspect cancellation action scope: {error}") from error
        return tuple(row["action_id"] for row in rows)

    def _require_action_scope(
            self, run_id: str, action_id: str, *,
            intent_digest: str | None = None,
            terminal_entry: bool = False) -> None:
        allowed = {action_id}
        if intent_digest is not None and not terminal_entry:
            allowed.add(_cleanup_action_id(intent_digest))
        with self._store._connection_lock:  # noqa: SLF001
            action_ids = self._action_ids_locked(run_id)
        if set(action_ids) not in ({action_id}, allowed):
            raise CancellationScopeRefusal(run_id, action_ids)

    def _require_runner_plan(self, action: EntityRecord) -> EffectPlan:
        plan = self._effects._load_plan(action.entity_id)  # noqa: SLF001
        if plan.kind is not EffectKind.RUNNER_EXECUTION:
            raise CancellationStateRefusal(
                action.run_id, "cancel non-runner action", action.state)
        return plan

    def _record_intent(
            self, run: EntityRecord, action: EntityRecord,
            reason: str) -> tuple[EntityRecord, CancellationIntent, str]:
        intent = CancellationIntent(run.entity_id, action.entity_id, reason)
        stored = self._artifacts.write(_canonical_bytes(_intent_payload(intent)))
        reference = ArtifactReference(
            reference_id=_intent_reference_id(run.entity_id),
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=stored.digest,
            size=stored.size,
        )
        updated = self._store.record_transition(
            EntityKind.RUN,
            run.entity_id,
            expected_version=run.version,
            next_state=_CANCEL_REQUESTED,
            reason=TransitionReason.CANCEL_REQUESTED,
            evidence_digest=stored.digest,
            artifact_references=(reference,),
        )
        return updated, intent, stored.digest

    def _load_intent(self, run_id: str) -> tuple[CancellationIntent, str]:
        reference_id = _intent_reference_id(run_id)
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload_bytes = self._artifacts.read_reference(reference)
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CancellationStateError(
                run_id, f"cancellation intent is unavailable: {error}") from error
        fields = {"schema", "run_id", "action_id", "reason"}
        if (not isinstance(payload, dict) or set(payload) != fields
                or payload.get("schema") != _INTENT_SCHEMA):
            raise CancellationStateError(run_id, "cancellation intent schema is not exact")
        try:
            intent = CancellationIntent(
                _nonempty(payload["run_id"], "intent.run_id"),
                _nonempty(payload["action_id"], "intent.action_id"),
                _nonempty(payload["reason"], "intent.reason"),
            )
        except (TypeError, ValueError) as error:
            raise CancellationStateError(
                run_id, f"cancellation intent fields are invalid: {error}") from error
        if (intent.run_id != run_id
                or reference.kind is not ArtifactReferenceKind.EVIDENCE
                or payload_bytes != _canonical_bytes(_intent_payload(intent))):
            raise CancellationStateError(run_id, "cancellation intent authority does not match")
        try:
            with self._store._connection_lock:  # noqa: SLF001 - verify ownership
                row = self._store._connection.execute(  # noqa: SLF001
                    "SELECT a.run_id, a.entity_kind, a.entity_id, t.next_state, t.reason, "
                    "t.evidence_digest FROM artifacts a JOIN transitions t "
                    "ON t.transition_id = a.transition_id WHERE a.reference_id = ?",
                    (reference_id,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise CancellationStateError(
                run_id, f"cannot validate cancellation intent binding: {error}") from error
        if (row is None or row["run_id"] != run_id
                or row["entity_kind"] != EntityKind.RUN.value
                or row["entity_id"] != run_id
                or row["next_state"] != _CANCEL_REQUESTED
                or row["reason"] != TransitionReason.CANCEL_REQUESTED.value
                or row["evidence_digest"] != reference.digest):
            raise CancellationStateError(run_id, "cancellation intent is not bound to its request")
        return intent, reference.digest

    def _require_intent(
            self, run: EntityRecord, action: EntityRecord, *,
            reason: str | None = None) -> str:
        intent, digest = self._load_intent(run.entity_id)
        if intent.action_id != action.entity_id:
            raise CancellationIdentityRefusal(
                action.entity_id,
                f"run cancellation targets action {intent.action_id!r}")
        if reason is not None and intent.reason != reason:
            raise CancellationStateError(
                run.entity_id, "repeated cancellation reason differs from durable intent")
        return digest

    def _transition_run(
            self, run: EntityRecord, next_state: str, *,
            intent_digest: str) -> EntityRecord:
        if run.state == next_state:
            return run
        return self._store.record_transition(
            EntityKind.RUN,
            run.entity_id,
            expected_version=run.version,
            next_state=next_state,
            reason=TransitionReason.CANCEL_REQUESTED,
            evidence_digest=intent_digest,
        )

    @staticmethod
    def _result(
            run: EntityRecord, action_id: str, *,
            liveness: LivenessObservation | None = None,
            effect: EffectResult | None = None,
            signal_sent: bool = False,
            principal: LeasePrincipal | None = None) -> CancellationResult:
        return CancellationResult(
            run.entity_id, action_id, run.state, _pending_reason(run.state),
            liveness, effect, signal_sent, principal)

    @staticmethod
    def _unknown_reason(
            liveness: LivenessObservation,
            effect: EffectResult) -> CancelPendingReason:
        if effect.state in _UNCERTAIN_EFFECT_STATES:
            return CancelPendingReason.UNKNOWN_EFFECT
        if (liveness.reason == "identity-mismatch"
                or liveness.reason.startswith("process-identity-unavailable")):
            return CancelPendingReason.IDENTITY_UNKNOWN
        return CancelPendingReason.LIVENESS_UNKNOWN

    def _set_pending(
            self, run: EntityRecord, action: EntityRecord,
            reason: CancelPendingReason, *, intent_digest: str,
            liveness: LivenessObservation,
            effect: EffectResult) -> CancellationResult:
        pending = self._transition_run(
            run, _pending_state(reason), intent_digest=intent_digest)
        return self._result(
            pending, action.entity_id, liveness=liveness, effect=effect)

    def _current_principal(self, action: EntityRecord) -> LeasePrincipal:
        try:
            return self._effects._current_principal(action)  # noqa: SLF001
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CancellationStateError(
                action.entity_id, f"current lease principal is unavailable: {error}") from error

    def _recorded_identity(
            self, run: EntityRecord, action: EntityRecord,
            principal: LeasePrincipal, plan: EffectPlan) -> ProcessIdentity:
        try:
            runtime = supervisor_module._read_runtime(  # noqa: SLF001
                self._supervisor._runtime_path(action.entity_id))  # noqa: SLF001
            launch = supervisor_module._read_launch(  # noqa: SLF001
                self._supervisor._launch_path(action.entity_id))  # noqa: SLF001
            identity = ProcessIdentity.from_payload(runtime["process_identity"])
            supervisor_identity = ProcessIdentity.from_payload(
                runtime["supervisor_identity"])
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CancellationIdentityRefusal(
                action.entity_id,
                f"supervisor process identity is unavailable: {error}") from error
        invocation_digest = plan.spec["invocation_digest"]
        common = {
            "run_id": run.entity_id,
            "job_id": action.parent_job_id,
            "action_id": action.entity_id,
            "owner_token": principal.owner_token,
            "fencing_epoch": principal.fencing_epoch,
            "entity_version": principal.entity_version,
            "invocation_digest": invocation_digest,
        }
        if (plan.run_id != run.entity_id
                or plan.job_id != action.parent_job_id
                or plan.action_id != action.entity_id
                or any(launch.get(key) != value for key, value in common.items())
                or any(runtime.get(key) != value for key, value in common.items())
                or runtime["launch_token"] != launch["launch_token"]
                or launch["project_root"] != str(self._store.project_root)
                or launch["completion_marker_path"]
                != str(self._supervisor._marker_path(action.entity_id))  # noqa: SLF001
                or identity.action_id != action.entity_id
                or identity.supervisor_owner_token != principal.owner_token
                or identity.fencing_epoch != principal.fencing_epoch
                or identity.invocation_digest != invocation_digest
                or identity.resolved_executable != launch["argv"][0]
                or supervisor_identity.action_id != action.entity_id
                or supervisor_identity.supervisor_owner_token != principal.owner_token
                or supervisor_identity.fencing_epoch != principal.fencing_epoch):
            raise CancellationIdentityRefusal(
                action.entity_id,
                "supervisor runtime, launch, process, and principal identities differ")
        return identity

    def _verified_signal_identity(
            self, run: EntityRecord,
            action: EntityRecord,
            plan: EffectPlan) -> tuple[LeasePrincipal, ProcessIdentity]:
        if self._signal_sender is None:
            raise SignalCapabilityUnavailable(action.entity_id)
        principal = self._current_principal(action)
        identity = self._recorded_identity(run, action, principal, plan)
        if (principal.run_id != run.entity_id
                or identity.action_id != action.entity_id
                or identity.supervisor_owner_token != principal.owner_token
                or identity.fencing_epoch != principal.fencing_epoch):
            raise CancellationIdentityRefusal(
                action.entity_id,
                "supervisor process identity does not match the current principal")
        direct = observe_process_identity(identity)
        observed = self._supervisor.probe_action(action.entity_id)
        if (direct.state is not LivenessState.ALIVE
                or observed.state is not LivenessState.ALIVE):
            raise CancellationIdentityRefusal(
                action.entity_id, "process identity is not positively alive")
        try:
            self._leases._guard_operation(  # noqa: SLF001 - no public cancel-signal guard
                principal, "cancel-signal-preflight", lambda: None)
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise CancellationIdentityRefusal(
                action.entity_id, "lease principal is not current at signal time") from error
        return principal, identity

    def _deliver_signal(
            self, run: EntityRecord, action: EntityRecord,
            principal: LeasePrincipal, plan: EffectPlan,
            expected_identity: ProcessIdentity) -> None:
        if self._signal_sender is None:
            raise SignalCapabilityUnavailable(action.entity_id)

        def send_if_still_verified() -> None:
            current = self._recorded_identity(run, action, principal, plan)
            direct = observe_process_identity(current)
            observed = self._supervisor.probe_action(action.entity_id)
            if (current != expected_identity
                    or direct.state is not LivenessState.ALIVE
                    or observed.state is not LivenessState.ALIVE):
                raise CancellationIdentityRefusal(
                    action.entity_id,
                    "supervisor identity changed before signal delivery")
            self._signal_sender(current)

        try:
            self._leases._guard_operation(  # noqa: SLF001 - exact cancel-signal guard
                principal, "cancel-signal", send_if_still_verified)
        except CancellationError:
            raise
        except (LeasePrincipalMismatch, LeasePrincipalUnknown) as error:
            raise CancellationIdentityRefusal(
                action.entity_id, "lease principal is not current at signal time") from error
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SignalDeliveryError(action.entity_id, str(error)) from error

    def _signal_alive(
            self, run: EntityRecord, action: EntityRecord, plan: EffectPlan, *,
            intent_digest: str, liveness: LivenessObservation,
            effect: EffectResult) -> CancellationResult:
        try:
            principal, identity = self._verified_signal_identity(
                run, action, plan)
        except CancellationIdentityRefusal:
            return self._set_pending(
                run, action, CancelPendingReason.IDENTITY_UNKNOWN,
                intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        run = self._transition_run(
            run, _STOPPING, intent_digest=intent_digest)
        try:
            self._deliver_signal(run, action, principal, plan, identity)
        except CancellationIdentityRefusal:
            return self._set_pending(
                run, action, CancelPendingReason.IDENTITY_UNKNOWN,
                intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        return self._result(
            run, action.entity_id, liveness=liveness, effect=effect,
            signal_sent=True, principal=principal)

    def request_cancel(
            self, run_id: str, action_id: str, *, reason: str) -> CancellationResult:
        """Record cancellation intent and optionally signal; never terminalize or clean."""
        request_reason = _nonempty(reason, "reason")
        run, action = self._records(run_id, action_id)
        plan = self._require_runner_plan(action)
        if run.state == "running":
            self._require_action_scope(run.entity_id, action.entity_id)
            run, _intent, intent_digest = self._record_intent(
                run, action, request_reason)
        else:
            allowed = {_CANCEL_REQUESTED, _STOPPING, _CANCELED}
            if run.state not in allowed and _pending_reason(run.state) is None:
                raise CancellationStateRefusal(
                    run.entity_id, "request cancellation", run.state)
            intent_digest = self._require_intent(
                run, action, reason=request_reason)
            self._require_action_scope(
                run.entity_id, action.entity_id,
                intent_digest=intent_digest)
            if run.state == _CANCELED:
                return self._result(run, action.entity_id)

        effect = self._effects.inspect_effect(action.entity_id)
        liveness = self._supervisor.probe_action(action.entity_id)
        if liveness.state is LivenessState.ALIVE:
            return self._signal_alive(
                run, action, plan, intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        if liveness.state is LivenessState.UNKNOWN:
            return self._set_pending(
                run, action, self._unknown_reason(liveness, effect),
                intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        if effect.state in _UNCERTAIN_EFFECT_STATES:
            return self._set_pending(
                run, action, CancelPendingReason.UNKNOWN_EFFECT,
                intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        return self._result(
            run, action.entity_id, liveness=liveness, effect=effect)

    def _terminalize(
            self, run: EntityRecord, action: EntityRecord, *,
            principal: LeasePrincipal, intent_digest: str,
            liveness: LivenessObservation,
            effect: EffectResult) -> CancellationResult:
        if liveness.state is not LivenessState.EXITED:
            raise CancellationNotQuiescent(
                action.entity_id, "positive process exit is not established")
        if effect.state not in _RECONCILED_EFFECT_STATES:
            raise CancellationNotQuiescent(
                action.entity_id, "effect reconciliation is not complete")
        terminal_payload: dict[str, object] = {
            "schema": _TERMINAL_SCHEMA,
            "run_id": run.entity_id,
            "action_id": action.entity_id,
            "intent_digest": intent_digest,
            "action_version": action.version,
            "owner_token": principal.owner_token,
            "fencing_epoch": principal.fencing_epoch,
            "effect_state": effect.state.value,
            "effect_observed_digest": effect.observed_digest,
            "liveness_reason": liveness.reason,
        }
        terminal = self._artifacts.write(_canonical_bytes(terminal_payload))
        reference = ArtifactReference(
            reference_id=_terminal_reference_id(run.entity_id),
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=terminal.digest,
            size=terminal.size,
        )

        def terminal_transition() -> EntityRecord:
            current_run = self._store._load_record(  # noqa: SLF001
                EntityKind.RUN, run.entity_id)
            current_action = self._store._load_record(  # noqa: SLF001
                EntityKind.ACTION, action.entity_id)
            if current_run != run:
                raise CancellationStateError(
                    run.entity_id, "run changed before terminal cancellation CAS")
            if (current_action != action
                    or current_action.state != "completed"
                    or current_action.version != principal.entity_version):
                raise CancellationNotQuiescent(
                    action.entity_id,
                    "completed action is not bound to the current principal")
            action_ids = self._action_ids_locked(run.entity_id)
            if action_ids != (action.entity_id,):
                raise CancellationScopeRefusal(run.entity_id, action_ids)
            if self._supervisor.probe_action(action.entity_id).state is not LivenessState.EXITED:
                raise CancellationNotQuiescent(
                    action.entity_id, "process exit changed before terminal cancellation CAS")
            next_version = current_run.version + 1
            updated = replace(
                current_run, state=_CANCELED, version=next_version)
            try:
                cursor = self._store._connection.execute(  # noqa: SLF001
                    "INSERT INTO transitions(run_id, entity_kind, entity_id, prev_state, "
                    "next_state, entity_version, reason, evidence_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (current_run.run_id, EntityKind.RUN.value, current_run.entity_id,
                     current_run.state, _CANCELED, next_version,
                     TransitionReason.CANCEL_REQUESTED.value, terminal.digest),
                )
                self._store._transaction_fault_point(  # noqa: SLF001
                    "after_transition_insert")
                result = self._store._connection.execute(  # noqa: SLF001
                    "UPDATE runs SET state = ?, version = ?, record_digest = ? "
                    "WHERE run_id = ? AND version = ? AND state = ?",
                    (_CANCELED, next_version, store_module._record_digest(updated),  # noqa: SLF001
                     current_run.entity_id, current_run.version, current_run.state),
                )
                self._store._connection.execute(  # noqa: SLF001
                    "INSERT INTO artifacts(reference_id, run_id, transition_id, entity_kind, "
                    "entity_id, entity_version, reference_kind, digest, size) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (reference.reference_id, current_run.run_id, cursor.lastrowid,
                     EntityKind.RUN.value, current_run.entity_id, next_version,
                     reference.kind.value, reference.digest, reference.size),
                )
                self._store._transaction_fault_point(  # noqa: SLF001
                    "after_artifact_references")
            except sqlite3.DatabaseError as error:
                raise CancellationStateError(
                    run.entity_id, f"terminal cancellation CAS failed: {error}") from error
            if result.rowcount != 1:
                raise CancellationStateError(
                    run.entity_id, "terminal cancellation CAS selected no current run")
            return updated

        canceled = self._leases._guard_operation(  # noqa: SLF001 - atomic run/action tuple CAS
            principal, "cancel-terminal", terminal_transition)
        return self._result(
            canceled, action.entity_id, liveness=liveness,
            effect=effect, principal=principal)

    def _load_terminal_evidence(
            self, run: EntityRecord, action: EntityRecord,
            intent_digest: str) -> tuple[dict[str, object], str]:
        reference_id = _terminal_reference_id(run.entity_id)
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload_bytes = self._artifacts.read_reference(reference)
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CancellationStateError(
                run.entity_id,
                f"terminal cancellation evidence is unavailable: {error}") from error
        fields = {
            "schema", "run_id", "action_id", "intent_digest", "action_version",
            "owner_token", "fencing_epoch", "effect_state",
            "effect_observed_digest", "liveness_reason",
        }
        if (not isinstance(payload, dict) or set(payload) != fields
                or payload.get("schema") != _TERMINAL_SCHEMA
                or payload.get("run_id") != run.entity_id
                or payload.get("action_id") != action.entity_id
                or payload.get("intent_digest") != intent_digest
                or payload.get("action_version") != action.version
                or payload.get("effect_state")
                not in {state.value for state in _RECONCILED_EFFECT_STATES}
                or not isinstance(payload.get("owner_token"), str)
                or not payload["owner_token"]
                or isinstance(payload.get("fencing_epoch"), bool)
                or not isinstance(payload.get("fencing_epoch"), int)
                or payload["fencing_epoch"] < 1
                or not isinstance(payload.get("liveness_reason"), str)
                or not payload["liveness_reason"]
                or payload_bytes != _canonical_bytes(payload)):
            raise CancellationStateError(
                run.entity_id, "terminal cancellation evidence is not exact")
        try:
            with self._store._connection_lock:  # noqa: SLF001
                row = self._store._connection.execute(  # noqa: SLF001
                    "SELECT a.run_id, a.entity_kind, a.entity_id, a.entity_version, "
                    "t.next_state, t.reason, t.evidence_digest FROM artifacts a "
                    "JOIN transitions t ON t.transition_id = a.transition_id "
                    "WHERE a.reference_id = ?",
                    (reference_id,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise CancellationStateError(
                run.entity_id,
                f"cannot validate terminal evidence binding: {error}") from error
        if (reference.kind is not ArtifactReferenceKind.EVIDENCE
                or row is None or row["run_id"] != run.entity_id
                or row["entity_kind"] != EntityKind.RUN.value
                or row["entity_id"] != run.entity_id
                or row["entity_version"] != run.version
                or row["next_state"] != _CANCELED
                or row["reason"] != TransitionReason.CANCEL_REQUESTED.value
                or row["evidence_digest"] != reference.digest):
            raise CancellationStateError(
                run.entity_id,
                "terminal cancellation evidence is not bound to the canceled transition")
        return payload, reference.digest

    def resume_cancel(
            self, run_id: str, action_id: str, *,
            ttl_seconds: float = 30) -> CancellationResult:
        """Reconcile only a positively exited runner, then terminalize atomically."""
        run, action = self._records(run_id, action_id)
        intent_digest = self._require_intent(run, action)
        plan = self._require_runner_plan(action)
        self._require_action_scope(
            run.entity_id, action.entity_id,
            intent_digest=intent_digest)
        if run.state == _CANCELED:
            self._load_terminal_evidence(run, action, intent_digest)
            return self._result(run, action.entity_id)
        if (run.state not in {_CANCEL_REQUESTED, _STOPPING}
                and _pending_reason(run.state) is None):
            raise CancellationStateRefusal(
                run.entity_id, "resume cancellation", run.state)

        effect = self._effects.inspect_effect(action.entity_id)
        liveness = self._supervisor.probe_action(action.entity_id)
        if liveness.state is LivenessState.ALIVE:
            return self._signal_alive(
                run, action, plan, intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        if liveness.state is LivenessState.UNKNOWN:
            return self._set_pending(
                run, action, self._unknown_reason(liveness, effect),
                intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        if effect.state is EffectResultState.EXITED_UNRECONCILED:
            reconciled = self._effects.reconcile_actions(
                [action.entity_id], ttl_seconds=ttl_seconds,
                quiescence_probe=self._supervisor.quiescence_probe)
            if len(reconciled) != 1:
                raise CancellationNotQuiescent(
                    action.entity_id, "effect reconcile returned an ambiguous result")
            effect = reconciled[0]
            liveness = self._supervisor.probe_action(action.entity_id)
            if liveness.state is not LivenessState.EXITED:
                reason = (
                    CancelPendingReason.IDENTITY_UNKNOWN
                    if liveness.reason == "identity-mismatch"
                    else CancelPendingReason.LIVENESS_UNKNOWN)
                return self._set_pending(
                    run, action, reason, intent_digest=intent_digest,
                    liveness=liveness, effect=effect)
        if effect.state not in _RECONCILED_EFFECT_STATES:
            return self._set_pending(
                run, action, CancelPendingReason.UNKNOWN_EFFECT,
                intent_digest=intent_digest,
                liveness=liveness, effect=effect)
        action = self._store.get_entity(EntityKind.ACTION, action.entity_id)
        if action.state != "completed":
            raise CancellationNotQuiescent(
                action.entity_id, "effect result is not bound to a completed action")
        principal = self._current_principal(action)
        return self._terminalize(
            run, action, principal=principal,
            intent_digest=intent_digest,
            liveness=liveness, effect=effect)

    def _cleanup_payload(
            self, run: EntityRecord, action: EntityRecord, *,
            cleanup_action_id: str, intent_digest: str,
            terminal_digest: str) -> dict[str, object]:
        if self._cleanup_executor_id is None:
            raise CleanupRefused(
                action.entity_id, "engine-owned cleanup executor is unavailable")
        return {
            "schema": _CLEANUP_PLAN_SCHEMA,
            "run_id": run.entity_id,
            "source_action_id": action.entity_id,
            "cleanup_action_id": cleanup_action_id,
            "intent_digest": intent_digest,
            "terminal_digest": terminal_digest,
            "executor_id": self._cleanup_executor_id,
        }

    def _load_cleanup_plan(
            self, cleanup: EntityRecord, run: EntityRecord,
            action: EntityRecord, *, intent_digest: str,
            terminal_digest: str) -> None:
        payload = self._cleanup_payload(
            run, action, cleanup_action_id=cleanup.entity_id,
            intent_digest=intent_digest, terminal_digest=terminal_digest)
        reference_id = _cleanup_reference_id(cleanup.entity_id)
        try:
            reference = self._store.get_artifact_reference(reference_id)
            payload_bytes = self._artifacts.read_reference(reference)
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CancellationStateError(
                cleanup.entity_id,
                f"cleanup plan evidence is unavailable: {error}") from error
        if payload_bytes != _canonical_bytes(payload):
            raise CancellationStateError(
                cleanup.entity_id, "cleanup plan differs from the engine-owned adapter")
        try:
            with self._store._connection_lock:  # noqa: SLF001
                row = self._store._connection.execute(  # noqa: SLF001
                    "SELECT a.run_id, a.entity_kind, a.entity_id, a.entity_version, "
                    "t.next_state, t.reason, t.evidence_digest FROM artifacts a "
                    "JOIN transitions t ON t.transition_id = a.transition_id "
                    "WHERE a.reference_id = ?",
                    (reference_id,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise CancellationStateError(
                cleanup.entity_id,
                f"cannot validate cleanup plan binding: {error}") from error
        if (reference.kind is not ArtifactReferenceKind.EVIDENCE
                or row is None or row["run_id"] != run.entity_id
                or row["entity_kind"] != EntityKind.ACTION.value
                or row["entity_id"] != cleanup.entity_id
                or row["entity_version"] != 1
                or row["next_state"] != _CLEANUP_READY
                or row["reason"] != TransitionReason.CANCEL_REQUESTED.value
                or row["evidence_digest"] != reference.digest):
            raise CancellationStateError(
                cleanup.entity_id,
                "cleanup plan is not bound to its dedicated action")

    def _maybe_cleanup_action(self, cleanup_action_id: str) -> EntityRecord | None:
        try:
            return self._store.get_entity(EntityKind.ACTION, cleanup_action_id)
        except RecordNotFoundError:
            return None

    def _ensure_cleanup_action(
            self, run: EntityRecord, action: EntityRecord, *,
            intent_digest: str, terminal_digest: str) -> EntityRecord:
        cleanup_action_id = _cleanup_action_id(intent_digest)
        cleanup = self._maybe_cleanup_action(cleanup_action_id)
        if cleanup is None:
            try:
                cleanup = self._store.create_action(
                    run.entity_id, action.parent_job_id or "",
                    action.parent_attempt_id or "", cleanup_action_id,
                    initial_state="planned")
            except AppendOnlyConflict:
                cleanup = self._store.get_entity(
                    EntityKind.ACTION, cleanup_action_id)
        if (cleanup.run_id != run.entity_id
                or cleanup.parent_job_id != action.parent_job_id
                or cleanup.parent_attempt_id != action.parent_attempt_id):
            raise CleanupRefused(
                action.entity_id, "cleanup action belongs to different source lineage")
        if cleanup.state == "planned" and cleanup.version == 0:
            payload = self._cleanup_payload(
                run, action, cleanup_action_id=cleanup.entity_id,
                intent_digest=intent_digest, terminal_digest=terminal_digest)
            stored = self._artifacts.write(_canonical_bytes(payload))
            reference = ArtifactReference(
                reference_id=_cleanup_reference_id(cleanup.entity_id),
                kind=ArtifactReferenceKind.EVIDENCE,
                digest=stored.digest,
                size=stored.size,
            )
            try:
                cleanup = self._store.record_transition(
                    EntityKind.ACTION, cleanup.entity_id,
                    expected_version=cleanup.version,
                    next_state=_CLEANUP_READY,
                    reason=TransitionReason.CANCEL_REQUESTED,
                    evidence_digest=stored.digest,
                    artifact_references=(reference,),
                )
            except (AppendOnlyConflict, EntityVersionConflict):
                cleanup = self._store.get_entity(
                    EntityKind.ACTION, cleanup.entity_id)
        if cleanup.state not in {
                _CLEANUP_READY, _CLEANUP_EXECUTING, _CLEANUP_COMPLETED}:
            raise CleanupRefused(
                action.entity_id,
                f"cleanup action has invalid state {cleanup.state!r}")
        self._load_cleanup_plan(
            cleanup, run, action, intent_digest=intent_digest,
            terminal_digest=terminal_digest)
        return cleanup

    def _cleanup_principal(
            self, cleanup: EntityRecord, *,
            ttl_seconds: float) -> tuple[EntityRecord, LeasePrincipal]:
        if cleanup.state == _CLEANUP_READY:
            try:
                principal = self._leases.claim(
                    cleanup.entity_id,
                    expected_entity_version=cleanup.version,
                    ttl_seconds=ttl_seconds)
                return cleanup, principal
            except LeaseAlreadyClaimed:
                cleanup = self._store.get_entity(
                    EntityKind.ACTION, cleanup.entity_id)
        return cleanup, self._current_principal(cleanup)

    @staticmethod
    def _terminal_principal(
            action: EntityRecord, payload: dict[str, object],
            principal: LeasePrincipal) -> None:
        if (payload["action_version"] != action.version
                or payload["owner_token"] != principal.owner_token
                or payload["fencing_epoch"] != principal.fencing_epoch
                or principal.entity_version != action.version):
            raise CleanupRefused(
                action.entity_id,
                "terminal evidence does not match the current source principal")

    def _start_cleanup(
            self, run: EntityRecord, action: EntityRecord,
            source_principal: LeasePrincipal, cleanup: EntityRecord,
            cleanup_principal: LeasePrincipal) -> tuple[EntityRecord, LeasePrincipal]:
        def start() -> EntityRecord:
            current_run = self._store._load_record(  # noqa: SLF001
                EntityKind.RUN, run.entity_id)
            current_action = self._store._load_record(  # noqa: SLF001
                EntityKind.ACTION, action.entity_id)
            current_cleanup = self._store._load_record(  # noqa: SLF001
                EntityKind.ACTION, cleanup.entity_id)
            action_ids = self._action_ids_locked(run.entity_id)
            if (current_run != run or current_run.state != _CANCELED
                    or current_action != action
                    or current_action.state != "completed"
                    or current_cleanup != cleanup
                    or set(action_ids) != {action.entity_id, cleanup.entity_id}):
                raise CleanupRefused(
                    action.entity_id, "run or action scope changed before cleanup WAI")
            if self._supervisor.probe_action(
                    action.entity_id).state is not LivenessState.EXITED:
                raise CleanupRefused(
                    action.entity_id, "process exit changed before cleanup WAI")
            return self._store._record_guarded_action_transition(  # noqa: SLF001
                cleanup.entity_id,
                expected_version=cleanup.version,
                owner_token=cleanup_principal.owner_token,
                fencing_epoch=cleanup_principal.fencing_epoch,
                next_state=_CLEANUP_EXECUTING,
                reason=TransitionReason.PROCESS_STARTED,
            )

        updated = self._leases.guard_cleanup(source_principal, start)
        return updated, replace(
            cleanup_principal, entity_version=updated.version)

    def _complete_cleanup(
            self, run: EntityRecord, action: EntityRecord,
            source_principal: LeasePrincipal, cleanup: EntityRecord,
            cleanup_principal: LeasePrincipal) -> tuple[EntityRecord, LeasePrincipal]:
        def complete() -> EntityRecord:
            current_run = self._store._load_record(  # noqa: SLF001
                EntityKind.RUN, run.entity_id)
            current_action = self._store._load_record(  # noqa: SLF001
                EntityKind.ACTION, action.entity_id)
            current_cleanup = self._store._load_record(  # noqa: SLF001
                EntityKind.ACTION, cleanup.entity_id)
            action_ids = self._action_ids_locked(run.entity_id)
            if (current_run != run or current_run.state != _CANCELED
                    or current_action != action
                    or current_action.state != "completed"
                    or current_cleanup != cleanup
                    or set(action_ids) != {action.entity_id, cleanup.entity_id}):
                raise CleanupRefused(
                    action.entity_id,
                    "run or action scope changed before cleanup completion")
            return self._store._record_guarded_action_transition(  # noqa: SLF001
                cleanup.entity_id,
                expected_version=cleanup.version,
                owner_token=cleanup_principal.owner_token,
                fencing_epoch=cleanup_principal.fencing_epoch,
                next_state=_CLEANUP_COMPLETED,
                reason=TransitionReason.COMPLETED,
            )

        updated = self._leases.guard_cleanup(source_principal, complete)
        return updated, replace(
            cleanup_principal, entity_version=updated.version)

    def _cleanup_lock_path(self, intent_digest: str) -> Path:
        return (
            self._store.project_root / ".waystone"
            / ("cancel-cleanup-" + intent_digest.split(":", 1)[1] + ".lock")
        )

    def cleanup(
            self, run_id: str, action_id: str, *,
            ttl_seconds: float = 30) -> CleanupResult:
        """Run a dedicated idempotent cleanup action after the full AND gate."""
        run, action = self._records(run_id, action_id)
        intent_digest = self._require_intent(run, action)
        self._require_runner_plan(action)
        if run.state != _CANCELED:
            raise CleanupRefused(action.entity_id, "run is not terminally canceled")
        terminal_payload, terminal_digest = self._load_terminal_evidence(
            run, action, intent_digest)
        self._require_action_scope(
            run.entity_id, action.entity_id,
            intent_digest=intent_digest)
        if action.state != "completed":
            raise CleanupRefused(
                action.entity_id, "action is not durably completed")
        if self._cleanup_executor is None or self._cleanup_executor_id is None:
            raise CleanupRefused(
                action.entity_id, "engine-owned cleanup executor is unavailable")
        source_principal = self._current_principal(action)
        self._terminal_principal(action, terminal_payload, source_principal)

        cleanup_action_id = _cleanup_action_id(intent_digest)
        existing = self._maybe_cleanup_action(cleanup_action_id)
        if existing is None or existing.state != _CLEANUP_COMPLETED:
            effect = self._effects.inspect_effect(action.entity_id)
            liveness = self._supervisor.probe_action(action.entity_id)
            if effect.state not in _RECONCILED_EFFECT_STATES:
                raise CleanupRefused(
                    action.entity_id, "effect reconciliation is not complete")
            if liveness.state is not LivenessState.EXITED:
                raise CleanupRefused(
                    action.entity_id, "positive process exit is not established")
            self._leases.guard_cleanup(source_principal, lambda: None)

        cleanup = self._ensure_cleanup_action(
            run, action, intent_digest=intent_digest,
            terminal_digest=terminal_digest)
        cleanup, cleanup_principal = self._cleanup_principal(
            cleanup, ttl_seconds=ttl_seconds)
        lock_path = self._cleanup_lock_path(intent_digest)
        with self._leases.advisory_lock(lock_path, cleanup_principal):
            run, action = self._records(run.entity_id, action.entity_id)
            intent_digest = self._require_intent(run, action)
            terminal_payload, terminal_digest = self._load_terminal_evidence(
                run, action, intent_digest)
            self._require_action_scope(
                run.entity_id, action.entity_id,
                intent_digest=intent_digest)
            cleanup = self._store.get_entity(
                EntityKind.ACTION, cleanup.entity_id)
            self._load_cleanup_plan(
                cleanup, run, action, intent_digest=intent_digest,
                terminal_digest=terminal_digest)
            source_principal = self._current_principal(action)
            self._terminal_principal(
                action, terminal_payload, source_principal)
            cleanup_principal = self._current_principal(cleanup)
            self._leases.guard_cleanup(source_principal, lambda: None)
            if cleanup.state == _CLEANUP_COMPLETED:
                self._leases.guard_cleanup(cleanup_principal, lambda: None)
                return CleanupResult(
                    run.entity_id, action.entity_id, cleanup.entity_id,
                    CleanupDisposition.NOOP)
            if self._supervisor.probe_action(
                    action.entity_id).state is not LivenessState.EXITED:
                raise CleanupRefused(
                    action.entity_id, "positive process exit is not established")
            if cleanup.state == _CLEANUP_READY:
                cleanup, cleanup_principal = self._start_cleanup(
                    run, action, source_principal,
                    cleanup, cleanup_principal)
            if cleanup.state != _CLEANUP_EXECUTING:
                raise CleanupRefused(
                    action.entity_id,
                    f"cleanup action cannot execute from {cleanup.state!r}")
            self._leases.guard_cleanup(cleanup_principal, lambda: None)
            plan = CleanupPlan(
                cleanup_id=f"cleanup:{terminal_digest.split(':', 1)[1]}",
                run_id=run.entity_id,
                action_id=action.entity_id,
                principal=source_principal,
                cleanup_action_id=cleanup.entity_id,
                cleanup_principal=cleanup_principal,
                executor_id=self._cleanup_executor_id,
            )
            try:
                self._cleanup_executor(plan)
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise CleanupExecutionError(cleanup.entity_id, str(error)) from error
            cleanup, _cleanup_principal = self._complete_cleanup(
                run, action, source_principal,
                cleanup, cleanup_principal)
            return CleanupResult(
                run.entity_id, action.entity_id, cleanup.entity_id,
                CleanupDisposition.CLEANED)
