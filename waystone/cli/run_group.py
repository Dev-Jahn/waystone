"""`waystone run` user and carrier transport surface."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Mapping

from waystone.adapters.git import GitReadError, git_full_sha, git_rc, git_read_bytes
from waystone.core import WorkflowError
from waystone.jobs import completion
from waystone.jobs.profile import RunAssembly as ProductionRunAssembly, assemble_run
from waystone.jobs.run_scaffold import scaffold_outcome_delta, scaffold_work_brief
from waystone.jobs.work_brief import import_owner_source_file, parse_work_brief_bytes
from waystone.project.brief import ProjectFactRef, read_project_frame_at_commit
from waystone.project.context import resolve_project_context
from waystone.runs.artifacts import ArtifactReference, ArtifactReferenceKind
from waystone.runs.assurance import (
    PromotionLineageRefusal,
    compile_assurance_plan,
    digest_bytes,
    parse_candidate_bytes,
    parse_evaluation_evidence_bytes,
)
from waystone.runs.engine import (
    CancelReason,
    ResumeResult,
    RunEngine,
    StagedRunEngine,
    load_review_cycle_chain,
)
from waystone.runs.preflight import PreflightError
from waystone.runs.spec import PromotionLineage, ResultPolicy, load_run_spec
from waystone.runs.transport import (
    ActionPlanRefusal,
    PreflightFailure,
    TransportError,
    encode_envelope,
    failure_envelope,
)


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ActionPlanRefusal(f"{label} must be a regular non-symlink file")
        return path.read_bytes()
    except ActionPlanRefusal:
        raise
    except OSError as error:
        raise ActionPlanRefusal(f"cannot read {label}: {error}") from error


def _start_arguments(
    args: list[str],
) -> tuple[str, Path, bool, Path | None, str | None, Path | None]:
    if not args:
        raise ActionPlanRefusal(
            "start requires a task id and exactly one of --work-brief or --work-brief-draft")
    task_id = args[0]
    work_brief = None
    semantic_draft = False
    owner_request = None
    stage = None
    from_worktree = None
    index = 1
    seen: set[str] = set()
    while index < len(args):
        option = args[index]
        if option not in {
                "--work-brief", "--work-brief-draft", "--owner-request", "--stage",
                "--from-worktree"}:
            raise ActionPlanRefusal(f"unexpected start argument {option!r}")
        if option in seen or index + 1 >= len(args):
            raise ActionPlanRefusal(f"{option} requires exactly one value")
        seen.add(option)
        value = args[index + 1]
        if option == "--work-brief":
            if work_brief is not None:
                raise ActionPlanRefusal(
                    "start accepts exactly one of --work-brief or --work-brief-draft")
            work_brief = Path(value)
        elif option == "--work-brief-draft":
            if work_brief is not None:
                raise ActionPlanRefusal(
                    "start accepts exactly one of --work-brief or --work-brief-draft")
            work_brief = Path(value)
            semantic_draft = True
        elif option == "--owner-request":
            owner_request = Path(value)
        elif option == "--stage":
            stage = value
        else:
            from_worktree = Path(value)
        index += 2
    if work_brief is None:
        raise ActionPlanRefusal(
            "start requires exactly one of --work-brief or --work-brief-draft")
    return task_id, work_brief, semantic_draft, owner_request, stage, from_worktree


def _close_arguments(args: list[str]) -> tuple[str, Path, bool]:
    if len(args) != 3 or args[1] not in {"--outcome", "--outcome-draft"}:
        raise ActionPlanRefusal(
            "close requires <run-id> and exactly one of --outcome or --outcome-draft")
    return args[0], Path(args[2]), args[1] == "--outcome-draft"


def _project_fact_refs(payload: object) -> tuple[ProjectFactRef, ...]:
    found: dict[tuple[object, ...], ProjectFactRef] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("kind") == "project-fact":
                key = (
                    value.get("commit"), value.get("path"), value.get("fact_id"),
                    value.get("fact_digest"), value.get("binding"),
                )
                found[key] = ProjectFactRef(
                    commit=value["commit"],
                    path=value["path"],
                    fact_id=value["fact_id"],
                    fact_digest=value["fact_digest"],
                    binding=value["binding"],
                )
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return tuple(found[key] for key in sorted(found))


def _compile_completion(
    assembly: ProductionRunAssembly,
    brief,
) -> completion.CompletionContract:
    objective = brief.objective.ref.to_dict()
    prefix = objective.get("fact_id", "").partition("/")[0]
    extended = [
        expectation for expectation in brief.evidence_expected
        if expectation.text is not None or expectation.source is not None
    ]
    if extended:
        if len(extended) != len(brief.evidence_expected):
            raise ActionPlanRefusal(
                "evidence_expected cannot mix authority-complete and legacy-shaped criteria")
        mode = {"explore": "learning", "evaluate": "evaluation",
                "promote": "promotion"}[brief.lifecycle_stage]
        criteria = [{
            "id": expectation.criterion_id,
            "mode": mode,
            "text": expectation.text,
            "source": expectation.source.to_dict(),
            "binding": (
                "nonbinding" if brief.lifecycle_stage == "explore" else "binding"),
            "evidence": {"kind": expectation.kind},
        } for expectation in brief.evidence_expected]
        return completion.compile_completion_contract(
            assembly.context.active_worktree_root,
            brief.lifecycle_stage,
            brief.objective.ref,
            criteria,
            artifact_store=assembly.artifact_store,
        )
    if brief.lifecycle_stage == "explore" and (
            objective.get("kind") != "project-fact"
            or prefix not in {"hypothesis", "question"}
            or objective.get("binding") != "nonbinding"):
        raise ActionPlanRefusal(
            "the WorkBrief does not identify authority for an explore learning criterion; "
            "A2 will not infer it from prose")
    if objective["kind"] == "project-fact":
        source_frame = read_project_frame_at_commit(
            assembly.context.active_worktree_root, objective["commit"])
        criterion_text = source_frame.fact(objective["fact_id"]).raw_bytes.decode(
            "utf-8", "strict").strip()
    elif objective["kind"] == "owner-request":
        criterion_text = assembly.artifact_store.read(objective["digest"]).decode(
            "utf-8", "strict").strip()
    else:
        raise ActionPlanRefusal(
            "the WorkBrief does not carry criterion authority for this objective variant")
    mode = {
        "explore": "learning",
        "promote": "promotion",
    }[brief.lifecycle_stage]
    criteria = [{
        "id": expectation.criterion_id,
        "mode": mode,
        "text": criterion_text,
        "source": objective,
        "binding": objective["binding"],
        "evidence": {"kind": expectation.kind},
    } for expectation in brief.evidence_expected]
    return completion.compile_completion_contract(
        assembly.context.active_worktree_root,
        brief.lifecycle_stage,
        brief.objective.ref,
        criteria,
        artifact_store=assembly.artifact_store,
    )


def _evidence_sources(brief) -> tuple[Mapping[str, object], ...]:
    items = [*brief.current_state, *brief.known_failures, *brief.constraints,
             *brief.non_goals, *brief.open_questions]
    sources = []
    for item in items:
        for source in item.sources:
            payload = source.to_dict()
            if "reference_id" in payload:
                sources.append(payload)
    return tuple(sources)


def _one_evidence_source(brief, prefix: str) -> Mapping[str, object]:
    matches = tuple(
        source for source in _evidence_sources(brief)
        if str(source.get("reference_id", "")).startswith(prefix))
    if len(matches) != 1:
        raise ActionPlanRefusal(
            f"{brief.lifecycle_stage} requires exactly one {prefix} evidence source")
    return matches[0]


def _parse_declared_risks_bytes(content: bytes) -> tuple[str, ...]:
    if not isinstance(content, bytes):
        raise TypeError("accepted-risks content must be bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PromotionLineageRefusal(
            "accepted-risks record must be UTF-8") from error
    lines = tuple(line.strip() for line in text.splitlines())
    if not lines or any(not line for line in lines):
        raise PromotionLineageRefusal(
            "accepted-risks record must contain non-empty risk lines or exactly 'none'")
    if lines == ("none",):
        return ()
    if "none" in lines or len(lines) != len(set(lines)):
        raise PromotionLineageRefusal(
            "accepted-risks cannot mix 'none' with risks or repeat a risk id")
    return tuple(sorted(lines))


def _candidate_input(assembly: ProductionRunAssembly, brief):
    source = _one_evidence_source(brief, "candidate:")
    candidate = parse_candidate_bytes(
        assembly.artifact_store.read(source["digest"]))
    descriptor = {
        "reference_id": source["reference_id"],
        "digest": source["digest"],
        "target_ref": candidate.target_ref,
        "target_oid": candidate.target_oid,
        "code_sha": candidate.code_sha,
        "config_digest": candidate.config_digest,
        "producer_result_digest": candidate.producer["result_digest"],
    }
    producer = load_run_spec(
        candidate.producer["run_id"], start=assembly.context.canonical_root)
    if producer.run_spec_digest != candidate.producer["run_spec_digest"]:
        raise ActionPlanRefusal("candidate producer RunSpec digest does not match descriptor")
    return candidate, descriptor, producer


def _evaluation_spec_input(brief) -> Mapping[str, object]:
    matches = []
    for expectation in brief.evidence_expected:
        if expectation.source is None:
            continue
        source = expectation.source.to_dict()
        if source.get("kind") == "evaluation-spec":
            matches.append(source)
    unique = {completion.canonical_json(item): item for item in matches}
    if len(unique) != 1:
        raise ActionPlanRefusal(
            f"{brief.lifecycle_stage} requires one frozen evaluation-spec AuthorityRef")
    source = next(iter(unique.values()))
    return {"commit": source["commit"], "path": source["path"],
            "digest": source["digest"], "generation": source["generation"]}


def _integration_target(root: Path) -> str:
    returncode, target, error = git_rc(root, "symbolic-ref", "--quiet", "HEAD")
    if returncode != 0:
        raise ActionPlanRefusal(
            "candidate-producing explore requires an attached integration target ref: "
            + (error or f"git exited {returncode}"))
    if not target.startswith("refs/heads/"):
        raise ActionPlanRefusal("integration target must be an attached refs/heads/* ref")
    return target


def _root_objective_digest(brief) -> str:
    return digest_bytes(completion.canonical_json(brief.objective.ref.to_dict()))


def _start_with_assembly(
    assembly: ProductionRunAssembly,
    task_id: str,
    work_brief_path: Path,
    semantic_draft: bool,
    owner_request_path: Path | None,
    stage_assertion: str | None,
):
    brief_content = _regular_bytes(
        work_brief_path, "WorkBrief semantic draft" if semantic_draft else "WorkBrief")
    if semantic_draft:
        brief_content = scaffold_work_brief(assembly, task_id, brief_content)
    assembly.artifact_store.write(brief_content)
    brief = parse_work_brief_bytes(
        brief_content, artifact_store=assembly.artifact_store)
    if stage_assertion is not None and stage_assertion != brief.lifecycle_stage:
        raise ActionPlanRefusal(
            f"--stage {stage_assertion!r} differs from WorkBrief stage "
            f"{brief.lifecycle_stage!r}")
    objective = brief.objective.ref.to_dict()
    owner_reference = None
    if objective["kind"] == "owner-request":
        if owner_request_path is None:
            raise ActionPlanRefusal(
                "--owner-request is required for an owner-request WorkBrief objective")
        ingress = import_owner_source_file(
            assembly.context.active_worktree_root,
            owner_request_path,
            reference_id=objective["artifact_reference_id"],
            declared_digest=objective["digest"],
            artifact_store=assembly.artifact_store,
        )
        owner_reference = ArtifactReference(
            ingress.reference_id,
            ArtifactReferenceKind.INPUT,
            ingress.artifact.digest,
            ingress.artifact.size,
        )
    elif owner_request_path is not None:
        raise ActionPlanRefusal(
            "--owner-request is forbidden unless objective_ref.kind is owner-request")
    contract = _compile_completion(assembly, brief)
    root = assembly.context.active_worktree_root
    evaluation_spec = None
    candidate_descriptor = None
    evaluation = None
    promotion_lineage = None
    result_policy = None
    review_cycles = ()
    if brief.lifecycle_stage in {"evaluate", "promote"}:
        _candidate, candidate_descriptor, candidate_producer = _candidate_input(
            assembly, brief)
        if brief.lifecycle_stage == "evaluate":
            evaluation_spec = _evaluation_spec_input(brief)
            target = _integration_target(root)
            promotion_lineage = PromotionLineage(
                id=brief.brief_id,
                root_objective_ref_digest=_root_objective_digest(brief),
                integration_target_ref=target,
                parent_run_spec_digest=candidate_producer.run_spec_digest,
                candidate_chain_head_digest=candidate_descriptor["digest"],
                review_cycle_head_digest=None,
            )
            evaluation = {"spec": dict(evaluation_spec), "evidence": None}
            result_policy = ResultPolicy("evidence-only", None, None)
        else:
            evidence_source = _one_evidence_source(brief, "evaluation-evidence:")
            evidence_value = parse_evaluation_evidence_bytes(
                assembly.artifact_store.read(evidence_source["digest"]))
            evaluation_run_id = str(evidence_source["reference_id"]).removeprefix(
                "evaluation-evidence:")
            evaluation_producer = load_run_spec(
                evaluation_run_id, start=assembly.context.canonical_root)
            if (evaluation_producer.promotion_lineage is None
                    or evaluation_producer.candidate != candidate_descriptor):
                raise ActionPlanRefusal(
                    "evaluation evidence producer does not carry the promoted candidate lineage")
            producer_spec = evaluation_producer.evaluation.get("spec")
            if not isinstance(producer_spec, Mapping):
                raise ActionPlanRefusal(
                    "evaluation evidence producer lacks its frozen evaluation spec")
            evaluation_spec = dict(producer_spec)
            if (evidence_value.result != "pass"
                    or evidence_value.candidate_digest != candidate_descriptor["digest"]
                    or evidence_value.evaluation_spec_digest != evaluation_spec["digest"]
                    or evidence_value.evaluation_generation != evaluation_spec["generation"]):
                raise ActionPlanRefusal(
                    "promotion requires passed evidence for the exact candidate/spec generation")
            inherited = evaluation_producer.promotion_lineage
            review_cycles = load_review_cycle_chain(
                assembly, inherited.id, inherited.review_cycle_head_digest)
            review_cycle_head = (
                review_cycles[-1].digest if review_cycles
                else inherited.review_cycle_head_digest)
            promotion_lineage = PromotionLineage(
                inherited.id, inherited.root_objective_ref_digest,
                inherited.integration_target_ref, evaluation_producer.run_spec_digest,
                candidate_descriptor["digest"], review_cycle_head)
            evaluation = {
                "spec": dict(evaluation_spec),
                "evidence": {
                    "reference_id": evidence_source["reference_id"],
                    "digest": evidence_source["digest"],
                    "generation": evidence_value.evaluation_generation,
                },
            }
            expected_oid = git_full_sha(root, inherited.integration_target_ref)
            if expected_oid is None:
                raise ActionPlanRefusal("promotion integration target is not locally reachable")
            result_policy = ResultPolicy(
                "integration-ref", inherited.integration_target_ref, expected_oid)
    declared_risks = ()
    if brief.lifecycle_stage == "promote":
        risks_source = _one_evidence_source(brief, "accepted-risks:")
        declared_risks = _parse_declared_risks_bytes(
            assembly.artifact_store.read(risks_source["digest"]))
    assurance_content = compile_assurance_plan(
        brief.lifecycle_stage,
        declared_risks=declared_risks,
        evaluation_spec=evaluation_spec,
        completion_contract={
            "reference_id": "completion-contract:<pending>",
            "digest": digest_bytes(contract.canonical_bytes()),
        },
        compiled_from=(() if evaluation_spec is None else ({
            "kind": "evaluation-spec",
            **evaluation_spec,
        },)),
        promotion_lineage_id=(
            None if promotion_lineage is None else promotion_lineage.id),
        review_cycles=review_cycles,
    ).canonical_bytes()
    head = git_full_sha(root)
    if head is None:
        raise ActionPlanRefusal("active worktree HEAD cannot be resolved")
    frame = read_project_frame_at_commit(
        assembly.context.active_worktree_root, head)
    facts = _project_fact_refs(brief.to_dict())
    return StagedRunEngine(assembly).start(
        task_id,
        work_brief_content=brief_content,
        completion_contract_content=contract.canonical_bytes(),
        assurance_plan_content=assurance_content,
        frame_status_ref=frame.status_ref,
        project_fact_refs=facts,
        owner_request_reference=owner_reference,
        promotion_lineage=promotion_lineage,
        candidate=candidate_descriptor,
        evaluation=evaluation,
        result_policy=result_policy,
    )


def _latest_run_id(assembly: ProductionRunAssembly) -> str:
    with assembly.store._connection_lock:  # noqa: SLF001 - public latest-run projection
        row = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if row is None:
        raise ActionPlanRefusal("no run exists")
    return row["run_id"]


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _failure(error: BaseException) -> int:
    if isinstance(error, TransportError):
        failure = error
    elif isinstance(error, PreflightError):
        detail = getattr(error, "detail", None)
        failure = PreflightFailure(
            detail if isinstance(detail, str) and detail else str(error))
    elif isinstance(error, WorkflowError):
        detail = getattr(error, "detail", None)
        failure = ActionPlanRefusal(
            detail if isinstance(detail, str) and detail else str(error))
    else:
        failure = error
    exit_code, envelope = failure_envelope(failure)
    print(encode_envelope(envelope).decode("utf-8"))
    return int(exit_code)


def _run_id(engine: RunEngine, value: str | None) -> str:
    return engine.latest_run_id() if value is None else value


def _resume_text(result: ResumeResult) -> str:
    if result.completion is not None:
        return (
            f"Run {result.run_id} completed on private integration ref "
            f"{result.completion.applied.target_ref}. Live-tree delivery is not performed."
        )
    if result.cancellation is not None:
        return f"Run {result.run_id} cancellation state: {result.cancellation.state}"
    if result.dispatch is not None:
        branch = result.dispatch
        if branch.get("engine") == "busy":
            return f"Run {result.run_id} is progressing; poll after {branch['poll_after_s']}s."
        if branch.get("engine") == "idle":
            return f"Run {result.run_id}: {branch['reason']}"
        action = branch.get("action")
        if isinstance(action, dict):
            return f"Run {result.run_id} is waiting for {action.get('executor_kind')} action."
    return f"Run {result.run_id} has no renderable result."


def _parse_optional_json(args: list[str]) -> tuple[str | None, bool]:
    run_id = None
    as_json = False
    for arg in args:
        if arg == "--json":
            if as_json:
                raise ActionPlanRefusal("--json may be passed only once")
            as_json = True
        elif run_id is None:
            run_id = arg
        else:
            raise ActionPlanRefusal(f"unexpected argument {arg!r}")
    return run_id, as_json


def main(argv: list[str]) -> int:
    """Parse one run subcommand and render only public projections/envelopes."""
    try:
        if not argv:
            raise ActionPlanRefusal(
                "expected start, resume, status, watch, cancel, or actions")
        command, args = argv[0], argv[1:]

        if command == "start":
            (task_id, work_brief, semantic_draft, owner_request,
             stage, from_worktree) = _start_arguments(args)
            # ProjectContext is proven before profile/config/intent/DB/file ingress.
            context = resolve_project_context(
                Path.cwd(),
                from_worktree=from_worktree,
                require_run_input=True,
            )
            with assemble_run(context) as assembly:
                result = _start_with_assembly(
                    assembly, task_id, work_brief, semantic_draft, owner_request, stage)
            print(
                f"Run {result.spec.run_id} started at {result.spec.lifecycle_stage.value} "
                f"with RunSpec revision {result.spec.revision}."
            )
            return 0

        if command == "resume":
            if len(args) > 1:
                raise ActionPlanRefusal("resume accepts at most one run id")
            context = resolve_project_context(Path.cwd())
            with assemble_run(context) as assembly:
                identity = args[0] if args else _latest_run_id(assembly)
                branch = StagedRunEngine(assembly).resume(identity)
            print(_resume_text(ResumeResult(identity, dispatch=branch)))
            return 0

        if command == "close":
            identity, outcome_path, semantic_draft = _close_arguments(args)
            context = resolve_project_context(Path.cwd())
            outcome_content = _regular_bytes(
                outcome_path,
                "OutcomeDelta semantic draft" if semantic_draft else "OutcomeDelta",
            )
            with assemble_run(context) as assembly:
                if semantic_draft:
                    outcome_content = scaffold_outcome_delta(
                        assembly, identity, outcome_content)
                result = StagedRunEngine(assembly).close(identity, outcome_content)
            print(
                f"Run {result.run_id} completed with outcome ledger commit "
                f"{result.commit_oid}."
            )
            return 0

        if command == "context":
            if not args or args[0] not in {"show", "provide"}:
                raise ActionPlanRefusal("context requires show or provide")
            context = resolve_project_context(Path.cwd())
            with assemble_run(context) as assembly:
                staged = StagedRunEngine(assembly)
                if args[0] == "show":
                    if len(args) != 2:
                        raise ActionPlanRefusal("context show requires exactly one run id")
                    pending = staged.pending_context(args[1])
                    print(_json({
                        "request": dict(pending.request),
                        "request_digest": pending.request_digest,
                        "run_id": pending.run_id,
                    }))
                else:
                    if len(args) != 4 or args[2] != "--response":
                        raise ActionPlanRefusal(
                            "context provide requires <run-id> --response <file>")
                    response = _regular_bytes(Path(args[3]), "context response")
                    resumed = staged.provide_context(args[1], response)
                    print(
                        f"Run {resumed.spec.run_id} resumed with RunSpec revision "
                        f"{resumed.spec.revision} and attempt {resumed.attempt_id}."
                    )
            return 0

        # Read/cancel/action surfaces still use the existing M1-B facade, but canonical
        # context is resolved before they can discover or open project-local state.
        context = resolve_project_context(Path.cwd())
        root = context.canonical_root
        engine = RunEngine(root)

        if command == "status":
            run_id, as_json = _parse_optional_json(args)
            identity = _run_id(engine, run_id)
            if as_json:
                print(_json(engine.status_json(identity)))
            else:
                print(engine.status_human(identity))
            return 0

        if command == "watch":
            if len(args) > 1:
                raise ActionPlanRefusal("watch accepts at most one run id")
            identity = _run_id(engine, args[0] if args else None)
            try:
                for frame in engine.watch(identity):
                    print(frame, flush=True)
            except KeyboardInterrupt:
                return 130
            return 0

        if command == "cancel":
            if len(args) != 3 or args[1] != "--reason":
                raise ActionPlanRefusal(
                    "cancel requires <run-id> --reason user-requested")
            try:
                reason = CancelReason(args[2])
            except ValueError as error:
                raise ActionPlanRefusal(
                    "cancel reason must be user-requested") from error
            result = engine.cancel(args[0], reason)
            print(f"Run {result.run_id} cancellation state: {result.state}")
            return 0

        if command == "actions":
            if not args:
                raise ActionPlanRefusal("actions requires next or submit")
            action_command, action_args = args[0], args[1:]
            if action_command == "next":
                if len(action_args) != 2 or action_args[1] != "--json":
                    raise ActionPlanRefusal(
                        "actions next requires <run-id> --json")
                print(_json(engine.actions_next(action_args[0])))
                return 0
            if action_command == "submit":
                if len(action_args) != 3 or action_args[1] != "--file":
                    raise ActionPlanRefusal(
                        "actions submit requires <action-id> --file <result>")
                path = Path(action_args[2])
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise ActionPlanRefusal(
                        f"cannot read canonical result file: {error}") from error
                if not isinstance(payload, dict):
                    raise ActionPlanRefusal("result file must contain one JSON object")
                print(_json(engine.actions_submit(action_args[0], payload)))
                return 0
            raise ActionPlanRefusal(f"unknown actions command {action_command!r}")

        if command == "deliver":
            raise ActionPlanRefusal(
                "run deliver is not implemented in M1-B; delivery policy belongs to M2")
        raise ActionPlanRefusal(f"unknown run command {command!r}")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:  # CLI boundary turns every failure into one typed envelope
        return _failure(error)


__all__ = ["main"]
