from __future__ import annotations

from support import *  # noqa: F401,F403

import json
import sys
import time
from types import SimpleNamespace
from unittest import mock

from test_work_brief import completion_contract, init_project, payload
from waystone.features.review_layout import new_run_id
from waystone.jobs import completion, work_brief
from waystone.jobs.profile import RunAssembly as ProductionRunAssembly
from waystone.project.context import ProjectContext
from waystone.reviews import findings
from waystone.runs.artifacts import ArtifactStore
from waystone.runs.assurance import (
    AmbiguousStageRefusal,
    AssurancePlanRefusal,
    Candidate,
    EvaluationEvidence,
    EvaluationSpec,
    HoldoutGenerationInvalidated,
    PromotionBlocked,
    PromotionLineageRefusal,
    ReviewCycle,
    ReviewCycleExhausted,
    ReviewerEvidence,
    StageMutationRefusal,
    assert_evaluation_generation_available,
    assert_promotion_unblocked,
    canonical_json,
    compile_assurance_plan,
    digest_bytes,
    execute_assurance_dag,
    parse_assurance_plan_bytes,
    parse_candidate_bytes,
    select_lifecycle_stage,
    validate_review_cycle_chain,
)
from waystone.runs.preflight import (
    NetworkCacheRequirements,
    SandboxContract,
    VerificationPlanDefinition,
)
from waystone.runs.effects import EffectEngine, RunnerExecutionEffect
from waystone.runs.engine import EngineBindingRefusal, StagedRunEngine
from waystone.runs.lease import LeaseManager
from waystone.runs.spec import PromotionLineage, ResultPolicy, plan_one_task_run
from waystone.runs.store import FilesystemInfo, RunStore
from waystone.runs.supervisor import RunnerInvocation, Supervisor
from waystone.runs.worker_result import parse_runner_completion_marker_v2_bytes


class AssurancePlanTests(unittest.TestCase):
    def evaluation_ref(self):
        return {"commit": "a" * 40, "path": "docs/evaluations/e/spec.yaml",
                "digest": "sha256:" + "1" * 64, "generation": 1}

    def test_stage_compiler_freezes_representative_exact_dags_and_ambiguous_refuses(self):
        explore = compile_assurance_plan("explore")
        evaluate = compile_assurance_plan("evaluate", evaluation_spec=self.evaluation_ref())
        promote = compile_assurance_plan(
            "promote", evaluation_spec=self.evaluation_ref(),
            promotion_lineage_id=new_run_id(), declared_risks=("trust-surface-store",))

        self.assertEqual(explore.actions, (
            "worker", "result-adapter", "candidate-publish", "completion"))
        self.assertEqual(evaluate.actions, (
            "freeze", "read-only-evaluator", "evaluation-evidence", "completion"))
        self.assertEqual(promote.actions, (
            "evaluated-candidate-freeze", "independent-verify", "adversarial-review",
            "integration-decision", "target-ref-apply", "completion"))
        self.assertEqual(explore.action_requirements["independent-verify"], "not-required")
        self.assertEqual(evaluate.review["max_cycles"], 2)
        self.assertEqual(promote.review["max_cycles"], 2)
        self.assertEqual(promote.completion["allowed_outcomes"], [
            "executable-capability", "measured-improvement",
            "validated-decision", "simplification"])
        self.assertEqual(parse_assurance_plan_bytes(promote.canonical_bytes()), promote)
        lowered = json.loads(evaluate.canonical_bytes())
        lowered["action_requirements"]["read-only-evaluator"] = "not-required"
        lowered["actions"].remove("read-only-evaluator")
        with self.assertRaises(AssurancePlanRefusal):
            parse_assurance_plan_bytes(canonical_json(lowered))
        with self.assertRaises(AmbiguousStageRefusal):
            select_lifecycle_stage(None, intent="change some code")

    def test_each_stage_executes_only_frozen_actions_and_evaluate_mutation_refuses(self):
        plans = (
            compile_assurance_plan("explore"),
            compile_assurance_plan("evaluate", evaluation_spec=self.evaluation_ref()),
            compile_assurance_plan(
                "promote", evaluation_spec=self.evaluation_ref(),
                promotion_lineage_id=new_run_id()),
        )
        for plan in plans:
            seen = []
            handlers = {
                action: (lambda action=action: seen.append(action) or action)
                for action in plan.actions
            }
            result = execute_assurance_dag(plan, handlers)
            self.assertEqual(tuple(seen), plan.actions)
            self.assertEqual(tuple(item[0] for item in result), plan.actions)

        evaluate = plans[1]
        live = {"digest": "before"}
        handlers = {action: (lambda: None) for action in evaluate.actions}
        handlers["read-only-evaluator"] = lambda: live.update(digest="after")
        with self.assertRaises(StageMutationRefusal):
            execute_assurance_dag(
                evaluate, handlers, mutation_digest=lambda: live["digest"])

    def test_check_free_explore_preflight_definition_is_valid(self):
        definition = VerificationPlanDefinition(
            required_checks=(), required_toolchains=(), environment_preparation=(),
            network_cache_requirements=NetworkCacheRequirements(
                False, (), "none", True),
            verifier_sandbox=SandboxContract("read-only", "isolated", "denied"),
        )
        self.assertEqual(definition.required_checks, ())


class CandidateEvaluationTests(unittest.TestCase):
    def candidate(self, supersedes=None):
        return Candidate(
            new_run_id(),
            {"run_id": new_run_id(), "run_spec_digest": "sha256:" + "2" * 64,
             "result_digest": "sha256:" + "3" * 64},
            "a" * 40, "sha256:" + "4" * 64,
            "refs/waystone/candidates/run", "a" * 40,
            supersedes, (),
        )

    def evaluation_spec(self):
        return EvaluationSpec(
            new_run_id(), 1,
            {"kind": "owner-request", "artifact_reference_id": "owner:one",
             "digest": "sha256:" + "5" * 64, "binding": "binding"},
            ({"id": "accuracy", "metric": "exact-match", "operator": "gte",
              "threshold": 0.9},),
            ({"id": "holdout", "artifact_reference_id": "dataset:one",
              "digest": "sha256:" + "6" * 64, "visibility": "harness-only"},),
            42, None,
        )

    def test_candidate_descriptor_is_content_bound_and_supersedes_is_explicit(self):
        first = self.candidate()
        second = self.candidate(first.digest)
        self.assertEqual(parse_candidate_bytes(second.canonical_bytes()), second)
        self.assertEqual(second.supersedes_candidate_digest, first.digest)

    def test_observed_evaluation_generation_cannot_be_reused(self):
        candidate = self.candidate()
        spec = self.evaluation_spec()
        evidence = EvaluationEvidence(
            candidate.digest, spec.digest, 1, "evaluate:one", "fail", ())
        with self.assertRaises(HoldoutGenerationInvalidated):
            assert_evaluation_generation_available(candidate.digest, spec, (evidence,))
        with self.assertRaises(HoldoutGenerationInvalidated):
            assert_evaluation_generation_available(
                candidate.digest, spec, (), holdout_exposed=True)

    def test_r2_extended_expectation_compiles_frozen_evaluation_criterion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head, frame = init_project(root)
            evaluation = (
                "schema: waystone-evaluation-spec-1\n"
                f"evaluation_id: {new_run_id()}\n"
                "generation: 1\n"
                "objective_ref: {kind: project-fact, commit: '" + head
                + "', path: PROJECT_BRIEF.md, fact_id: commitment/outcome, fact_digest: '"
                + frame.fact("commitment/outcome").digest + "', binding: binding}\n"
                "criteria:\n  - {id: accuracy, metric: exact-match, operator: gte, threshold: 0.9}\n"
                "datasets:\n  - {id: holdout, artifact_reference_id: dataset:one, digest: 'sha256:"
                + "7" * 64 + "', visibility: harness-only}\n"
                "seed: 42\nsupersedes_spec_digest: null\n"
            ).encode()
            evaluation_path = root / "docs/evaluations/demo/spec.yaml"
            evaluation_path.parent.mkdir(parents=True)
            evaluation_path.write_bytes(evaluation)
            git(root, "add", str(evaluation_path.relative_to(root)))
            self.assertEqual(git(root, "commit", "-qm", "evaluation spec").returncode, 0)
            evaluation_commit = git(root, "rev-parse", "HEAD").stdout.strip()
            body = payload(head, frame, new_run_id())
            body["lifecycle_stage"] = "evaluate"
            body["objective"]["ref"] = frame.fact_ref("commitment/outcome").to_dict()
            source = {"kind": "evaluation-spec", "commit": evaluation_commit,
                      "path": "docs/evaluations/demo/spec.yaml", "generation": 1,
                      "digest": "sha256:" + hashlib.sha256(evaluation).hexdigest()}
            body["evidence_expected"] = [{
                "criterion_id": "accuracy", "kind": "evaluation-evidence",
                "text": "Accuracy is at least 0.9.", "source": source,
            }]
            content = completion.canonical_json(body)
            parsed = work_brief.parse_work_brief_bytes(content)
            contract = completion.compile_completion_contract(
                root, "evaluate", parsed.objective.ref, [{
                    "id": "accuracy", "mode": "evaluation",
                    "text": parsed.evidence_expected[0].text,
                    "source": parsed.evidence_expected[0].source.to_dict(),
                    "binding": "binding", "evidence": {"kind": "evaluation-evidence"},
                }])
            rebound = work_brief.parse_work_brief_bytes(
                content, completion_contract=contract)
            self.assertEqual(rebound.evidence_expected[0].source.to_dict(), source)
            with self.assertRaises(completion.StageModeRefusal):
                completion.compile_completion_contract(
                    root, "evaluate", parsed.objective.ref, [{
                        "id": "hypothesis-is-not-evaluation-authority", "mode": "evaluation",
                        "text": "Do not elevate a hypothesis into an evaluation criterion.",
                        "source": frame.fact_ref("hypothesis/solver").to_dict(),
                        "binding": "binding", "evidence": {"kind": "evaluation-evidence"},
                    }])

    def test_evaluate_and_promote_runspec_freeze_one_candidate_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            head, frame = init_project(root)
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "  - id: feat/semantic-brief\n    title: Stage candidate\n"
                "    status: pending\n    scope: [src.py]\n    deps: []\n",
                encoding="utf-8")
            git(root, "add", "tasks.yaml")
            self.assertEqual(git(root, "commit", "-qm", "task").returncode, 0)
            store = ArtifactStore(root)
            explore_contract = completion_contract(root, frame)
            explore_brief = completion.canonical_json(
                payload(head, frame, new_run_id()))
            filesystem = mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo("apfs", Path("/"), writable=True))
            with filesystem:
                explore = plan_one_task_run(
                    "feat/semantic-brief", work_brief_content=explore_brief,
                    completion_contract_content=explore_contract.canonical_bytes(),
                    assurance_plan_content=compile_assurance_plan("explore").canonical_bytes(),
                    frame_status_ref=frame.status_ref,
                    project_fact_refs=(frame.fact_ref("hypothesis/solver"),), start=root)
            candidate_ref = f"refs/waystone/candidates/{explore.run_id}"
            candidate_oid = git(root, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(
                git(root, "update-ref", candidate_ref, candidate_oid).returncode, 0)
            worker_result = store.write(b"frozen worker result")
            candidate = Candidate(
                new_run_id(),
                {"run_id": explore.run_id, "run_spec_digest": explore.run_spec_digest,
                 "result_digest": worker_result.digest},
                candidate_oid, "sha256:" + "8" * 64,
                candidate_ref, candidate_oid, None, ())
            candidate_artifact = store.write(candidate.canonical_bytes())
            evaluation_body = {
                "schema": "waystone-evaluation-spec-1", "evaluation_id": new_run_id(),
                "generation": 1,
                "objective_ref": frame.fact_ref("commitment/outcome").to_dict(),
                "criteria": [{"id": "accuracy", "metric": "exact-match",
                              "operator": "gte", "threshold": 0.9}],
                "datasets": [{"id": "holdout", "artifact_reference_id": "dataset:one",
                              "digest": "sha256:" + "9" * 64,
                              "visibility": "harness-only"}],
                "seed": 42, "supersedes_spec_digest": None,
            }
            evaluation_bytes = yaml.safe_dump(evaluation_body, sort_keys=True).encode()
            evaluation_path = root / "docs/evaluations/demo/spec.yaml"
            evaluation_path.parent.mkdir(parents=True)
            evaluation_path.write_bytes(evaluation_bytes)
            git(root, "add", str(evaluation_path.relative_to(root)))
            self.assertEqual(git(root, "commit", "-qm", "evaluation").returncode, 0)
            evaluation_commit = git(root, "rev-parse", "HEAD").stdout.strip()
            evaluation_ref = {
                "kind": "evaluation-spec", "commit": evaluation_commit,
                "path": "docs/evaluations/demo/spec.yaml", "generation": 1,
                "digest": "sha256:" + hashlib.sha256(evaluation_bytes).hexdigest(),
            }
            evaluate_payload = payload(head, frame, new_run_id())
            evaluate_payload["lifecycle_stage"] = "evaluate"
            evaluate_payload["objective"]["ref"] = frame.fact_ref(
                "commitment/outcome").to_dict()
            evaluate_payload["evidence_expected"] = [{
                "criterion_id": "accuracy", "kind": "evaluation-evidence",
                "text": "Accuracy reaches the frozen threshold.", "source": evaluation_ref,
            }]
            evaluate_brief = completion.canonical_json(evaluate_payload)
            parsed_evaluate = work_brief.parse_work_brief_bytes(evaluate_brief)
            evaluate_contract = completion.compile_completion_contract(
                root, "evaluate", parsed_evaluate.objective.ref, [{
                    "id": "accuracy", "mode": "evaluation",
                    "text": "Accuracy reaches the frozen threshold.",
                    "source": evaluation_ref, "binding": "binding",
                    "evidence": {"kind": "evaluation-evidence"},
                }])
            descriptor = {
                "reference_id": f"candidate:{explore.run_id}",
                "digest": candidate_artifact.digest, "target_ref": candidate_ref,
                "target_oid": candidate_oid, "code_sha": candidate_oid,
                "config_digest": candidate.config_digest,
                "producer_result_digest": worker_result.digest,
            }
            lineage_id = new_run_id()
            lineage = PromotionLineage(
                lineage_id,
                "sha256:" + hashlib.sha256(completion.canonical_json(
                    parsed_evaluate.objective.ref.to_dict())).hexdigest(),
                "refs/heads/main", explore.run_spec_digest,
                candidate_artifact.digest, None)
            evaluation_descriptor = {
                "commit": evaluation_commit, "path": evaluation_ref["path"],
                "digest": evaluation_ref["digest"], "generation": 1,
            }
            with filesystem:
                evaluate = plan_one_task_run(
                    "feat/semantic-brief", work_brief_content=evaluate_brief,
                    completion_contract_content=evaluate_contract.canonical_bytes(),
                    assurance_plan_content=compile_assurance_plan(
                        "evaluate", evaluation_spec=evaluation_descriptor,
                        promotion_lineage_id=lineage_id).canonical_bytes(),
                    frame_status_ref=frame.status_ref,
                    project_fact_refs=(frame.fact_ref("commitment/outcome"),),
                    promotion_lineage=lineage, candidate=descriptor,
                    evaluation={"spec": evaluation_descriptor, "evidence": None},
                    result_policy=ResultPolicy("evidence-only", None, None), start=root)
            self.assertEqual(evaluate.candidate["digest"], candidate_artifact.digest)

            evidence = EvaluationEvidence(
                candidate_artifact.digest, evaluation_ref["digest"], 1,
                f"{evaluate.run_id}:evaluator", "pass", ())
            evidence_artifact = store.write(evidence.canonical_bytes())
            evidence_ref = {
                "kind": "evaluation-evidence",
                "reference_id": f"evaluation-evidence:{evaluate.run_id}",
                "candidate_digest": candidate_artifact.digest,
                "generation": 1, "digest": evidence_artifact.digest,
            }
            promote_payload = payload(head, frame, new_run_id())
            promote_payload["lifecycle_stage"] = "promote"
            promote_payload["objective"]["ref"] = frame.fact_ref(
                "commitment/outcome").to_dict()
            promote_payload["evidence_expected"] = [{
                "criterion_id": "accuracy", "kind": "regression-contract",
                "text": "Promote the passed frozen generation.", "source": evidence_ref,
            }]
            promote_brief = completion.canonical_json(promote_payload)
            parsed_promote = work_brief.parse_work_brief_bytes(promote_brief)
            promote_contract = completion.compile_completion_contract(
                root, "promote", parsed_promote.objective.ref, [{
                    "id": "accuracy", "mode": "promotion",
                    "text": "Promote the passed frozen generation.",
                    "source": evidence_ref, "binding": "binding",
                    "evidence": {"kind": "regression-contract"},
                }], artifact_store=store)
            promote_lineage = PromotionLineage(
                lineage.id, lineage.root_objective_ref_digest,
                lineage.integration_target_ref, evaluate.run_spec_digest,
                candidate_artifact.digest, None)
            expected_old = git(root, "rev-parse", "refs/heads/main").stdout.strip()
            with filesystem:
                promote = plan_one_task_run(
                    "feat/semantic-brief", work_brief_content=promote_brief,
                    completion_contract_content=promote_contract.canonical_bytes(),
                    assurance_plan_content=compile_assurance_plan(
                        "promote", evaluation_spec=evaluation_descriptor,
                        promotion_lineage_id=lineage_id).canonical_bytes(),
                    frame_status_ref=frame.status_ref,
                    project_fact_refs=(frame.fact_ref("commitment/outcome"),),
                    promotion_lineage=promote_lineage, candidate=descriptor,
                    evaluation={"spec": evaluation_descriptor, "evidence": {
                        "reference_id": evidence_ref["reference_id"],
                        "digest": evidence_artifact.digest, "generation": 1}},
                    result_policy=ResultPolicy(
                        "integration-ref", "refs/heads/main", expected_old), start=root)
            self.assertEqual(promote.evaluation["evidence"]["digest"], evidence_artifact.digest)
            with RunStore.open(root) as run_store:
                context = ProjectContext(
                    "project:assurance-test", root, root, root / ".git",
                    "sha256:" + "a" * 64, root / ".waystone" / "state.sqlite3")
                assembly = ProductionRunAssembly(
                    context, None, {}, run_store, store, None, None, None, None)
                called = []
                handlers = {
                    action: (lambda action=action: called.append(action))
                    for action in parse_assurance_plan_bytes(
                        store.read(promote.assurance_plan.digest)).actions
                }
                with self.assertRaises(EngineBindingRefusal):
                    StagedRunEngine(assembly).execute_stage(promote.run_id, handlers)
                self.assertEqual(called, [])


class PromotionLineageTests(unittest.TestCase):
    def d(self, value):
        token = format(value, "x")
        return "sha256:" + (token * 64)[:64]

    def test_review_cycle_count_rederives_and_cannot_be_reset(self):
        lineage = new_run_id()
        first = ReviewCycle(lineage, 1, self.d(1), self.d(2), None)
        second = ReviewCycle(lineage, 2, self.d(3), self.d(4), first.digest)
        with self.assertRaises(ReviewCycleExhausted) as raised:
            validate_review_cycle_chain(lineage, (first, second), max_cycles=2)
        self.assertEqual(raised.exception.waiting_user()["options"], [
            "accept-risk", "reduce-supported-scope", "simplify-architecture",
            "approve-separate-research-track"])
        with self.assertRaises(PromotionLineageRefusal):
            compile_assurance_plan(
                "promote", evaluation_spec={"digest": self.d(5), "generation": 1},
                promotion_lineage_id=lineage, consumed_review_cycles=1,
                review_cycles=(first, second))

    def test_e2e4_durable_review_head_rejects_new_run_budget_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            init_project(root)
            artifacts = ArtifactStore(root)
            filesystem = mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo("apfs", Path("/"), writable=True),
            )
            with filesystem, RunStore.open(root) as store:
                run = store.create_run(initial_state="dispatch-ready")
                job_id = f"{run.run_id}:job"
                store.create_job(run.run_id, job_id, initial_state="running")
                lineage_id = new_run_id()
                plan = compile_assurance_plan(
                    "promote",
                    evaluation_spec={"digest": self.d(31), "generation": 1},
                    promotion_lineage_id=lineage_id,
                    declared_risks=("public-contract",),
                )
                plan_artifact = artifacts.write(plan.canonical_bytes())
                spec = SimpleNamespace(
                    run_id=run.run_id,
                    job_id=job_id,
                    run_spec_digest=self.d(36),
                    candidate={
                        "digest": self.d(33),
                        "producer_result_digest": self.d(34),
                    },
                    assurance_plan=SimpleNamespace(digest=plan_artifact.digest),
                    promotion_lineage=PromotionLineage(
                        lineage_id, self.d(32), "refs/heads/main", None,
                        self.d(33), None),
                )
                context = ProjectContext(
                    "project:review-cycle", root, root, root / ".git",
                    "canonical", root / ".waystone" / "state.db",
                )
                assembly = ProductionRunAssembly(
                    context, None, {}, store, artifacts, None, None, None, None)
                reviewer = ReviewerEvidence(
                    lineage_id, spec.run_spec_digest, spec.candidate["digest"],
                    self.d(34), self.d(35),
                    {"actor_id": "reviewer", "role": "reviewer"}, (),
                )
                artifacts.write(reviewer.canonical_bytes())
                with mock.patch("waystone.runs.engine.load_run_spec", return_value=spec):
                    cycle = StagedRunEngine(assembly).append_review_cycle(
                        run.run_id,
                        target_result_digest=self.d(34),
                        review_digest=reviewer.digest,
                    )

                reference = store.get_artifact_reference(
                    f"review-cycle:{lineage_id}:1")
                self.assertEqual(reference.digest, cycle.digest)
                from waystone.runs.engine import load_review_cycle_chain
                saved = load_review_cycle_chain(assembly, lineage_id, None)
                descendant = compile_assurance_plan(
                    "promote",
                    evaluation_spec={"digest": self.d(31), "generation": 1},
                    promotion_lineage_id=lineage_id,
                    consumed_review_cycles=0,
                    review_cycles=saved,
                )
                self.assertEqual(descendant.review["consumed_cycles"], 1)
                self.assertEqual(
                    descendant.review["cycle_chain_head_digest"], cycle.digest)

    def test_fix_before_promotion_blocks_until_verified_descendant_clearance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, frame = init_project(root)
            reviews = root / "docs" / "reviews"
            run_id = new_run_id()
            finding_id = new_run_id()
            lineage = new_run_id()
            artifacts = ArtifactStore(root)
            old_descriptor = Candidate(
                new_run_id(),
                {"run_id": new_run_id(), "run_spec_digest": self.d(6),
                 "result_digest": self.d(7)},
                "b" * 40, self.d(8), "refs/waystone/candidates/old",
                "b" * 40, None, ())
            old_candidate = artifacts.write(old_descriptor.canonical_bytes()).digest
            new_descriptor = Candidate(
                new_run_id(),
                {"run_id": new_run_id(), "run_spec_digest": self.d(9),
                 "result_digest": self.d(10)},
                "c" * 40, self.d(11), "refs/waystone/candidates/new",
                "c" * 40, old_candidate, ())
            new_candidate = artifacts.write(new_descriptor.canonical_bytes()).digest
            validation_evidence = artifacts.write(b"reproduced promotion failure")
            claim = findings.write_claim(reviews, {
                "schema": findings.CLAIM_SCHEMA, "finding_id": finding_id,
                "review_run_id": run_id,
                "target": {"run_spec_digest": self.d(12), "result_digest": self.d(13),
                           "review_artifact_digest": self.d(14)},
                "source_finding_id": "WS-B2-001", "claim": "failure remains",
                "evidence": ["reproduced"],
                "reviewer_assessment": {"impact": "major", "suggested_remediation": None},
                "reported_by": {"role": "reviewer", "binding_digest": self.d(15),
                                "principal": None},
            })
            validation = findings.append_validation(reviews, run_id, finding_id, {
                "schema": findings.VALIDATION_SCHEMA, "finding_id": finding_id,
                "finding_digest": claim.digest, "revision": 1, "supersedes_digest": None,
                "validity": "confirmed", "failure_mechanism": "X because Y",
                "evidence_refs": [{"kind": "code", "digest": validation_evidence.digest}],
                "validated_by": {"role": "coordinator", "binding_digest": self.d(17),
                                 "principal": None},
            }, root=root)
            base = {
                "schema": findings.DISPOSITION_SCHEMA, "finding_id": finding_id,
                "finding_digest": claim.digest,
                "confirmed_validation_digest": validation.digest,
                "revision": 1, "supersedes_digest": None,
                "objective_ref": frame.fact_ref("commitment/outcome").to_dict(),
                "lifecycle_stage": "promote",
                "applies_to": {"promotion_lineage_id": lineage,
                               "candidate_digest": old_candidate, "result_digest": self.d(19)},
                "impact": "major", "exposure": "common", "relevance": "promotion-bound",
                "disposition": "fix-before-promotion", "remediation_scope": "local",
                "estimated_cost": "low", "rationale": "must repair", "clearance": None,
                "decided_by": {"role": "coordinator", "binding_digest": self.d(20),
                               "principal": None}, "materialized_task_id": None,
            }
            initial = findings.append_disposition(
                reviews, run_id, finding_id, base, root=root)
            with self.assertRaises(PromotionBlocked):
                assert_promotion_unblocked(reviews, lineage, (old_candidate, new_candidate))

            result = {
                "base_oid": "b" * 40, "base_tree_oid": "d" * 40,
                "changed_files": [], "patch_bytes": "", "result_oid": "c" * 40,
                "result_tree_oid": "e" * 40,
            }
            result["result_digest"] = digest_bytes(canonical_json(result))
            verification = artifacts.write(canonical_json({
                "schema": "waystone-verifier-evidence-1",
                "run_id": new_run_id(), "job_id": "clearance:job",
                "attempt_id": "clearance:attempt", "action_id": "clearance:verify",
                "worker_actor_id": "worker",
                "actor": {"actor_id": "independent-verifier", "role": "verifier"},
                "run_spec_digest": self.d(21), "base_snapshot_digest": self.d(22),
                "verification_plan_digest": self.d(23),
                "preflight_evidence_digest": self.d(24),
                "engine_check_artifact_digest": self.d(25),
                "engine_check_reference_id": "engine-check:clearance",
                "runner_observation_digest": self.d(26),
                "runner_stdout_digest": self.d(27),
                "runner_stderr_digest": self.d(28),
                "verifier_binding": {"backend": "fixture:verifier",
                                     "execution_category": "external", "role": "verifier"},
                "verifier_sandbox": {"filesystem": "read-only", "network": "denied",
                                     "process": "isolated"},
                "verifier_capability_digest": self.d(29),
                "result": result,
                "criterion_results": [{"criterion": "X because Y", "passed": True,
                                       "evidence_digests": [self.d(30)]}],
                "blockers": [], "summary": "failure mechanism no longer reproduces",
            }))
            cleared = dict(base)
            cleared.update({
                "revision": 2, "supersedes_digest": initial.digest,
                "clearance": {"candidate_digest": new_candidate,
                              "supersedes_candidate_digest": old_candidate,
                              "verification_evidence_digest": verification.digest},
            })
            findings.append_disposition(
                reviews, run_id, finding_id, cleared, root=root)
            assert_promotion_unblocked(reviews, lineage, (old_candidate, new_candidate))


class MarkerV2ProductionWiringTests(unittest.TestCase):
    def test_staged_runner_adapts_once_before_marker_v2_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            head, frame = init_project(root)
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "  - id: feat/semantic-brief\n"
                "    title: Explore candidate\n    status: pending\n"
                "    scope: [src.py]\n    deps: []\n", encoding="utf-8")
            git(root, "add", "tasks.yaml")
            self.assertEqual(git(root, "commit", "-qm", "task").returncode, 0)
            contract = completion_contract(root, frame)
            brief = completion.canonical_json(payload(head, frame, new_run_id()))
            with mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo("apfs", Path("/"), writable=True)):
                spec = plan_one_task_run(
                    "feat/semantic-brief", work_brief_content=brief,
                    completion_contract_content=contract.canonical_bytes(),
                    assurance_plan_content=compile_assurance_plan("explore").canonical_bytes(),
                    frame_status_ref=frame.status_ref,
                    project_fact_refs=(frame.fact_ref("hypothesis/solver"),), start=root)
                attempt_id = f"{spec.run_id}:attempt:1"
                action_id = f"{spec.run_id}:worker"
                result = (
                    "schema: waystone-worker-result-1\nstatus: completed\n"
                    f"run_spec_digest: {spec.run_spec_digest}\n"
                    f"attempt_id: {attempt_id}\n"
                    "result_summary: Candidate explored.\nevidence_refs: []\n"
                )
                worker = (
                    "from pathlib import Path\n"
                    f"Path('WAYSTONE_RESULT.yaml').write_text({result!r}, encoding='utf-8')\n"
                )
                with RunStore.open(root) as store:
                    store.create_attempt(spec.run_id, spec.job_id, attempt_id)
                    leases = LeaseManager(store)
                    invocation_digest = "sha256:" + hashlib.sha256(b"worker-v2").hexdigest()
                    supervisor = Supervisor(
                        store, leases,
                        invocations={invocation_digest: RunnerInvocation(
                            (sys.executable, "-c", worker), root)},
                        heartbeat_interval=0.05, lease_ttl=0.25)
                    effects = EffectEngine(
                        store, leases, runner_executor=supervisor.runner_executor,
                        runner_identity_verifier=supervisor.runner_identity_verifier)
                    effect_plan = effects.plan_effect(
                        spec.run_id, spec.job_id, attempt_id, action_id,
                        RunnerExecutionEffect(invocation_digest))
                    claimed = effects.claim_effect(effect_plan, ttl_seconds=5)
                    effects.execute_effect(claimed)
                    marker_path = Path(effect_plan.spec["completion_marker"])
                    deadline = time.monotonic() + 10
                    while not marker_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker_path.exists())
                    marker = parse_runner_completion_marker_v2_bytes(marker_path.read_bytes())
                    control = (root / "WAYSTONE_RESULT.yaml").read_bytes()
                    self.assertEqual(
                        marker.worker_result_digest,
                        "sha256:" + hashlib.sha256(control).hexdigest())
                    observed = effects.reconcile_actions(
                        (action_id,), quiescence_probe=supervisor.quiescence_probe)[0]
                    self.assertIn(observed.state.value, {"completed", "no-op"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
