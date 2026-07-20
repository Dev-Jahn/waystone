"""One-task run-engine composition facade.

This module owns no retry policy and implements no effect, verification, or
cancellation protocol.  It wires the frozen inputs and typed results exposed by
the neighbouring run modules into the M1-B one-task vertical path.
"""
from __future__ import annotations

import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Mapping

from waystone.core import WorkflowError
from waystone.jobs.domain import Role
from waystone.runs.cancel import CancellationEngine, CancellationResult
from waystone.runs.effects import (
    EffectEngine,
    EffectKind,
    EffectResultState,
    EffectStateRefusal,
    GitRefEffect,
    RunnerExecutionEffect,
)
from waystone.runs.lease import LeaseManager
from waystone.runs.observe import (
    RunSnapshot,
    json_projection,
    render_human,
    snapshot_run,
    watch_run,
)
from waystone.runs.preflight import (
    CapabilitySet,
    DispatchReady,
    MaterializedToolchain,
    RunnerContext,
    RunnerProof,
    VerificationPlan,
    VerificationPlanDefinition,
    freeze_verification_plan,
    load_dispatch_ready,
    preflight_for_dispatch,
)
from waystone.runs.spec import RunSpec, load_run_spec, plan_one_task_run
from waystone.runs.store import (
    EntityKind,
    FilesystemInfo,
    RecordNotFoundError,
    RunStore,
    TransitionReason,
)
from waystone.runs.supervisor import LivenessState, RunnerInvocation, Supervisor
from waystone.runs.transport import (
    ActionPlanRefusal,
    ActionTransport,
    EngineExecutorUnavailable,
    RunNotActionable,
)
from waystone.runs.verify import (
    ActorIdentity,
    ApplyResult,
    DecisionInput,
    DecisionOutcome,
    EngineCheckExecutor,
    IntegrationDecision,
    VerifierAdapter,
    VerifierEvidence,
    apply_integration_decision,
    execute_verifier,
    record_integration_decision,
)
from waystone.runs import store as store_module


class EngineAssemblyError(WorkflowError):
    """The requested vertical path lacks an explicit composition input."""

    code = "run_engine_assembly_error"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class EngineConfigurationUnavailable(EngineAssemblyError):
    code = "run_engine_configuration_unavailable"


class EngineBindingRefusal(EngineAssemblyError):
    code = "run_engine_binding_refused"


class ReadOnlyStoreUnavailable(EngineAssemblyError):
    code = "run_status_unavailable"


class CancelReason(str, Enum):
    """The M1-B CLI's single owner-authored cancellation reason."""

    USER_REQUESTED = "user-requested"


@dataclass(frozen=True)
class PreflightInputs:
    capabilities: CapabilitySet
    materialized_toolchains: tuple[MaterializedToolchain, ...]
    runner_context: RunnerContext
    runner_proof: RunnerProof


@dataclass(frozen=True)
class RunAssembly:
    """Explicit adapters needed to execute the otherwise protocol-only slice."""

    verification_plan: VerificationPlanDefinition
    preflight_inputs: Callable[[VerificationPlan], PreflightInputs]
    runner_invocations: Callable[[DispatchReady], Mapping[str, RunnerInvocation]]
    result_ref: str
    worker_actor_id: str
    verifier_actor: ActorIdentity
    coordinator_actor: ActorIdentity
    check_executor: EngineCheckExecutor
    verifier_adapter: VerifierAdapter
    decision_input: Callable[[VerifierEvidence, ActorIdentity], DecisionInput]

    def __post_init__(self) -> None:
        if not isinstance(self.verification_plan, VerificationPlanDefinition):
            raise TypeError("verification_plan must be a VerificationPlanDefinition")
        for value, label in (
                (self.preflight_inputs, "preflight_inputs"),
                (self.runner_invocations, "runner_invocations"),
                (self.check_executor, "check_executor"),
                (self.decision_input, "decision_input")):
            if not callable(value):
                raise TypeError(f"{label} must be callable")
        if not isinstance(self.result_ref, str) or not self.result_ref.startswith("refs/"):
            raise ValueError("result_ref must be a full refs/* name")
        if not isinstance(self.worker_actor_id, str) or not self.worker_actor_id.strip():
            raise ValueError("worker_actor_id must be non-empty")
        if (not isinstance(self.verifier_actor, ActorIdentity)
                or self.verifier_actor.role is not Role.VERIFIER):
            raise ValueError("verifier_actor must have the verifier role")
        if (not isinstance(self.coordinator_actor, ActorIdentity)
                or self.coordinator_actor.role is not Role.COORDINATOR):
            raise ValueError("coordinator_actor must have the coordinator role")
        if not isinstance(self.verifier_adapter, VerifierAdapter):
            raise TypeError("verifier_adapter must be a VerifierAdapter")


@dataclass(frozen=True)
class StartResult:
    run_id: str
    dispatch: Mapping[str, object]


@dataclass(frozen=True)
class CompletionResult:
    run_id: str
    verifier: VerifierEvidence
    decision: IntegrationDecision
    applied: ApplyResult


@dataclass(frozen=True)
class ResumeResult:
    run_id: str
    dispatch: Mapping[str, object] | None = None
    cancellation: CancellationResult | None = None
    completion: CompletionResult | None = None


_CANCELLATION_STATES = {
    "cancel-requested",
    "stopping",
    "cancel-pending(reason=identity-unknown)",
    "cancel-pending(reason=liveness-unknown)",
    "cancel-pending(reason=unknown-effect)",
    "canceled",
}


def _attempt_id(run_id: str) -> str:
    return f"{run_id}:attempt"


def _runner_action_id(run_id: str, check_id: str) -> str:
    return f"{run_id}:runner:{check_id}"


def _verify_action_id(run_id: str) -> str:
    return f"{run_id}:verify"


def _decision_action_id(run_id: str) -> str:
    return f"{run_id}:decision"


def _target_action_id(run_id: str) -> str:
    return f"{run_id}:integration-target"


def _apply_action_id(run_id: str) -> str:
    return f"{run_id}:apply"


def _target_ref(run_id: str) -> str:
    return f"refs/waystone/integration/{run_id}"


def _regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ReadOnlyStoreUnavailable(f"cannot inspect {label}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReadOnlyStoreUnavailable(f"{label} is not a regular file")


@contextmanager
def open_read_only_store(project_root: Path) -> Iterator[RunStore]:
    """Open a copied SQLite/WAL snapshot without touching project state bytes."""
    root = Path(project_root).resolve(strict=True)
    marker = root / ".waystone.yml"
    _regular_file(marker, "project marker")
    state = root / ".waystone"
    database = state / "state.db"
    _regular_file(database, "runtime database")

    with tempfile.TemporaryDirectory(prefix="waystone-status-") as temporary:
        snapshot_root = Path(temporary)
        copied_database = snapshot_root / "state.db"
        source = None
        connection = None
        try:
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                try:
                    info = sidecar.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ReadOnlyStoreUnavailable(
                        f"runtime database sidecar {sidecar.name} is not a regular file")
            source = sqlite3.connect(
                database.as_uri() + "?mode=ro",
                uri=True,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            source.execute("PRAGMA query_only=ON")
            connection = sqlite3.connect(
                copied_database,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            source.backup(connection)
            source.close()
            source = None
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            version = store_module._existing_schema_version(connection)  # noqa: SLF001
            store_module._validate_schema(connection)  # noqa: SLF001
            connection.set_authorizer(store_module._store_authorizer)  # noqa: SLF001
        except (OSError, sqlite3.DatabaseError, WorkflowError) as error:
            if source is not None:
                source.close()
            if connection is not None:
                connection.close()
            raise ReadOnlyStoreUnavailable(str(error)) from error
        store = RunStore(
            root,
            copied_database,
            connection,
            FilesystemInfo("read-only-snapshot", snapshot_root, writable=False),
            version,
            _token=store_module._RUN_STORE_CONSTRUCTION_TOKEN,  # noqa: SLF001
        )
        try:
            yield store
        finally:
            store.close()


class RunEngine:
    """Compose the existing M1-B modules for one frozen task run."""

    def __init__(self, project_root: Path, assembly: RunAssembly | None = None):
        self.root = Path(project_root).resolve(strict=True)
        self.assembly = assembly

    def _require_assembly(self) -> RunAssembly:
        if self.assembly is None:
            raise EngineConfigurationUnavailable(
                "no explicit VerificationPlan/backend assembly is configured; "
                "legacy delegate execution is not a fallback")
        return self.assembly

    @staticmethod
    def _ensure_attempt(store: RunStore, spec: RunSpec):
        identity = _attempt_id(spec.run_id)
        try:
            return store.get_entity(EntityKind.ATTEMPT, identity)
        except RecordNotFoundError:
            return store.create_attempt(spec.run_id, spec.job_id, identity, initial_state="running")

    def _invocations(self, dispatch: DispatchReady) -> dict[str, RunnerInvocation]:
        assembly = self._require_assembly()
        supplied = dict(assembly.runner_invocations(dispatch))
        expected = {action.prepared_input_digest: action for action in dispatch.engine_actions}
        if set(supplied) != set(expected):
            raise EngineBindingRefusal(
                "runner invocation set does not exactly match frozen engine actions")
        normalized: dict[str, RunnerInvocation] = {}
        for digest, action in expected.items():
            invocation = supplied[digest]
            if not isinstance(invocation, RunnerInvocation):
                raise EngineBindingRefusal(
                    f"invocation {digest!r} is not a RunnerInvocation")
            if invocation.argv != action.command:
                raise EngineBindingRefusal(
                    f"check {action.check_id!r} invocation differs from its frozen command")
            if action.child_environment:
                raise EngineBindingRefusal(
                    f"check {action.check_id!r} requires child environment values that "
                    "RunnerInvocation cannot carry")
            try:
                invocation.cwd.resolve(strict=True)
            except OSError as error:
                raise EngineBindingRefusal(
                    f"check {action.check_id!r} cwd is unavailable: {error}") from error
            normalized[digest] = invocation
        return normalized

    def _runtime(self, store: RunStore, invocations: Mapping[str, RunnerInvocation]):
        leases = LeaseManager(store)
        supervisor = Supervisor(store, leases, invocations=invocations)
        effects = EffectEngine(
            store,
            leases,
            runner_executor=supervisor.runner_executor,
            runner_identity_verifier=supervisor.runner_identity_verifier,
        )
        return leases, supervisor, effects, ActionTransport(store, effects)

    def _materialize_dispatch(self, store: RunStore, spec: RunSpec,
                              dispatch: DispatchReady) -> None:
        attempt = self._ensure_attempt(store, spec)
        _leases, _supervisor, effects, _transport = self._runtime(
            store, self._invocations(dispatch))
        for action in dispatch.engine_actions:
            identity = _runner_action_id(spec.run_id, action.check_id)
            try:
                existing = store.get_entity(EntityKind.ACTION, identity)
            except RecordNotFoundError:
                effects.plan_effect(
                    spec.run_id,
                    spec.job_id,
                    attempt.entity_id,
                    identity,
                    RunnerExecutionEffect(action.prepared_input_digest),
                )
            else:
                plan = effects._load_plan(identity)  # noqa: SLF001 - composition validation
                if (existing.run_id != spec.run_id
                        or plan.kind is not EffectKind.RUNNER_EXECUTION
                        or plan.spec.get("invocation_digest") != action.prepared_input_digest):
                    raise EngineBindingRefusal(
                        f"runner action {identity!r} differs from frozen preflight")

    def start(self, task_id: str) -> StartResult:
        assembly = self._require_assembly()
        spec = plan_one_task_run(task_id, start=self.root)
        plan = freeze_verification_plan(
            spec.run_id, assembly.verification_plan, start=self.root)
        inputs = assembly.preflight_inputs(plan)
        if not isinstance(inputs, PreflightInputs):
            raise EngineBindingRefusal("preflight_inputs did not return PreflightInputs")
        dispatch = preflight_for_dispatch(
            spec.run_id,
            capabilities=inputs.capabilities,
            materialized_toolchains=inputs.materialized_toolchains,
            current_runner_context=inputs.runner_context,
            reusable_runner_proof=inputs.runner_proof,
            start=self.root,
        )
        with RunStore.open(self.root) as store:
            self._materialize_dispatch(store, spec, dispatch)
            branch = self._actions_next_open(store, spec.run_id, dispatch=dispatch)
        return StartResult(spec.run_id, branch)

    def _runner_actions(self, store: RunStore, run_id: str):
        with store._connection_lock:  # noqa: SLF001 - package composition query
            rows = store._connection.execute(  # noqa: SLF001
                "SELECT action_id FROM actions WHERE run_id = ? AND state != 'completed' "
                "ORDER BY action_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            action = store.get_entity(EntityKind.ACTION, row["action_id"])
            try:
                reference = store.get_artifact_reference(f"effect-plan:{action.entity_id}")
            except RecordNotFoundError:
                continue
            del reference
            result.append(action)
        return tuple(result)

    def _actions_next_open(self, store: RunStore, run_id: str, *,
                           dispatch: DispatchReady | None = None) -> Mapping[str, object]:
        invocations: Mapping[str, RunnerInvocation] = {}
        if dispatch is not None:
            invocations = self._invocations(dispatch)
        leases, supervisor, effects, transport = self._runtime(store, invocations)
        del leases
        runner_seen = False
        for action in self._runner_actions(store, run_id):
            plan = effects._load_plan(action.entity_id)  # noqa: SLF001
            if plan.kind is not EffectKind.RUNNER_EXECUTION:
                continue
            runner_seen = True
            if action.state == "planned":
                if plan.spec["invocation_digest"] not in invocations:
                    raise EngineExecutorUnavailable(
                        "planned runner has no exact detached Supervisor invocation")
                claimed = effects.claim_effect(plan, ttl_seconds=30)
                effects.execute_effect(claimed)
                current_run = store.get_run(run_id)
                return {
                    "action": None,
                    "engine": "busy",
                    "poll_after_s": 1,
                    "run_state": current_run.state,
                }
            if action.state in {"claimed", "effect", "observed"}:
                observation = supervisor.probe_action(action.entity_id)
                if observation.state is LivenessState.ALIVE:
                    current_run = store.get_run(run_id)
                    return {
                        "action": None,
                        "engine": "busy",
                        "poll_after_s": 1,
                        "run_state": current_run.state,
                    }
                if observation.state is LivenessState.EXITED:
                    effects.reconcile_actions(
                        (action.entity_id,),
                        quiescence_probe=supervisor.quiescence_probe,
                    )
                    continue
                raise RunNotActionable(
                    f"runner {action.entity_id!r} liveness is unknown: {observation.reason}")
        if runner_seen:
            current_run = store.get_run(run_id)
            return {
                "action": None,
                "engine": "busy",
                "poll_after_s": 1,
                "run_state": current_run.state,
            }
        return transport.actions_next(run_id)

    def actions_next(self, run_id: str) -> Mapping[str, object]:
        dispatch = None
        if self.assembly is not None:
            dispatch = load_dispatch_ready(run_id, start=self.root)
        with RunStore.open(self.root) as store:
            return self._actions_next_open(store, run_id, dispatch=dispatch)

    def actions_submit(self, action_id: str, payload: Mapping[str, object]):
        with RunStore.open(self.root) as store:
            _leases, _supervisor, _effects, transport = self._runtime(store, {})
            return transport.submit(action_id, payload)

    def _primary_runner_action(self, store: RunStore, run_id: str) -> str:
        actions = []
        for action in self._runner_actions(store, run_id):
            try:
                leases, supervisor, effects, _transport = self._runtime(store, {})
                del leases, supervisor
                plan = effects._load_plan(action.entity_id)  # noqa: SLF001
                if plan.kind is EffectKind.RUNNER_EXECUTION:
                    actions.append(action.entity_id)
            except RecordNotFoundError:
                continue
        if len(actions) != 1:
            raise ActionPlanRefusal(
                f"run {run_id!r} does not have exactly one cancellable runner action")
        return actions[0]

    def cancel(self, run_id: str, reason: CancelReason) -> CancellationResult:
        typed_reason = CancelReason(reason)
        dispatch = None
        if self.assembly is not None:
            dispatch = load_dispatch_ready(run_id, start=self.root)
        invocations = {} if dispatch is None else self._invocations(dispatch)
        with RunStore.open(self.root) as store:
            action_id = self._primary_runner_action(store, run_id)
            run = store.get_run(run_id)
            if run.state == "dispatch-ready":
                store.record_transition(
                    EntityKind.RUN,
                    run_id,
                    expected_version=run.version,
                    next_state="running",
                    reason=TransitionReason.PROCESS_STARTED,
                )
            leases, supervisor, effects, _transport = self._runtime(store, invocations)
            cancellation = CancellationEngine(store, effects, leases, supervisor)
            return cancellation.request_cancel(
                run_id, action_id, reason=typed_reason.value)

    def _resume_cancel(self, store: RunStore, run_id: str,
                       invocations: Mapping[str, RunnerInvocation]) -> CancellationResult:
        action_id = self._primary_runner_action(store, run_id)
        leases, supervisor, effects, _transport = self._runtime(store, invocations)
        cancellation = CancellationEngine(store, effects, leases, supervisor)
        return cancellation.resume_cancel(run_id, action_id)

    def _complete(self, spec: RunSpec) -> CompletionResult:
        assembly = self._require_assembly()
        attempt_id = _attempt_id(spec.run_id)
        with RunStore.open(self.root) as store:
            try:
                store.get_entity(EntityKind.ATTEMPT, attempt_id)
            except RecordNotFoundError:
                store.create_attempt(
                    spec.run_id, spec.job_id, attempt_id, initial_state="running")
        evidence = execute_verifier(
            spec.run_id,
            attempt_id,
            _verify_action_id(spec.run_id),
            self.root,
            assembly.result_ref,
            assembly.worker_actor_id,
            assembly.verifier_actor,
            assembly.check_executor,
            assembly.verifier_adapter,
            start=self.root,
        )
        decision_input = assembly.decision_input(evidence, assembly.coordinator_actor)
        if not isinstance(decision_input, DecisionInput):
            raise EngineBindingRefusal("decision_input did not return DecisionInput")
        decision = record_integration_decision(
            spec.run_id,
            attempt_id,
            _decision_action_id(spec.run_id),
            decision_input,
            start=self.root,
        )
        if decision.outcome is not DecisionOutcome.ACCEPT:
            raise EngineBindingRefusal("rejected decision cannot enter apply")

        target = _target_ref(spec.run_id)
        with RunStore.open(self.root) as store:
            leases = LeaseManager(store)
            effects = EffectEngine(store, leases)
            try:
                target_plan = effects.plan_effect(
                    spec.run_id,
                    spec.job_id,
                    attempt_id,
                    _target_action_id(spec.run_id),
                    GitRefEffect(self.root, target, None, spec.base_snapshot.head),
                )
                claimed = effects.claim_effect(target_plan, ttl_seconds=30)
                target_result = effects.execute_effect(claimed)
            except EffectStateRefusal:
                target_result = effects.reconcile_actions(
                    (_target_action_id(spec.run_id),))[0]
            if target_result.state not in {
                    EffectResultState.COMPLETED, EffectResultState.NOOP}:
                raise EngineBindingRefusal(
                    target_result.reason or "private integration target was not established")
        applied = apply_integration_decision(
            spec.run_id,
            attempt_id,
            _apply_action_id(spec.run_id),
            self.root,
            assembly.result_ref,
            target,
            evidence.artifact_reference.reference_id,
            decision.artifact_reference.reference_id,
            start=self.root,
        )
        with RunStore.open(self.root) as store:
            job = store.get_entity(EntityKind.JOB, spec.job_id)
            if job.state != "accepted":
                store.record_transition(
                    EntityKind.JOB,
                    spec.job_id,
                    expected_version=job.version,
                    next_state="accepted",
                    reason=TransitionReason.COMPLETED,
                    evidence_digest=applied.observed_digest,
                )
            run = store.get_run(spec.run_id)
            if run.state != "completed":
                store.record_transition(
                    EntityKind.RUN,
                    spec.run_id,
                    expected_version=run.version,
                    next_state="completed",
                    reason=TransitionReason.COMPLETED,
                    evidence_digest=applied.observed_digest,
                )
        return CompletionResult(spec.run_id, evidence, decision, applied)

    def resume(self, run_id: str) -> ResumeResult:
        spec = load_run_spec(run_id, start=self.root)
        with RunStore.open(self.root) as store:
            run = store.get_run(run_id)
            if run.state in _CANCELLATION_STATES:
                return ResumeResult(
                    run_id,
                    cancellation=self._resume_cancel(store, run_id, {}),
                )
            if run.state == "completed":
                return ResumeResult(
                    run_id,
                    dispatch={
                        "action": None,
                        "engine": "idle",
                        "reason": "run_completed",
                        "run_state": "completed",
                    },
                )
        dispatch = load_dispatch_ready(run_id, start=self.root)
        with RunStore.open(self.root) as store:
            if not self._runner_actions(store, run_id):
                branch = None
            else:
                branch = self._actions_next_open(store, run_id, dispatch=dispatch)
            if branch is not None and (
                    branch.get("engine") == "busy" or branch.get("action") is not None):
                return ResumeResult(run_id, dispatch=branch)
            if branch is not None and self._runner_actions(store, run_id):
                return ResumeResult(run_id, dispatch=branch)
        return ResumeResult(run_id, completion=self._complete(spec))

    def status(self, run_id: str) -> RunSnapshot:
        with open_read_only_store(self.root) as store:
            leases = LeaseManager(store)
            supervisor = Supervisor(store, leases, invocations={})
            effects = EffectEngine(
                store,
                leases,
                runner_identity_verifier=supervisor.runner_identity_verifier,
            )
            return snapshot_run(store, effects, supervisor, run_id)

    def status_human(self, run_id: str) -> str:
        return render_human(self.status(run_id))

    def status_json(self, run_id: str) -> Mapping[str, object]:
        return json_projection(self.status(run_id))

    def watch(self, run_id: str, *, poll_interval: float = 1.0) -> Iterator[str]:
        return watch_run(lambda: self.status(run_id), poll_interval=poll_interval)

    def latest_run_id(self) -> str:
        with open_read_only_store(self.root) as store:
            with store._connection_lock:  # noqa: SLF001
                row = store._connection.execute(  # noqa: SLF001
                    "SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if row is None:
            raise RecordNotFoundError("run", "latest")
        return row["run_id"]


__all__ = [
    "CancelReason",
    "CompletionResult",
    "EngineAssemblyError",
    "EngineBindingRefusal",
    "EngineConfigurationUnavailable",
    "PreflightInputs",
    "ReadOnlyStoreUnavailable",
    "ResumeResult",
    "RunAssembly",
    "RunEngine",
    "StartResult",
    "open_read_only_store",
]
