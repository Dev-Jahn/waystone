"""Frozen stage assurance, candidate evaluation, and promotion lineage contracts."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from waystone.core import WorkflowError
from waystone.jobs.completion import LifecycleStage
from waystone.reviews import findings
from waystone.runs.artifacts import ArtifactStore, validate_sha256_digest


ASSURANCE_PLAN_SCHEMA = "waystone-assurance-plan-1"
CANDIDATE_SCHEMA = "waystone-candidate-1"
EVALUATION_SPEC_SCHEMA = "waystone-evaluation-spec-1"
EVALUATION_EVIDENCE_SCHEMA = "waystone-evaluation-evidence-1"
REVIEW_CYCLE_SCHEMA = "waystone-review-cycle-1"
REVIEWER_EVIDENCE_SCHEMA = "waystone-promotion-review-evidence-1"

_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_STAGE_ACTIONS = {
    LifecycleStage.EXPLORE: (
        "worker", "result-adapter", "candidate-publish", "completion"),
    LifecycleStage.EVALUATE: (
        "freeze", "read-only-evaluator", "evaluation-evidence", "completion"),
    LifecycleStage.PROMOTE: (
        "evaluated-candidate-freeze", "independent-verify", "adversarial-review",
        "integration-decision", "target-ref-apply", "completion"),
}
_ALL_ACTIONS = tuple(dict.fromkeys(
    action for stage_actions in _STAGE_ACTIONS.values() for action in stage_actions))
_REVIEW_EXHAUSTED_OPTIONS = (
    "accept-risk",
    "reduce-supported-scope",
    "simplify-architecture",
    "approve-separate-research-track",
)
_ALLOWED_OUTCOMES = (
    "executable-capability",
    "measured-improvement",
    "validated-decision",
    "simplification",
)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class AssuranceError(WorkflowError):
    code = "assurance_error"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class AssurancePlanRefusal(AssuranceError):
    code = "assurance_plan_refusal"


class AmbiguousStageRefusal(AssurancePlanRefusal):
    code = "ambiguous_lifecycle_stage"


class CandidateRefusal(AssuranceError):
    code = "candidate_refusal"


class EvaluationFreezeRefusal(AssuranceError):
    code = "evaluation_freeze_refusal"


class HoldoutGenerationInvalidated(EvaluationFreezeRefusal):
    code = "holdout_generation_invalidated"


class PromotionLineageRefusal(AssuranceError):
    code = "promotion_lineage_refusal"


class ReviewCycleExhausted(PromotionLineageRefusal):
    code = "review_cycle_exhausted"

    def __init__(self, consumed: int, maximum: int):
        self.consumed = consumed
        self.maximum = maximum
        self.options = _REVIEW_EXHAUSTED_OPTIONS
        super().__init__(
            f"review cycles are exhausted ({consumed}/{maximum}); owner choice is required")

    def waiting_user(self) -> dict[str, object]:
        return {
            "state": "waiting_user",
            "reason": "review-cycle-exhausted",
            "options": list(self.options),
        }


class PromotionBlocked(PromotionLineageRefusal):
    code = "promotion_blocked"

    def __init__(self, finding_ids: Sequence[str]):
        self.finding_ids = tuple(sorted(finding_ids))
        super().__init__(
            "fix-before-promotion findings lack verified descendant clearance: "
            + ", ".join(self.finding_ids))


class StageExecutionRefusal(AssuranceError):
    code = "stage_execution_refusal"


class StageMutationRefusal(StageExecutionRefusal):
    code = "stage_mutation_refusal"


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssurancePlanRefusal(f"{field} must be a mapping")
    return dict(value)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssurancePlanRefusal(f"{field} must be a non-empty string")
    return value


def _digest(value: object, field: str) -> str:
    try:
        return validate_sha256_digest(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise AssurancePlanRefusal(f"{field}: {error}") from error


def _oid(value: object, field: str) -> str:
    value = _nonempty(value, field)
    if _OID.fullmatch(value) is None:
        raise AssurancePlanRefusal(f"{field} must be a full lowercase Git OID")
    return value


def _canonical_document(content: bytes, schema: str, field: str) -> dict[str, Any]:
    if not isinstance(content, bytes):
        raise TypeError(f"{field} content must be bytes")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssurancePlanRefusal(f"{field} must be canonical UTF-8 JSON: {error}") from error
    if not isinstance(value, dict) or canonical_json(value) != content:
        raise AssurancePlanRefusal(f"{field} bytes must be canonical JSON")
    if value.get("schema") != schema:
        raise AssurancePlanRefusal(f"{field}.schema must be {schema}")
    return value


def select_lifecycle_stage(
    declared_stage: str | LifecycleStage | None, *, intent: str | None = None,
) -> LifecycleStage:
    """Resolve only the deterministic §3.2 defaults; ambiguous intent never dispatches."""
    if declared_stage is not None:
        try:
            return LifecycleStage(declared_stage)
        except (TypeError, ValueError) as error:
            raise AmbiguousStageRefusal(
                "lifecycle_stage must be explore, evaluate, or promote") from error
    normalized = "" if intent is None else intent.strip().lower()
    proposals = {
        "spike": LifecycleStage.EXPLORE,
        "research": LifecycleStage.EXPLORE,
        "unclear-solution": LifecycleStage.EXPLORE,
        "frozen-candidate-judgment": LifecycleStage.EVALUATE,
        "merge": LifecycleStage.PROMOTE,
        "release": LifecycleStage.PROMOTE,
        "public-contract": LifecycleStage.PROMOTE,
    }
    try:
        return proposals[normalized]
    except KeyError as error:
        raise AmbiguousStageRefusal(
            "stage is ambiguous; owner or coordinator ruling is required before dispatch") from error


@dataclass(frozen=True)
class AssurancePlan:
    lifecycle_stage: LifecycleStage
    compiled_from: tuple[Mapping[str, object], ...]
    completion: Mapping[str, object]
    actions: tuple[str, ...]
    action_requirements: Mapping[str, str]
    verification: Mapping[str, object]
    tests: Mapping[str, str]
    review: Mapping[str, object]
    promotion: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": ASSURANCE_PLAN_SCHEMA,
            "lifecycle_stage": self.lifecycle_stage.value,
            "compiled_from": [dict(item) for item in self.compiled_from],
            "execution_safety": {
                "sandbox_preflight": "required",
                "lease_fencing": "required",
                "effect_reconciliation": "required",
            },
            "completion": dict(self.completion),
            "actions": list(self.actions),
            "action_requirements": dict(self.action_requirements),
            "verification": dict(self.verification),
            "tests": dict(self.tests),
            "review": dict(self.review),
            "promotion": dict(self.promotion),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    def requires(self, action: str) -> bool:
        return self.action_requirements.get(action) in {"required", "risk-gated"}


def _policy_cycles(project_policy: Mapping[str, object] | None, stage: LifecycleStage) -> int:
    if project_policy is None:
        return 0 if stage is LifecycleStage.EXPLORE else 2
    value = project_policy.get("max_review_cycles")
    if value is None:
        return 0 if stage is LifecycleStage.EXPLORE else 2
    if type(value) is not int or value < 0:
        raise AssurancePlanRefusal("project policy max_review_cycles must be zero or positive")
    return value


def _stage_requirements(
        stage: LifecycleStage, risks: Sequence[str]) -> dict[str, str]:
    requirements = {action: "not-required" for action in _ALL_ACTIONS}
    for action in _STAGE_ACTIONS[stage]:
        requirements[action] = "required"
    if stage is LifecycleStage.PROMOTE:
        requirements["adversarial-review"] = "risk-gated" if risks else "not-required"
    return requirements


def compile_assurance_plan(
    lifecycle_stage: str | LifecycleStage | None,
    *,
    declared_risks: Sequence[str] = (),
    evaluation_spec: Mapping[str, object] | None = None,
    project_policy: Mapping[str, object] | None = None,
    completion_contract: Mapping[str, object] | None = None,
    compiled_from: Sequence[Mapping[str, object]] = (),
    promotion_lineage_id: str | None = None,
    consumed_review_cycles: int = 0,
    review_cycles: Sequence["ReviewCycle"] = (),
    inherited_max_review_cycles: int | None = None,
    intent: str | None = None,
) -> AssurancePlan:
    stage = select_lifecycle_stage(lifecycle_stage, intent=intent)
    risks = tuple(sorted({_nonempty(item, "declared risk") for item in declared_risks}))
    if type(consumed_review_cycles) is not int or consumed_review_cycles < 0:
        raise AssurancePlanRefusal("consumed_review_cycles must be non-negative")
    maximum = _policy_cycles(project_policy, stage)
    if inherited_max_review_cycles is not None:
        if type(inherited_max_review_cycles) is not int or inherited_max_review_cycles < 0:
            raise AssurancePlanRefusal("inherited review budget is invalid")
        if maximum != inherited_max_review_cycles:
            raise PromotionLineageRefusal(
                "a descendant run cannot reset its inherited max_review_cycles")
        maximum = inherited_max_review_cycles
    cycle_head = None
    for index, cycle in enumerate(review_cycles, start=1):
        if not isinstance(cycle, ReviewCycle):
            raise PromotionLineageRefusal("review_cycles must contain ReviewCycle values")
        if (cycle.promotion_lineage_id != promotion_lineage_id
                or cycle.cycle != index or cycle.supersedes_digest != cycle_head):
            raise PromotionLineageRefusal("review cycle chain is divergent or non-contiguous")
        cycle_head = cycle.digest
    if review_cycles and consumed_review_cycles not in (0, len(review_cycles)):
        raise PromotionLineageRefusal(
            "caller consumed_review_cycles differs from the rederived chain length")
    if not review_cycles and consumed_review_cycles:
        raise PromotionLineageRefusal(
            "consumed_review_cycles must be rederived from supplied immutable cycles")
    consumed_review_cycles = len(review_cycles)
    if consumed_review_cycles > maximum:
        raise PromotionLineageRefusal("consumed review cycles exceed the frozen maximum")
    if stage in (LifecycleStage.EVALUATE, LifecycleStage.PROMOTE):
        if not isinstance(evaluation_spec, Mapping):
            raise AssurancePlanRefusal(f"{stage.value} requires a frozen evaluation spec")
        for field in ("digest", "generation"):
            if field not in evaluation_spec:
                raise AssurancePlanRefusal(f"evaluation_spec.{field} is required")
        _digest(evaluation_spec["digest"], "evaluation_spec.digest")
        if type(evaluation_spec["generation"]) is not int or evaluation_spec["generation"] < 1:
            raise AssurancePlanRefusal("evaluation_spec.generation must be positive")
    if stage is LifecycleStage.PROMOTE and not promotion_lineage_id:
        raise AssurancePlanRefusal("promote requires a frozen promotion_lineage_id")

    stage_actions = _STAGE_ACTIONS[stage]
    requirements = _stage_requirements(stage, risks)
    actions = tuple(action for action in stage_actions if requirements[action] != "not-required")
    frozen_contract = dict(completion_contract or {})
    if frozen_contract:
        if set(frozen_contract) != {"reference_id", "digest"}:
            raise AssurancePlanRefusal("completion contract ref fields are not canonical")
        _nonempty(frozen_contract["reference_id"], "completion.contract.reference_id")
        _digest(frozen_contract["digest"], "completion.contract.digest")
    completion = {
        "contract": frozen_contract or None,
        "allowed_outcomes": list(_ALLOWED_OUTCOMES),
    }
    verification: dict[str, object] = {
        "independent": (
            "not-required" if stage is LifecycleStage.EXPLORE else "required"),
        "evaluation_spec": None if evaluation_spec is None else dict(evaluation_spec),
    }
    tests = {
        "probe": "optional" if stage is LifecycleStage.EXPLORE else "not-required",
        "evaluation_check": (
            "required" if stage in (LifecycleStage.EVALUATE, LifecycleStage.PROMOTE)
            else "not-required"),
        "regression_contract": (
            "required" if stage is LifecycleStage.PROMOTE else "not-required"),
    }
    review = {
        "requirement": (
            "risk-gated" if stage is LifecycleStage.PROMOTE else "not-required"),
        "reasons": list(risks),
        "promotion_lineage_id": promotion_lineage_id,
        "max_cycles": maximum,
        "consumed_cycles": consumed_review_cycles,
        "cycle_chain_head_digest": cycle_head,
    }
    promotion = {
        "committed_frame": "required" if stage is LifecycleStage.PROMOTE else "not-required",
        "supported_scope_record": "required" if stage is LifecycleStage.PROMOTE else "not-required",
        "accepted_risks_record": "required" if stage is LifecycleStage.PROMOTE else "not-required",
        "integration_apply": "required" if stage is LifecycleStage.PROMOTE else "not-required",
    }
    return AssurancePlan(
        stage, tuple(dict(item) for item in compiled_from), completion, actions,
        requirements, verification, tests, review, promotion)


def parse_assurance_plan_bytes(content: bytes) -> AssurancePlan:
    row = _canonical_document(content, ASSURANCE_PLAN_SCHEMA, "assurance plan")
    expected = {
        "schema", "lifecycle_stage", "compiled_from", "execution_safety", "completion",
        "actions", "action_requirements", "verification", "tests", "review", "promotion",
    }
    if set(row) != expected:
        raise AssurancePlanRefusal("assurance plan fields are not canonical")
    stage = select_lifecycle_stage(row["lifecycle_stage"])
    safety = row["execution_safety"]
    if safety != {
            "sandbox_preflight": "required", "lease_fencing": "required",
            "effect_reconciliation": "required"}:
        raise AssurancePlanRefusal("execution safety requirements cannot be lowered")
    raw_from = row["compiled_from"]
    if not isinstance(raw_from, list) or any(not isinstance(item, Mapping) for item in raw_from):
        raise AssurancePlanRefusal("compiled_from must be a list of typed refs")
    actions = row["actions"]
    requirements = row["action_requirements"]
    if not isinstance(actions, list) or not isinstance(requirements, Mapping):
        raise AssurancePlanRefusal("actions/action_requirements are invalid")
    review = _mapping(row["review"], "review")
    if set(review) != {
            "requirement", "reasons", "promotion_lineage_id", "max_cycles",
            "consumed_cycles", "cycle_chain_head_digest"}:
        raise AssurancePlanRefusal("review fields are not canonical")
    raw_reasons = review["reasons"]
    if (not isinstance(raw_reasons, list)
            or any(not isinstance(item, str) or not item.strip() for item in raw_reasons)
            or raw_reasons != sorted(set(raw_reasons))):
        raise AssurancePlanRefusal("review reasons must be sorted unique non-empty strings")
    expected_requirements = _stage_requirements(stage, raw_reasons)
    if dict(requirements) != expected_requirements:
        raise AssurancePlanRefusal("action requirements differ from the exact stage DAG")
    expected_actions = tuple(
        action for action in _STAGE_ACTIONS[stage]
        if requirements[action] != "not-required")
    if tuple(actions) != expected_actions:
        raise AssurancePlanRefusal("actions differ from the frozen exact stage DAG")
    expected_review_requirement = (
        "risk-gated" if stage is LifecycleStage.PROMOTE else "not-required")
    if review["requirement"] != expected_review_requirement:
        raise AssurancePlanRefusal("review requirement differs from lifecycle stage")
    lineage_id = review["promotion_lineage_id"]
    if stage is LifecycleStage.PROMOTE:
        _nonempty(lineage_id, "review.promotion_lineage_id")
    elif lineage_id is not None and stage is not LifecycleStage.EVALUATE:
        raise AssurancePlanRefusal("review lineage is valid only for evaluate/promote")
    for field in ("max_cycles", "consumed_cycles"):
        if type(review.get(field)) is not int or review[field] < 0:
            raise AssurancePlanRefusal(f"review.{field} must be non-negative")
    if review["consumed_cycles"] > review["max_cycles"]:
        raise AssurancePlanRefusal("review consumed_cycles exceeds max_cycles")
    if review["cycle_chain_head_digest"] is not None:
        _digest(review["cycle_chain_head_digest"], "review.cycle_chain_head_digest")
    verification = _mapping(row["verification"], "verification")
    if set(verification) != {"independent", "evaluation_spec"}:
        raise AssurancePlanRefusal("verification fields are not canonical")
    expected_independent = "not-required" if stage is LifecycleStage.EXPLORE else "required"
    if verification["independent"] != expected_independent:
        raise AssurancePlanRefusal("independent verification differs from lifecycle stage")
    evaluation_spec = verification["evaluation_spec"]
    if stage is LifecycleStage.EXPLORE:
        if evaluation_spec is not None:
            raise AssurancePlanRefusal("explore cannot freeze an evaluation spec")
    else:
        evaluation_spec = _mapping(evaluation_spec, "verification.evaluation_spec")
        _digest(evaluation_spec.get("digest"), "verification.evaluation_spec.digest")
        generation = evaluation_spec.get("generation")
        if type(generation) is not int or generation < 1:
            raise AssurancePlanRefusal(
                "verification.evaluation_spec.generation must be positive")
    expected_tests = {
        "probe": "optional" if stage is LifecycleStage.EXPLORE else "not-required",
        "evaluation_check": (
            "required" if stage in (LifecycleStage.EVALUATE, LifecycleStage.PROMOTE)
            else "not-required"),
        "regression_contract": (
            "required" if stage is LifecycleStage.PROMOTE else "not-required"),
    }
    if _mapping(row["tests"], "tests") != expected_tests:
        raise AssurancePlanRefusal("test requirements differ from lifecycle stage")
    expected_promotion = {
        "committed_frame": "required" if stage is LifecycleStage.PROMOTE else "not-required",
        "supported_scope_record": (
            "required" if stage is LifecycleStage.PROMOTE else "not-required"),
        "accepted_risks_record": (
            "required" if stage is LifecycleStage.PROMOTE else "not-required"),
        "integration_apply": "required" if stage is LifecycleStage.PROMOTE else "not-required",
    }
    if _mapping(row["promotion"], "promotion") != expected_promotion:
        raise AssurancePlanRefusal("promotion requirements differ from lifecycle stage")
    completion = _mapping(row["completion"], "completion")
    if (set(completion) != {"contract", "allowed_outcomes"}
            or completion["allowed_outcomes"] != list(_ALLOWED_OUTCOMES)):
        raise AssurancePlanRefusal("completion fields or allowed outcomes are not canonical")
    contract = completion["contract"]
    if contract is not None:
        contract = _mapping(contract, "completion.contract")
        if set(contract) != {"reference_id", "digest"}:
            raise AssurancePlanRefusal("completion contract ref fields are not canonical")
        _nonempty(contract["reference_id"], "completion.contract.reference_id")
        _digest(contract["digest"], "completion.contract.digest")
    return AssurancePlan(
        stage, tuple(dict(item) for item in raw_from),
        completion, tuple(actions), dict(requirements),
        verification, expected_tests, review, expected_promotion,
    )


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    producer: Mapping[str, str]
    code_sha: str
    config_digest: str
    target_ref: str
    target_oid: str
    supersedes_candidate_digest: str | None
    repair_of_finding_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.candidate_id, "candidate_id")
        if set(self.producer) != {"run_id", "run_spec_digest", "result_digest"}:
            raise CandidateRefusal("candidate producer fields are not canonical")
        _nonempty(self.producer["run_id"], "producer.run_id")
        _digest(self.producer["run_spec_digest"], "producer.run_spec_digest")
        _digest(self.producer["result_digest"], "producer.result_digest")
        _oid(self.code_sha, "code_sha")
        _oid(self.target_oid, "target_oid")
        if self.code_sha != self.target_oid:
            raise CandidateRefusal("candidate code_sha must equal its immutable target OID")
        _digest(self.config_digest, "config_digest")
        if not self.target_ref.startswith("refs/waystone/candidates/"):
            raise CandidateRefusal("candidate target_ref must be engine-owned candidates ref")
        if self.supersedes_candidate_digest is not None:
            _digest(self.supersedes_candidate_digest, "supersedes_candidate_digest")
        for reference in self.repair_of_finding_refs:
            _nonempty(reference, "repair_of_finding_refs item")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "producer": dict(self.producer),
            "code_sha": self.code_sha,
            "config_digest": self.config_digest,
            "target_ref": self.target_ref,
            "target_oid": self.target_oid,
            "supersedes_candidate_digest": self.supersedes_candidate_digest,
            "repair_of_finding_refs": list(self.repair_of_finding_refs),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return digest_bytes(self.canonical_bytes())


def parse_candidate_bytes(content: bytes) -> Candidate:
    row = _canonical_document(content, CANDIDATE_SCHEMA, "candidate")
    if set(row) != {
            "schema", "candidate_id", "producer", "code_sha", "config_digest",
            "target_ref", "target_oid", "supersedes_candidate_digest",
            "repair_of_finding_refs"}:
        raise CandidateRefusal("candidate fields are not canonical")
    repairs = row["repair_of_finding_refs"]
    if not isinstance(repairs, list):
        raise CandidateRefusal("repair_of_finding_refs must be a list")
    return Candidate(
        row["candidate_id"], _mapping(row["producer"], "producer"), row["code_sha"],
        row["config_digest"], row["target_ref"], row["target_oid"],
        row["supersedes_candidate_digest"], tuple(repairs))


@dataclass(frozen=True)
class EvaluationSpec:
    evaluation_id: str
    generation: int
    objective_ref: Mapping[str, object]
    criteria: tuple[Mapping[str, object], ...]
    datasets: tuple[Mapping[str, object], ...]
    seed: int
    supersedes_spec_digest: str | None
    content_digest: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.evaluation_id, "evaluation_id")
        if type(self.generation) is not int or self.generation < 1:
            raise EvaluationFreezeRefusal("evaluation generation must be positive")
        if not self.criteria:
            raise EvaluationFreezeRefusal("evaluation criteria must be non-empty")
        ids = []
        for index, criterion in enumerate(self.criteria):
            if set(criterion) != {"id", "metric", "operator", "threshold"}:
                raise EvaluationFreezeRefusal(f"criteria[{index}] fields are not canonical")
            ids.append(_nonempty(criterion["id"], f"criteria[{index}].id"))
        if len(ids) != len(set(ids)):
            raise EvaluationFreezeRefusal("evaluation criterion ids must be unique")
        if not self.datasets:
            raise EvaluationFreezeRefusal("evaluation datasets must be non-empty")
        for index, dataset in enumerate(self.datasets):
            if set(dataset) != {"id", "artifact_reference_id", "digest", "visibility"}:
                raise EvaluationFreezeRefusal(f"datasets[{index}] fields are not canonical")
            _digest(dataset["digest"], f"datasets[{index}].digest")
            if dataset["visibility"] != "harness-only":
                raise EvaluationFreezeRefusal("evaluation datasets must remain harness-only")
        if type(self.seed) is not int or isinstance(self.seed, bool):
            raise EvaluationFreezeRefusal("evaluation seed must be an integer")
        if self.generation == 1 and self.supersedes_spec_digest is not None:
            raise EvaluationFreezeRefusal("generation 1 cannot supersede a prior spec")
        if self.generation > 1:
            _digest(self.supersedes_spec_digest, "supersedes_spec_digest")
        if self.content_digest is not None:
            _digest(self.content_digest, "content_digest")

    @property
    def digest(self) -> str:
        return self.content_digest or digest_bytes(self.canonical_bytes())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": EVALUATION_SPEC_SCHEMA,
            "evaluation_id": self.evaluation_id,
            "generation": self.generation,
            "objective_ref": dict(self.objective_ref),
            "criteria": [dict(item) for item in self.criteria],
            "datasets": [dict(item) for item in self.datasets],
            "seed": self.seed,
            "supersedes_spec_digest": self.supersedes_spec_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


def parse_evaluation_spec_bytes(content: bytes) -> EvaluationSpec:
    if not isinstance(content, bytes):
        raise TypeError("evaluation spec content must be bytes")
    try:
        row = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise EvaluationFreezeRefusal(f"evaluation spec must be UTF-8 YAML: {error}") from error
    if not isinstance(row, dict) or row.get("schema") != EVALUATION_SPEC_SCHEMA:
        raise EvaluationFreezeRefusal(
            f"evaluation spec schema must be {EVALUATION_SPEC_SCHEMA}")
    if set(row) != {
            "schema", "evaluation_id", "generation", "objective_ref", "criteria",
            "datasets", "seed", "supersedes_spec_digest"}:
        raise EvaluationFreezeRefusal("evaluation spec fields are not canonical")
    if not isinstance(row["criteria"], list) or not isinstance(row["datasets"], list):
        raise EvaluationFreezeRefusal("evaluation criteria/datasets must be lists")
    return EvaluationSpec(
        row["evaluation_id"], row["generation"],
        _mapping(row["objective_ref"], "objective_ref"),
        tuple(_mapping(item, "criterion") for item in row["criteria"]),
        tuple(_mapping(item, "dataset") for item in row["datasets"]),
        row["seed"], row["supersedes_spec_digest"], digest_bytes(content))


@dataclass(frozen=True)
class EvaluationEvidence:
    candidate_digest: str
    evaluation_spec_digest: str
    evaluation_generation: int
    evaluator_action_id: str
    result: str
    metric_artifacts: tuple[Mapping[str, str], ...]

    def __post_init__(self) -> None:
        _digest(self.candidate_digest, "candidate_digest")
        _digest(self.evaluation_spec_digest, "evaluation_spec_digest")
        if type(self.evaluation_generation) is not int or self.evaluation_generation < 1:
            raise EvaluationFreezeRefusal("evaluation_generation must be positive")
        _nonempty(self.evaluator_action_id, "evaluator_action_id")
        if self.result not in {"pass", "fail"}:
            raise EvaluationFreezeRefusal("evaluation result must be pass or fail")
        for index, metric in enumerate(self.metric_artifacts):
            if set(metric) != {"criterion_id", "reference_id", "digest"}:
                raise EvaluationFreezeRefusal(f"metric_artifacts[{index}] is invalid")
            _digest(metric["digest"], f"metric_artifacts[{index}].digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": EVALUATION_EVIDENCE_SCHEMA,
            "candidate_digest": self.candidate_digest,
            "evaluation_spec_digest": self.evaluation_spec_digest,
            "evaluation_generation": self.evaluation_generation,
            "evaluator_action_id": self.evaluator_action_id,
            "result": self.result,
            "metric_artifacts": [dict(item) for item in self.metric_artifacts],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


def parse_evaluation_evidence_bytes(content: bytes) -> EvaluationEvidence:
    row = _canonical_document(content, EVALUATION_EVIDENCE_SCHEMA, "evaluation evidence")
    if set(row) != {
            "schema", "candidate_digest", "evaluation_spec_digest", "evaluation_generation",
            "evaluator_action_id", "result", "metric_artifacts"}:
        raise EvaluationFreezeRefusal("evaluation evidence fields are not canonical")
    if not isinstance(row["metric_artifacts"], list):
        raise EvaluationFreezeRefusal("metric_artifacts must be a list")
    return EvaluationEvidence(
        row["candidate_digest"], row["evaluation_spec_digest"],
        row["evaluation_generation"], row["evaluator_action_id"], row["result"],
        tuple(_mapping(item, "metric artifact") for item in row["metric_artifacts"]))


def assert_evaluation_generation_available(
    candidate_digest: str,
    spec: EvaluationSpec,
    prior_evidence: Iterable[EvaluationEvidence],
    *,
    holdout_exposed: bool = False,
) -> None:
    """A revealed result or dataset exposure closes this exact generation permanently."""
    candidate_digest = _digest(candidate_digest, "candidate_digest")
    if holdout_exposed:
        raise HoldoutGenerationInvalidated(
            "harness-only dataset was exposed; use a new dataset/spec and higher generation")
    for evidence in prior_evidence:
        if (evidence.candidate_digest == candidate_digest
                and evidence.evaluation_spec_digest == spec.digest
                and evidence.evaluation_generation == spec.generation):
            raise HoldoutGenerationInvalidated(
                "this candidate/spec generation already exposed an evaluation result")


@dataclass(frozen=True)
class ReviewCycle:
    promotion_lineage_id: str
    cycle: int
    target_result_digest: str
    review_digest: str
    supersedes_digest: str | None

    def __post_init__(self) -> None:
        _nonempty(self.promotion_lineage_id, "promotion_lineage_id")
        if type(self.cycle) is not int or self.cycle < 1:
            raise PromotionLineageRefusal("review cycle must be positive")
        _digest(self.target_result_digest, "target_result_digest")
        _digest(self.review_digest, "review_digest")
        if self.cycle == 1 and self.supersedes_digest is not None:
            raise PromotionLineageRefusal("review cycle 1 cannot supersede another cycle")
        if self.cycle > 1:
            _digest(self.supersedes_digest, "supersedes_digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": REVIEW_CYCLE_SCHEMA,
            "promotion_lineage_id": self.promotion_lineage_id,
            "cycle": self.cycle,
            "target_result_digest": self.target_result_digest,
            "review_digest": self.review_digest,
            "supersedes_digest": self.supersedes_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return digest_bytes(self.canonical_bytes())


@dataclass(frozen=True)
class ReviewerEvidence:
    promotion_lineage_id: str
    target_run_spec_digest: str
    candidate_digest: str
    target_result_digest: str
    review_artifact_digest: str
    actor: Mapping[str, str]
    finding_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.promotion_lineage_id, "promotion_lineage_id")
        for field in (
                "target_run_spec_digest", "candidate_digest", "target_result_digest",
                "review_artifact_digest"):
            _digest(getattr(self, field), field)
        if (not isinstance(self.actor, Mapping)
                or set(self.actor) != {"actor_id", "role"}
                or self.actor.get("role") != "reviewer"):
            raise PromotionLineageRefusal(
                "reviewer evidence actor must be an exact reviewer identity")
        _nonempty(self.actor.get("actor_id"), "reviewer actor_id")
        findings = tuple(_digest(item, "reviewer finding digest")
                         for item in self.finding_digests)
        if len(findings) != len(set(findings)):
            raise PromotionLineageRefusal("reviewer finding digests must be unique")
        object.__setattr__(self, "actor", dict(self.actor))
        object.__setattr__(self, "finding_digests", tuple(sorted(findings)))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": REVIEWER_EVIDENCE_SCHEMA,
            "promotion_lineage_id": self.promotion_lineage_id,
            "target_run_spec_digest": self.target_run_spec_digest,
            "candidate_digest": self.candidate_digest,
            "target_result_digest": self.target_result_digest,
            "review_artifact_digest": self.review_artifact_digest,
            "actor": dict(self.actor),
            "finding_digests": list(self.finding_digests),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return digest_bytes(self.canonical_bytes())


def parse_reviewer_evidence_bytes(content: bytes) -> ReviewerEvidence:
    row = _canonical_document(content, REVIEWER_EVIDENCE_SCHEMA, "reviewer evidence")
    if set(row) != {
            "schema", "promotion_lineage_id", "target_run_spec_digest",
            "candidate_digest", "target_result_digest", "review_artifact_digest",
            "actor", "finding_digests"}:
        raise PromotionLineageRefusal("reviewer evidence fields are not canonical")
    finding_digests = row["finding_digests"]
    if not isinstance(finding_digests, list):
        raise PromotionLineageRefusal("reviewer finding digests must be a list")
    return ReviewerEvidence(
        row["promotion_lineage_id"],
        row["target_run_spec_digest"],
        row["candidate_digest"],
        row["target_result_digest"],
        row["review_artifact_digest"],
        row["actor"],
        tuple(finding_digests),
    )


def parse_review_cycle_bytes(content: bytes) -> ReviewCycle:
    row = _canonical_document(content, REVIEW_CYCLE_SCHEMA, "review cycle")
    if set(row) != {
            "schema", "promotion_lineage_id", "cycle", "target_result_digest",
            "review_digest", "supersedes_digest"}:
        raise PromotionLineageRefusal("review cycle fields are not canonical")
    return ReviewCycle(
        row["promotion_lineage_id"], row["cycle"], row["target_result_digest"],
        row["review_digest"], row["supersedes_digest"])


def validate_review_cycle_chain(
    promotion_lineage_id: str,
    cycles: Sequence[ReviewCycle],
    *,
    max_cycles: int,
) -> tuple[int, str | None]:
    if type(max_cycles) is not int or max_cycles < 0:
        raise PromotionLineageRefusal("max_cycles must be non-negative")
    head = None
    for index, cycle in enumerate(cycles, start=1):
        if (cycle.promotion_lineage_id != promotion_lineage_id
                or cycle.cycle != index or cycle.supersedes_digest != head):
            raise PromotionLineageRefusal("review cycle chain is divergent or non-contiguous")
        head = cycle.digest
    if len(cycles) >= max_cycles:
        raise ReviewCycleExhausted(len(cycles), max_cycles)
    return len(cycles), head


def _verified_clearance(
        artifact_store: ArtifactStore, clearance: Mapping[str, object],
        failure_mechanism: object) -> bool:
    try:
        content = artifact_store.read(clearance.get("verification_evidence_digest"))
        candidate = parse_candidate_bytes(
            artifact_store.read(clearance.get("candidate_digest")))
        row = json.loads(content.decode("utf-8"))
    except (WorkflowError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return False
    evidence_fields = {
        "action_id", "actor", "attempt_id", "base_snapshot_digest", "blockers",
        "criterion_results", "engine_check_artifact_digest",
        "engine_check_reference_id", "job_id", "preflight_evidence_digest",
        "result", "run_id", "run_spec_digest", "runner_observation_digest",
        "runner_stderr_digest", "runner_stdout_digest", "schema", "summary",
        "verification_plan_digest", "verifier_binding", "verifier_capability_digest",
        "verifier_sandbox", "worker_actor_id",
    }
    if (not isinstance(row, dict) or set(row) != evidence_fields
            or canonical_json(row) != content
            or row.get("schema") != "waystone-verifier-evidence-1"):
        return False
    actor = row.get("actor")
    binding = row.get("verifier_binding")
    sandbox = row.get("verifier_sandbox")
    if (not isinstance(actor, Mapping) or set(actor) != {"actor_id", "role"}
            or actor.get("role") != "verifier"
            or actor.get("actor_id") == row.get("worker_actor_id")
            or not isinstance(binding, Mapping) or binding.get("role") != "verifier"
            or not isinstance(sandbox, Mapping) or sandbox.get("filesystem") != "read-only"
            or row.get("blockers") != []):
        return False
    criteria = row.get("criterion_results")
    if not isinstance(criteria, list) or not criteria:
        return False
    direct = False
    for criterion in criteria:
        if (not isinstance(criterion, Mapping)
                or set(criterion) != {"criterion", "passed", "evidence_digests"}
                or criterion.get("passed") is not True
                or not isinstance(criterion.get("evidence_digests"), list)
                or not criterion["evidence_digests"]):
            return False
        try:
            for digest in criterion["evidence_digests"]:
                _digest(digest, "clearance criterion evidence")
        except AssurancePlanRefusal:
            return False
        direct = direct or criterion.get("criterion") == failure_mechanism
    result = row.get("result")
    if (not direct or not isinstance(result, Mapping)
            or set(result) != {
                "base_oid", "base_tree_oid", "changed_files", "patch_bytes",
                "result_oid", "result_tree_oid", "result_digest"}
            or result.get("result_oid") != candidate.target_oid):
        return False
    try:
        for field in ("base_oid", "base_tree_oid", "result_oid", "result_tree_oid"):
            _oid(result[field], f"clearance result.{field}")
        _digest(result["result_digest"], "clearance result.result_digest")
    except AssurancePlanRefusal:
        return False
    result_body = {key: value for key, value in result.items() if key != "result_digest"}
    return digest_bytes(canonical_json(result_body)) == result["result_digest"]


def promotion_blockers(
    reviews_dir: Path,
    promotion_lineage_id: str,
    candidate_lineage: Sequence[str],
) -> tuple[str, ...]:
    """Return current fix-before-promotion heads not cleared on this descendant lineage."""
    lineage = tuple(_digest(item, "candidate lineage digest") for item in candidate_lineage)
    if not lineage:
        raise PromotionLineageRefusal("candidate lineage must contain the promoted candidate")
    root = Path(reviews_dir)
    artifact_store = ArtifactStore(root.parent.parent)
    blocked: list[str] = []
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return ()
    for finding_dir in sorted(runs_root.glob("*/findings/*")):
        run_id = finding_dir.parents[1].name
        finding_id = finding_dir.name
        disposition = findings.disposition_head(root, run_id, finding_id)
        if disposition is None:
            continue
        row = disposition.payload
        applies = row.get("applies_to", {})
        if (row.get("disposition") != "fix-before-promotion"
                or applies.get("promotion_lineage_id") != promotion_lineage_id):
            continue
        validation = findings.validation_head(root, run_id, finding_id)
        if (validation is None
                or row.get("confirmed_validation_digest") != validation.digest
                or validation.payload.get("validity") != "confirmed"):
            blocked.append(finding_id)
            continue
        clearance = row.get("clearance")
        cleared = (
            isinstance(clearance, Mapping)
            and _verified_clearance(
                artifact_store, clearance, validation.payload.get("failure_mechanism"))
            and clearance.get("candidate_digest") in lineage
            and clearance.get("supersedes_candidate_digest") in lineage
            and lineage.index(clearance["supersedes_candidate_digest"])
            < lineage.index(clearance["candidate_digest"])
        )
        if not cleared:
            blocked.append(finding_id)
    return tuple(sorted(set(blocked)))


def assert_promotion_unblocked(
    reviews_dir: Path,
    promotion_lineage_id: str,
    candidate_lineage: Sequence[str],
) -> None:
    blockers = promotion_blockers(reviews_dir, promotion_lineage_id, candidate_lineage)
    if blockers:
        raise PromotionBlocked(blockers)


def execute_assurance_dag(
    plan: AssurancePlan,
    handlers: Mapping[str, Callable[[], object]],
    *,
    mutation_digest: Callable[[], str] | None = None,
) -> tuple[tuple[str, object], ...]:
    """Execute the frozen actions once and enforce the stage live-target boundary."""
    if not isinstance(plan, AssurancePlan):
        raise TypeError("plan must be an AssurancePlan")
    if set(handlers) != set(plan.actions):
        raise StageExecutionRefusal(
            "handler set must exactly match frozen actions; runtime add/drop is forbidden")
    results = []
    for action in plan.actions:
        before = None if mutation_digest is None else mutation_digest()
        result = handlers[action]()
        after = None if mutation_digest is None else mutation_digest()
        changed = before is not None and after != before
        mutation_allowed = (
            (plan.lifecycle_stage is LifecycleStage.EXPLORE and action == "worker")
            or (plan.lifecycle_stage is LifecycleStage.PROMOTE and action == "target-ref-apply")
        )
        if changed and not mutation_allowed:
            raise StageMutationRefusal(
                f"{plan.lifecycle_stage.value} action {action!r} mutated the live target")
        results.append((action, result))
    return tuple(results)


__all__ = [
    "ASSURANCE_PLAN_SCHEMA", "AssuranceError", "AssurancePlan",
    "AssurancePlanRefusal", "AmbiguousStageRefusal", "CANDIDATE_SCHEMA", "Candidate",
    "CandidateRefusal", "EVALUATION_EVIDENCE_SCHEMA", "EVALUATION_SPEC_SCHEMA",
    "EvaluationEvidence", "EvaluationFreezeRefusal", "EvaluationSpec",
    "HoldoutGenerationInvalidated", "PromotionBlocked", "PromotionLineageRefusal",
    "REVIEW_CYCLE_SCHEMA", "REVIEWER_EVIDENCE_SCHEMA", "ReviewCycle",
    "ReviewCycleExhausted", "ReviewerEvidence",
    "StageExecutionRefusal", "StageMutationRefusal",
    "assert_evaluation_generation_available", "assert_promotion_unblocked", "canonical_json",
    "compile_assurance_plan", "digest_bytes", "execute_assurance_dag",
    "parse_assurance_plan_bytes",
    "parse_candidate_bytes", "parse_evaluation_evidence_bytes", "parse_evaluation_spec_bytes",
    "parse_review_cycle_bytes", "parse_reviewer_evidence_bytes", "promotion_blockers",
    "select_lifecycle_stage",
    "validate_review_cycle_chain",
]
