"""One-task run-engine composition facade.

This module owns no retry policy and implements no effect, verification, or
cancellation protocol.  It wires the frozen inputs and typed results exposed by
the neighbouring run modules into the M1-B one-task vertical path.
"""
from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from waystone.adapters.git import GitReadError, git_full_sha, git_read_bytes
from waystone.core import WorkflowError
from waystone.jobs.domain import Role
from waystone.jobs.profile import RunAssembly as ProductionRunAssembly
from waystone.project.brief import FrameStatusRef, ProjectFactRef
from waystone.runs.artifacts import (
    ArtifactReference, ArtifactReferenceKind, ArtifactStore, validate_sha256_digest,
)
from waystone.runs.assurance import (
    Candidate,
    EvaluationEvidence,
    assert_evaluation_generation_available,
    ReviewCycleExhausted,
    ReviewCycle,
    assert_promotion_unblocked,
    execute_assurance_dag,
    parse_assurance_plan_bytes,
    parse_candidate_bytes,
    parse_evaluation_evidence_bytes,
    parse_evaluation_spec_bytes,
    parse_review_cycle_bytes,
)
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
from waystone.runs.outcome import OutcomePublication, publish_outcome
from waystone.runs.spec import (
    RunSpec,
    load_run_spec,
    plan_one_task_run,
    prepare_run_spec_revision,
)
from waystone.runs.store import (
    ContextNotCurrent,
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
from waystone.runs.worker_result import (
    AdaptedWorkerResult,
    ContextRequestedWorkerResult,
    CompletedWorkerResult,
    ContextResponse,
    WorkerResultAdapter,
    parse_runner_completion_marker_v2_bytes,
    parse_context_response_bytes,
    revise_work_brief_for_response,
    capture_result_snapshot,
)
from waystone.runs.verify import (
    ActorIdentity,
    ApplyResult,
    DecisionInput,
    EngineCheckExecutor,
    IntegrationDecision,
    VerifierAdapter,
    VerifierEvidence,
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


@dataclass(frozen=True)
class StagedStartResult:
    spec: RunSpec
    attempt_id: str


@dataclass(frozen=True)
class PendingContext:
    run_id: str
    request_digest: str
    request: Mapping[str, object]


@dataclass(frozen=True)
class ContextResumeResult:
    spec: RunSpec
    response: ContextResponse
    attempt_id: str


class StagedRunEngine:
    """A2 production entry point over an already assembled canonical kernel graph."""

    def __init__(self, assembly: ProductionRunAssembly):
        if not isinstance(assembly, ProductionRunAssembly):
            raise TypeError("assembly must be a production RunAssembly")
        self.assembly = assembly
        self.root = assembly.context.canonical_root
        self.input_root = assembly.context.active_worktree_root

    def start(
        self,
        task_id: str,
        *,
        work_brief_content: bytes,
        completion_contract_content: bytes,
        assurance_plan_content: bytes,
        frame_status_ref: FrameStatusRef,
        project_fact_refs: Sequence[ProjectFactRef],
        owner_request_reference: ArtifactReference | None = None,
        promotion_lineage=None,
        candidate: Mapping[str, object] | None = None,
        evaluation: Mapping[str, object] | None = None,
        result_policy=None,
    ) -> StagedStartResult:
        spec = plan_one_task_run(
            task_id,
            work_brief_content=work_brief_content,
            completion_contract_content=completion_contract_content,
            assurance_plan_content=assurance_plan_content,
            frame_status_ref=frame_status_ref,
            project_fact_refs=project_fact_refs,
            owner_request_reference=owner_request_reference,
            promotion_lineage=promotion_lineage,
            candidate=candidate,
            evaluation=evaluation,
            result_policy=result_policy,
            artifact_store=self.assembly.artifact_store,
            run_store=self.assembly.store,
            start=self.input_root,
        )
        store = self.assembly.store
        run = store.get_run(spec.run_id)
        ready = store.record_transition(
            EntityKind.RUN,
            spec.run_id,
            expected_version=run.version,
            next_state="dispatch-ready",
            reason=TransitionReason.PLANNED,
            evidence_digest=spec.run_spec_digest,
        )
        del ready
        job = store.get_entity(EntityKind.JOB, spec.job_id)
        store.record_transition(
            EntityKind.JOB,
            spec.job_id,
            expected_version=job.version,
            next_state="running",
            reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=spec.run_spec_digest,
        )
        attempt_id = f"{spec.run_id}:attempt:1"
        attempt = store.create_attempt(
            spec.run_id, spec.job_id, attempt_id, initial_state="dispatch-ready")
        store.record_transition(
            EntityKind.ATTEMPT,
            attempt_id,
            expected_version=attempt.version,
            next_state="running",
            reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=spec.run_spec_digest,
        )
        return StagedStartResult(spec, attempt_id)

    def observe_worker_result(
        self,
        run_id: str,
        attempt_id: str,
        action_id: str,
    ) -> AdaptedWorkerResult:
        """Follow an observed runner effect to its reserved union result exactly once."""
        store = self.assembly.store
        action = store.get_entity(EntityKind.ACTION, action_id)
        if (action.run_id != run_id or action.parent_attempt_id != attempt_id
                or action.state != "completed"):
            raise EngineBindingRefusal(
                "worker result may be consumed only after its bound runner action is observed")
        spec = load_run_spec(run_id, start=self.root)
        plan = self.assembly.effect_executor._load_plan(action_id)  # noqa: SLF001
        marker_path = Path(plan.spec["completion_marker"])
        try:
            marker = parse_runner_completion_marker_v2_bytes(marker_path.read_bytes())
        except (OSError, WorkflowError) as error:
            raise EngineBindingRefusal(
                f"completed staged runner lacks a valid marker v2: {error}") from error
        if (marker.run_id != run_id or marker.job_id != spec.job_id
                or marker.action_id != action_id):
            raise EngineBindingRefusal("runner marker v2 identity differs from the bound action")
        adapted = WorkerResultAdapter(
            self.input_root, self.assembly.artifact_store).adapt_published(
                marker.worker_result_digest,
                run_id=run_id,
                job_id=spec.job_id,
                attempt_id=attempt_id,
                run_spec_digest=spec.run_spec_digest,
                work_brief_digest=spec.work_brief.digest,
                base_snapshot_digest=spec.base_snapshot.digest,
            )
        if isinstance(adapted.result, ContextRequestedWorkerResult):
            assert adapted.context_request_artifact is not None
            store.record_context_request(
                run_id,
                spec.job_id,
                attempt_id,
                context_request_digest=adapted.context_request_artifact.digest,
                artifact_references=(
                    ArtifactReference(
                        f"worker-result:{attempt_id}",
                        ArtifactReferenceKind.EVIDENCE,
                        adapted.worker_result_artifact.digest,
                        adapted.worker_result_artifact.size,
                    ),
                    ArtifactReference(
                        f"context-request:{run_id}:{spec.revision}",
                        ArtifactReferenceKind.EVIDENCE,
                        adapted.context_request_artifact.digest,
                        adapted.context_request_artifact.size,
                    ),
                ),
            )
        return adapted

    def pending_context(self, run_id: str) -> PendingContext:
        store = self.assembly.store
        run = store.get_run(run_id)
        if run.state != "waiting_context":
            raise ContextNotCurrent(run_id, f"run state is {run.state!r}")
        with store._connection_lock:  # noqa: SLF001 - package context-head projection
            row = store._connection.execute(  # noqa: SLF001
                "SELECT reference_id FROM artifacts WHERE run_id = ? "
                "AND reference_id LIKE ? ORDER BY transition_id DESC LIMIT 1",
                (run_id, f"context-request:{run_id}:%"),
            ).fetchone()
        if row is None:
            raise ContextNotCurrent(run_id, "waiting run has no context request head")
        reference = store.get_artifact_reference(row["reference_id"])
        content = self.assembly.artifact_store.read_reference(reference)
        try:
            request = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EngineBindingRefusal(f"context request artifact is invalid: {error}") from error
        if not isinstance(request, dict):
            raise EngineBindingRefusal("context request artifact is not an object")
        return PendingContext(run_id, reference.digest, request)

    def provide_context(self, run_id: str, response_content: bytes) -> ContextResumeResult:
        """Bind a response, derived semantic revisions, and the next attempt in one store CAS."""
        pending = self.pending_context(run_id)
        response_artifact = self.assembly.artifact_store.write(response_content)
        coordinator = self.assembly.profile.binding_for(Role.COORDINATOR)
        response = parse_context_response_bytes(
            response_content,
            expected_request_digest=pending.request_digest,
            expected_binding_digest=coordinator.binding_digest,
        )
        source_digest = response.answer_source.get("digest")
        if response.answer_source.get("kind") in {"owner-artifact", "evidence"}:
            try:
                self.assembly.artifact_store.read(source_digest)  # type: ignore[arg-type]
            except WorkflowError as error:
                raise EngineBindingRefusal(
                    "context answer source bytes are not present in canonical CAS") from error
        previous = load_run_spec(run_id, start=self.root)
        prior_brief = self.assembly.artifact_store.read(previous.work_brief.digest)
        revised_brief = revise_work_brief_for_response(prior_brief, response)
        assurance = self.assembly.artifact_store.read(previous.assurance_plan.digest)
        completion = self.assembly.artifact_store.read(
            previous.job_input.completion_contract.digest)
        prepared = prepare_run_spec_revision(
            previous,
            work_brief_content=revised_brief,
            completion_contract_content=completion,
            assurance_plan_content=assurance,
            resolves_context_request_digest=pending.request_digest,
            start=self.root,
        )
        revision = prepared.spec.revision
        transition = self.assembly.store.provide_context(
            run_id,
            previous.job_id,
            request_digest=pending.request_digest,
            run_spec_digest=prepared.spec.run_spec_digest,
            max_total_attempts=previous.retry.max_total_attempts,
            artifact_references=(
                ArtifactReference(
                    f"context-response:{run_id}:{revision}",
                    ArtifactReferenceKind.INPUT,
                    response_artifact.digest,
                    response_artifact.size,
                ),
                ArtifactReference(
                    prepared.spec.work_brief.reference_id,
                    ArtifactReferenceKind.INPUT,
                    prepared.work_brief_artifact.digest,
                    prepared.work_brief_artifact.size,
                ),
                ArtifactReference(
                    prepared.spec.assurance_plan.reference_id,
                    ArtifactReferenceKind.INPUT,
                    prepared.assurance_plan_artifact.digest,
                    prepared.assurance_plan_artifact.size,
                ),
                ArtifactReference(
                    prepared.spec.job_input.completion_contract.reference_id,
                    ArtifactReferenceKind.INPUT,
                    prepared.completion_contract_artifact.digest,
                    prepared.completion_contract_artifact.size,
                ),
                ArtifactReference(
                    f"run-spec:{run_id}:{revision}",
                    ArtifactReferenceKind.INPUT,
                    prepared.run_spec_artifact.digest,
                    prepared.run_spec_artifact.size,
                ),
            ),
        )
        return ContextResumeResult(prepared.spec, response, transition.attempt.entity_id)

    def resume(self, run_id: str) -> Mapping[str, object]:
        return self.assembly.transport.actions_next(run_id)

    def close(self, run_id: str, outcome_content: bytes) -> OutcomePublication:
        """Publish one evidence-bound outcome pair before completing the run."""
        return publish_outcome(self.assembly, run_id, outcome_content)

    def execute_stage(
        self,
        run_id: str,
        handlers: Mapping[str, Callable[[], object]],
        *,
        regression_contract_digest: str | None = None,
        supported_scope_digest: str | None = None,
        accepted_risks_digest: str | None = None,
    ) -> tuple[tuple[str, object], ...]:
        """Run exactly the frozen stage DAG and close only its declared completion path."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if plan.lifecycle_stage is not spec.lifecycle_stage:
            raise EngineBindingRefusal("AssurancePlan stage differs from RunSpec")
        stage = spec.lifecycle_stage.value
        candidate_ref = None
        candidate_oid = None
        if spec.candidate is not None:
            candidate_ref = spec.candidate["target_ref"]
            candidate_oid = spec.candidate["target_oid"]
            if git_full_sha(self.input_root, candidate_ref) != candidate_oid:
                raise EngineBindingRefusal("frozen candidate ref changed before stage execution")
        if stage == "explore":
            candidate_ref = spec.result_policy.target_ref
            if candidate_ref is None or git_full_sha(self.input_root, candidate_ref) is not None:
                raise EngineBindingRefusal(
                    "explore candidate publication requires an absent run-owned ref")
        if spec.lifecycle_stage.value == "promote":
            if spec.frame_status_ref.status != "committed":
                raise EngineBindingRefusal("promotion requires a committed project frame")
            required_records = {
                "regression contract": regression_contract_digest,
                "supported scope": supported_scope_digest,
                "accepted risks": accepted_risks_digest,
            }
            for label, digest in required_records.items():
                if digest is None:
                    raise EngineBindingRefusal(f"promotion requires a {label} record")
                try:
                    self.assembly.artifact_store.read(digest)
                except WorkflowError as error:
                    raise EngineBindingRefusal(
                        f"promotion {label} record is not present in canonical CAS") from error
            if spec.promotion_lineage is None or spec.candidate is None:
                raise EngineBindingRefusal("promotion lineage/candidate is not frozen")
            target_ref = spec.result_policy.target_ref
            expected_oid = spec.result_policy.expected_oid
            if (target_ref is None or expected_oid is None
                    or git_full_sha(self.input_root, target_ref) != expected_oid):
                raise EngineBindingRefusal(
                    "promotion target differs from its frozen expected-old OID")
            candidate_lineage = self._candidate_lineage(spec.candidate["digest"])
            assert_promotion_unblocked(
                self.root / "docs" / "reviews",
                spec.promotion_lineage.id,
                candidate_lineage,
            )
            review = plan.review
            if (plan.requires("adversarial-review")
                    and review["consumed_cycles"] >= review["max_cycles"]):
                exhausted = ReviewCycleExhausted(
                    review["consumed_cycles"], review["max_cycles"])
                self._wait_for_review_budget(spec, exhausted)
                raise exhausted
        results = execute_assurance_dag(
            plan,
            handlers,
            mutation_digest=lambda: capture_result_snapshot(self.input_root).digest,
        )
        if stage == "explore":
            try:
                reference = self.assembly.store.get_artifact_reference(
                    f"candidate:{run_id}")
            except RecordNotFoundError as error:
                raise EngineBindingRefusal(
                    "explore completed without a published candidate descriptor") from error
            candidate = parse_candidate_bytes(
                self.assembly.artifact_store.read_reference(reference))
            if (candidate.target_ref != candidate_ref
                    or git_full_sha(self.input_root, candidate.target_ref)
                    != candidate.target_oid):
                raise EngineBindingRefusal(
                    "explore candidate ref does not match its frozen published descriptor")
        elif stage == "evaluate":
            if (candidate_ref is None or candidate_oid is None
                    or git_full_sha(self.input_root, candidate_ref) != candidate_oid):
                raise EngineBindingRefusal("evaluate mutated its frozen candidate ref")
            try:
                evidence = self.assembly.store.get_artifact_reference(
                    f"evaluation-evidence:{run_id}")
            except RecordNotFoundError as error:
                raise EngineBindingRefusal(
                    "evaluate completed without bound evaluation evidence") from error
            parse_evaluation_evidence_bytes(
                self.assembly.artifact_store.read_reference(evidence))
        else:
            assert candidate_ref is not None and candidate_oid is not None
            if git_full_sha(self.input_root, candidate_ref) != candidate_oid:
                raise EngineBindingRefusal("promotion mutated its evaluated candidate ref")
            assert spec.result_policy.target_ref is not None
            if git_full_sha(self.input_root, spec.result_policy.target_ref) != candidate_oid:
                raise EngineBindingRefusal(
                    "promotion apply did not publish the evaluated candidate OID")
        evidence_digest = spec.run_spec_digest
        if results and isinstance(results[-1][1], str):
            try:
                validate_sha256_digest(results[-1][1])
            except ValueError:
                pass
            else:
                evidence_digest = results[-1][1]
        self._record_stage_completion(spec, evidence_digest)
        return results

    def publish_candidate(
        self,
        run_id: str,
        attempt_id: str,
        adapted: AdaptedWorkerResult,
        *,
        target_oid: str,
        config_digest: str,
        supersedes_candidate_digest: str | None = None,
        repair_of_finding_refs: Sequence[str] = (),
    ) -> Candidate:
        """Publish one explore result with creation-only candidate-ref CAS."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if (spec.lifecycle_stage.value != "explore"
                or not plan.requires("candidate-publish")
                or not isinstance(adapted.result, CompletedWorkerResult)):
            raise EngineBindingRefusal(
                "candidate publication requires a completed explore result and frozen action")
        target_ref = spec.result_policy.target_ref
        if target_ref != f"refs/waystone/candidates/{run_id}":
            raise EngineBindingRefusal("candidate target ref is not the frozen run-owned ref")
        candidate = Candidate(
            candidate_id=run_id,
            producer={
                "run_id": run_id,
                "run_spec_digest": spec.run_spec_digest,
                "result_digest": adapted.worker_result_artifact.digest,
            },
            code_sha=target_oid,
            config_digest=config_digest,
            target_ref=target_ref,
            target_oid=target_oid,
            supersedes_candidate_digest=supersedes_candidate_digest,
            repair_of_finding_refs=tuple(repair_of_finding_refs),
        )
        candidate_artifact = self.assembly.artifact_store.write(candidate.canonical_bytes())
        action_id = f"{run_id}:candidate-publish"
        try:
            effect_plan = self.assembly.effect_executor.plan_effect(
                run_id, spec.job_id, attempt_id, action_id,
                GitRefEffect(self.input_root, target_ref, None, target_oid),
            )
            claimed = self.assembly.effect_executor.claim_effect(effect_plan, ttl_seconds=30)
            result = self.assembly.effect_executor.execute_effect(claimed)
        except EffectStateRefusal:
            result = self.assembly.effect_executor.reconcile_actions((action_id,))[0]
        if result.state not in {EffectResultState.COMPLETED, EffectResultState.NOOP}:
            raise EngineBindingRefusal(result.reason or "candidate publication did not complete")
        reference_id = f"candidate:{run_id}"
        try:
            reference = self.assembly.store.get_artifact_reference(reference_id)
        except RecordNotFoundError:
            job = self.assembly.store.get_entity(EntityKind.JOB, spec.job_id)
            self.assembly.store.record_transition(
                EntityKind.JOB, spec.job_id, expected_version=job.version,
                next_state=job.state, reason=TransitionReason.CANDIDATE_PUBLISHED,
                evidence_digest=candidate_artifact.digest,
                artifact_references=(ArtifactReference(
                    reference_id, ArtifactReferenceKind.EVIDENCE,
                    candidate_artifact.digest, candidate_artifact.size),),
            )
        else:
            if reference.digest != candidate_artifact.digest:
                raise EngineBindingRefusal(
                    "published candidate reference differs from the deterministic descriptor")
        return candidate

    def publish_evaluation_evidence(
        self,
        run_id: str,
        *,
        evaluator_action_id: str,
        result: str,
        metric_artifacts: Sequence[Mapping[str, str]],
        prior_evidence: Sequence[EvaluationEvidence] = (),
        holdout_exposed: bool = False,
    ) -> tuple[EvaluationEvidence, ArtifactReference]:
        """Bind a read-only evaluator result to the exact candidate/spec generation."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if (spec.lifecycle_stage.value != "evaluate"
                or not plan.requires("read-only-evaluator")
                or spec.candidate is None):
            raise EngineBindingRefusal(
                "evaluation evidence requires a frozen evaluate plan and candidate")
        frozen_spec = spec.evaluation.get("spec")
        if not isinstance(frozen_spec, Mapping):
            raise EngineBindingRefusal("evaluation spec is not frozen")
        try:
            spec_bytes = git_read_bytes(
                self.input_root, "show",
                f"{frozen_spec['commit']}:{frozen_spec['path']}")
        except GitReadError as error:
            raise EngineBindingRefusal(f"evaluation spec is unavailable: {error}") from error
        parsed_spec = parse_evaluation_spec_bytes(spec_bytes)
        with self.assembly.store._connection_lock:  # noqa: SLF001 - evidence index
            rows = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT reference_id FROM artifacts WHERE reference_id LIKE ?",
                ("evaluation-evidence:%",),
            ).fetchall()
        indexed_evidence = []
        for row in rows:
            reference = self.assembly.store.get_artifact_reference(row["reference_id"])
            indexed_evidence.append(parse_evaluation_evidence_bytes(
                self.assembly.artifact_store.read_reference(reference)))
        assert_evaluation_generation_available(
            spec.candidate["digest"], parsed_spec,
            (*indexed_evidence, *prior_evidence),
            holdout_exposed=holdout_exposed,
        )
        evidence = EvaluationEvidence(
            candidate_digest=spec.candidate["digest"],
            evaluation_spec_digest=frozen_spec["digest"],
            evaluation_generation=frozen_spec["generation"],
            evaluator_action_id=evaluator_action_id,
            result=result,
            metric_artifacts=tuple(dict(item) for item in metric_artifacts),
        )
        artifact = self.assembly.artifact_store.write(evidence.canonical_bytes())
        reference = ArtifactReference(
            f"evaluation-evidence:{run_id}", ArtifactReferenceKind.EVIDENCE,
            artifact.digest, artifact.size,
        )
        job = self.assembly.store.get_entity(EntityKind.JOB, spec.job_id)
        self.assembly.store.record_transition(
            EntityKind.JOB, spec.job_id, expected_version=job.version,
            next_state=job.state, reason=TransitionReason.EVALUATION_EVIDENCE,
            evidence_digest=artifact.digest, artifact_references=(reference,),
        )
        return evidence, reference

    def append_review_cycle(
        self, run_id: str, *, target_result_digest: str, review_digest: str,
    ) -> ReviewCycle:
        """Append one immutable review cycle without trusting a caller-supplied count."""
        spec = load_run_spec(run_id, start=self.root)
        plan = parse_assurance_plan_bytes(
            self.assembly.artifact_store.read(spec.assurance_plan.digest))
        if (spec.promotion_lineage is None
                or plan.review.get("promotion_lineage_id") != spec.promotion_lineage.id
                or not plan.requires("adversarial-review")):
            raise EngineBindingRefusal("run has no frozen risk-gated review action")
        cycles = []
        head = plan.review.get("cycle_chain_head_digest")
        while head is not None:
            cycle = parse_review_cycle_bytes(self.assembly.artifact_store.read(head))
            cycles.append(cycle)
            head = cycle.supersedes_digest
        cycles.reverse()
        if len(cycles) != plan.review["consumed_cycles"]:
            raise EngineBindingRefusal(
                "AssurancePlan consumed review count does not rederive from its CAS chain")
        maximum = plan.review["max_cycles"]
        if len(cycles) >= maximum:
            exhausted = ReviewCycleExhausted(len(cycles), maximum)
            self._wait_for_review_budget(spec, exhausted)
            raise exhausted
        cycle = ReviewCycle(
            promotion_lineage_id=spec.promotion_lineage.id,
            cycle=len(cycles) + 1,
            target_result_digest=target_result_digest,
            review_digest=review_digest,
            supersedes_digest=(cycles[-1].digest if cycles else None),
        )
        self.assembly.artifact_store.write(cycle.canonical_bytes())
        return cycle

    def _candidate_lineage(self, candidate_digest: object) -> tuple[str, ...]:
        current = validate_sha256_digest(candidate_digest)  # type: ignore[arg-type]
        lineage = []
        seen = set()
        while current is not None:
            if current in seen:
                raise EngineBindingRefusal("candidate supersedes lineage contains a cycle")
            seen.add(current)
            candidate = parse_candidate_bytes(self.assembly.artifact_store.read(current))
            lineage.append(current)
            current = candidate.supersedes_candidate_digest
        return tuple(reversed(lineage))

    def _wait_for_review_budget(
            self, spec: RunSpec, exhausted: ReviewCycleExhausted) -> None:
        artifact = self.assembly.artifact_store.write(
            json.dumps(
                exhausted.waiting_user(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8"))
        for kind, identity in ((EntityKind.JOB, spec.job_id), (EntityKind.RUN, spec.run_id)):
            entity = (
                self.assembly.store.get_entity(kind, identity)
                if kind is EntityKind.JOB else self.assembly.store.get_run(identity))
            if entity.state != "waiting_user":
                self.assembly.store.record_transition(
                    kind, identity, expected_version=entity.version,
                    next_state="waiting_user",
                    reason=TransitionReason.REVIEW_CYCLE_EXHAUSTED,
                    evidence_digest=artifact.digest,
                )

    def _record_stage_completion(self, spec: RunSpec, evidence_digest: str) -> None:
        with self.assembly.store._connection_lock:  # noqa: SLF001 - final attempt projection
            row = self.assembly.store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id FROM attempts WHERE run_id = ? AND job_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (spec.run_id, spec.job_id),
            ).fetchone()
        if row is None:
            raise EngineBindingRefusal("stage completion requires a final attempt")
        attempt_id = row["attempt_id"]
        for kind, identity, next_state in (
                (EntityKind.ATTEMPT, attempt_id, "completed"),
                (EntityKind.JOB, spec.job_id, "completed"),
                (EntityKind.RUN, spec.run_id, "closeout-ready")):
            entity = (
                self.assembly.store.get_run(identity)
                if kind is EntityKind.RUN
                else self.assembly.store.get_entity(kind, identity))
            if entity.state != next_state:
                self.assembly.store.record_transition(
                    kind, identity, expected_version=entity.version,
                    next_state=next_state, reason=TransitionReason.COMPLETED,
                    evidence_digest=evidence_digest,
                )


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
                "an alternate execution path is not a fallback")
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
        assurance = parse_assurance_plan_bytes(
            ArtifactStore(self.root).read(spec.assurance_plan.digest))
        raise EngineConfigurationUnavailable(
            f"RunSpec v2 {assurance.lifecycle_stage.value} completion must execute "
            "through StagedRunEngine.execute_stage with its frozen exact action DAG")

    def resume(self, run_id: str) -> ResumeResult:
        spec = load_run_spec(run_id, start=self.root)
        completion_started = False
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
            try:
                store.get_artifact_reference(
                    f"verifier-evidence:{_verify_action_id(run_id)}")
            except RecordNotFoundError:
                pass
            else:
                completion_started = True
        if completion_started:
            return ResumeResult(run_id, completion=self._complete(spec))
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
    "ContextResumeResult",
    "EngineAssemblyError",
    "EngineBindingRefusal",
    "EngineConfigurationUnavailable",
    "PreflightInputs",
    "PendingContext",
    "ReadOnlyStoreUnavailable",
    "ResumeResult",
    "RunAssembly",
    "RunEngine",
    "StagedRunEngine",
    "StagedStartResult",
    "StartResult",
    "open_read_only_store",
]
