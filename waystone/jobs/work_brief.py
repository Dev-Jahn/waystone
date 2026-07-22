"""Semantic WorkBrief validation, CAS ingress, lineage, and prompt projection."""
from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from waystone.core import WorkflowError
from waystone.features.review_layout import require_uuid7
from waystone.project import TASK_ID_RE
from waystone.runs.artifacts import ArtifactStore, StoredArtifact, validate_sha256_digest

from .completion import (
    AuthorityRef,
    AuthorityRefRefusal,
    CompletionContract,
    ObjectiveRef,
    canonical_json,
    parse_authority_ref,
    parse_objective_ref,
)


WORK_BRIEF_SCHEMA = "waystone-work-brief-1"
PROVENANCE_KINDS = frozenset((
    "owner-source", "harness-observation", "coordinator-summary",
))
LIFECYCLE_STAGES = frozenset(("explore", "evaluate", "promote"))
_FULL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class WorkBriefError(WorkflowError):
    """Base class for typed WorkBrief refusals."""

    code = "work_brief_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class WorkBriefSchemaRefusal(WorkBriefError):
    code = "work_brief_schema_refusal"


class ProvenanceRefusal(WorkBriefError):
    code = "work_brief_provenance_refusal"


class WorkBriefLineageRefusal(WorkBriefError):
    code = "work_brief_lineage_refusal"


class WorkBriefDigestRefusal(WorkBriefError):
    code = "work_brief_digest_refusal"


class WorkBriefContractRefusal(WorkBriefError):
    code = "work_brief_contract_refusal"


class OwnerSourceIngressRefusal(WorkBriefError):
    code = "owner_source_ingress_refusal"


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkBriefSchemaRefusal(f"{field}: must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _string(value, field)
    try:
        return validate_sha256_digest(value)
    except ValueError as error:
        raise WorkBriefSchemaRefusal(f"{field}: {error}") from error


def _path(value: Any, field: str) -> str:
    value = _string(value, field)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise WorkBriefSchemaRefusal(f"{field}: must be a relative project path")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkBriefSchemaRefusal(f"{field}: must be a mapping")
    return dict(value)


def _exact_fields(row: Mapping[str, Any], fields: set[str], field: str) -> None:
    if set(row) != fields:
        missing = sorted(fields - set(row))
        unknown = sorted(set(row) - fields)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise WorkBriefSchemaRefusal(f"{field}: " + "; ".join(detail))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkBriefSchemaRefusal(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _decode_canonical_json(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, WorkBriefSchemaRefusal):
            raise
        raise WorkBriefSchemaRefusal(f"WorkBrief must be canonical UTF-8 JSON: {error}") from error
    if not isinstance(payload, dict):
        raise WorkBriefSchemaRefusal("WorkBrief must contain one JSON object")
    if canonical_json(payload) != content:
        raise WorkBriefSchemaRefusal(
            "WorkBrief bytes must use sorted-key compact canonical JSON")
    return payload


@dataclass(frozen=True)
class SemanticSource:
    payload: dict[str, Any]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class SemanticItem:
    text: str
    provenance: str
    sources: tuple[SemanticSource, ...]
    source_form: str

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"text": self.text, "provenance": self.provenance}
        if self.source_form == "source":
            row["source"] = self.sources[0].to_dict()
        else:
            row["sources"] = [source.to_dict() for source in self.sources]
        return row


@dataclass(frozen=True)
class WorkObjective:
    ref: ObjectiveRef
    desired_delta: str
    why_now: SemanticItem

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.to_dict(),
            "desired_delta": self.desired_delta,
            "why_now": self.why_now.to_dict(),
        }


@dataclass(frozen=True)
class DecisionSpace:
    fixed: tuple[SemanticItem, ...]
    worker_may_choose: tuple[SemanticItem, ...]
    requires_escalation: tuple[SemanticItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed": [item.to_dict() for item in self.fixed],
            "worker_may_choose": [item.to_dict() for item in self.worker_may_choose],
            "requires_escalation": [item.to_dict() for item in self.requires_escalation],
        }


@dataclass(frozen=True)
class EvidenceExpectation:
    criterion_id: str
    kind: str
    text: str | None = None
    source: AuthorityRef | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"criterion_id": self.criterion_id, "kind": self.kind}
        if self.text is not None and self.source is not None:
            row["text"] = self.text
            row["source"] = self.source.to_dict()
        return row


@dataclass(frozen=True)
class RelevantReference:
    path: str
    anchor: str
    digest: str
    purpose: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "anchor": self.anchor,
            "digest": self.digest,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class WorkBrief:
    brief_id: str
    task_id: str
    revision: int
    supersedes_digest: str | None
    resolves_context_request_digest: str | None
    lifecycle_stage: str
    objective: WorkObjective
    current_state: tuple[SemanticItem, ...]
    decisions: DecisionSpace
    constraints: tuple[SemanticItem, ...]
    non_goals: tuple[SemanticItem, ...]
    known_failures: tuple[SemanticItem, ...]
    evidence_expected: tuple[EvidenceExpectation, ...]
    references: tuple[RelevantReference, ...]
    open_questions: tuple[SemanticItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORK_BRIEF_SCHEMA,
            "brief_id": self.brief_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "supersedes_digest": self.supersedes_digest,
            "resolves_context_request_digest": self.resolves_context_request_digest,
            "lifecycle_stage": self.lifecycle_stage,
            "objective": self.objective.to_dict(),
            "current_state": [item.to_dict() for item in self.current_state],
            "decisions": self.decisions.to_dict(),
            "constraints": [item.to_dict() for item in self.constraints],
            "non_goals": [item.to_dict() for item in self.non_goals],
            "known_failures": [item.to_dict() for item in self.known_failures],
            "evidence_expected": [item.to_dict() for item in self.evidence_expected],
            "references": [item.to_dict() for item in self.references],
            "open_questions": [item.to_dict() for item in self.open_questions],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class CASIngress:
    reference_id: str
    artifact: StoredArtifact


@dataclass(frozen=True)
class PublishedWorkBrief:
    brief: WorkBrief
    reference_id: str
    artifact: StoredArtifact


def _semantic_source(value: Any, field: str) -> SemanticSource:
    row = _mapping(value, field)
    kind = row.get("kind")
    try:
        if kind in {
            "project-fact", "owner-request", "milestone", "accepted-adr",
            "evaluation-spec", "evaluation-evidence",
        }:
            source = parse_authority_ref(row, field).to_dict()
        elif kind == "git":
            _exact_fields(row, {"kind", "commit", "path", "digest"}, field)
            commit = _string(row["commit"], f"{field}.commit")
            if _FULL_COMMIT_RE.fullmatch(commit) is None:
                raise ProvenanceRefusal(f"{field}.commit: must be a full lowercase Git object id")
            source = {
                "kind": "git",
                "commit": commit,
                "path": _path(row["path"], f"{field}.path"),
                "digest": _digest(row["digest"], f"{field}.digest"),
            }
        elif kind in ("owner-artifact", "evidence"):
            allowed = {"kind", "digest"} | ({"reference_id"} if "reference_id" in row else set())
            _exact_fields(row, allowed, field)
            source = {"kind": kind, "digest": _digest(row["digest"], f"{field}.digest")}
            if "reference_id" in row:
                source["reference_id"] = _string(row["reference_id"], f"{field}.reference_id")
        else:
            raise ProvenanceRefusal(f"{field}.kind: unsupported provenance source")
    except (AuthorityRefRefusal, WorkBriefSchemaRefusal) as error:
        raise ProvenanceRefusal(str(error)) from error
    digest = source.get("fact_digest") or source.get("item_digest") or source.get("digest")
    if not isinstance(digest, str):
        raise ProvenanceRefusal(f"{field}: source is not digest-bound")
    return SemanticSource(source, digest)


def _semantic_item(value: Any, field: str) -> SemanticItem:
    row = _mapping(value, field)
    base = {"text", "provenance"}
    if set(row) == base | {"source"}:
        source_form = "source"
        sources = (_semantic_source(row["source"], f"{field}.source"),)
    elif set(row) == base | {"sources"}:
        source_form = "sources"
        raw_sources = row["sources"]
        if not isinstance(raw_sources, list):
            raise ProvenanceRefusal(f"{field}.sources: must be a list")
        sources = tuple(
            _semantic_source(source, f"{field}.sources[{index}]")
            for index, source in enumerate(raw_sources)
        )
    else:
        raise ProvenanceRefusal(
            f"{field}: fields must be text/provenance and exactly one of source/sources")
    text = _string(row["text"], f"{field}.text")
    provenance = row["provenance"]
    if provenance not in PROVENANCE_KINDS:
        raise ProvenanceRefusal(f"{field}.provenance: unsupported provenance label")
    if provenance in ("owner-source", "harness-observation") and not sources:
        raise ProvenanceRefusal(f"{field}: {provenance} requires a digest-bound source")
    source_kinds = {source.payload["kind"] for source in sources}
    if provenance == "owner-source" and not source_kinds.issubset({
        "project-fact", "owner-request", "milestone", "owner-artifact",
    }):
        raise ProvenanceRefusal(
            f"{field}: owner-source must cite owner bytes or a Git-tracked owner fact")
    if provenance == "harness-observation" and not source_kinds.issubset({
        "git", "evidence", "accepted-adr", "evaluation-spec", "evaluation-evidence",
    }):
        raise ProvenanceRefusal(
            f"{field}: harness-observation must cite Git/store/evidence bytes")
    return SemanticItem(text, provenance, sources, source_form)


def _semantic_list(value: Any, field: str) -> tuple[SemanticItem, ...]:
    if not isinstance(value, list):
        raise WorkBriefSchemaRefusal(f"{field}: must be a list")
    return tuple(_semantic_item(item, f"{field}[{index}]") for index, item in enumerate(value))


def _objective(value: Any) -> WorkObjective:
    row = _mapping(value, "objective")
    _exact_fields(row, {"ref", "desired_delta", "why_now"}, "objective")
    try:
        ref = parse_objective_ref(row["ref"], "objective.ref")
    except AuthorityRefRefusal as error:
        raise WorkBriefSchemaRefusal(str(error)) from error
    return WorkObjective(
        ref,
        _string(row["desired_delta"], "objective.desired_delta"),
        _semantic_item(row["why_now"], "objective.why_now"),
    )


def _decisions(value: Any) -> DecisionSpace:
    row = _mapping(value, "decisions")
    _exact_fields(row, {"fixed", "worker_may_choose", "requires_escalation"}, "decisions")
    return DecisionSpace(
        _semantic_list(row["fixed"], "decisions.fixed"),
        _semantic_list(row["worker_may_choose"], "decisions.worker_may_choose"),
        _semantic_list(row["requires_escalation"], "decisions.requires_escalation"),
    )


def _expectations(value: Any) -> tuple[EvidenceExpectation, ...]:
    if not isinstance(value, list) or not value:
        raise WorkBriefSchemaRefusal("evidence_expected: must be a non-empty list")
    result = []
    for index, item in enumerate(value):
        row = _mapping(item, f"evidence_expected[{index}]")
        base = {"criterion_id", "kind"}
        if set(row) == base:
            text = None
            source = None
        elif set(row) == base | {"text", "source"}:
            text = _string(row["text"], f"evidence_expected[{index}].text")
            try:
                source = parse_authority_ref(
                    row["source"], f"evidence_expected[{index}].source")
            except AuthorityRefRefusal as error:
                raise WorkBriefSchemaRefusal(str(error)) from error
        else:
            _exact_fields(row, base | {"text", "source"}, f"evidence_expected[{index}]")
            raise AssertionError("unreachable")
        result.append(EvidenceExpectation(
            _string(row["criterion_id"], f"evidence_expected[{index}].criterion_id"),
            _string(row["kind"], f"evidence_expected[{index}].kind"),
            text,
            source,
        ))
    ids = [item.criterion_id for item in result]
    if len(ids) != len(set(ids)):
        raise WorkBriefSchemaRefusal("evidence_expected criterion ids must be unique")
    return tuple(result)


def _references(value: Any) -> tuple[RelevantReference, ...]:
    if not isinstance(value, list):
        raise WorkBriefSchemaRefusal("references: must be a list")
    result = []
    for index, item in enumerate(value):
        row = _mapping(item, f"references[{index}]")
        _exact_fields(row, {"path", "anchor", "digest", "purpose"}, f"references[{index}]")
        result.append(RelevantReference(
            _path(row["path"], f"references[{index}].path"),
            _string(row["anchor"], f"references[{index}].anchor"),
            _digest(row["digest"], f"references[{index}].digest"),
            _string(row["purpose"], f"references[{index}].purpose"),
        ))
    return tuple(result)


def _validate_contract(brief: WorkBrief, contract: CompletionContract) -> None:
    if brief.lifecycle_stage != contract.lifecycle_stage.value:
        raise WorkBriefContractRefusal("WorkBrief and CompletionContract lifecycle stages differ")
    if brief.objective.ref.to_dict() != contract.objective_ref.to_dict():
        raise WorkBriefContractRefusal("WorkBrief and CompletionContract objective refs differ")
    criteria = {criterion.id: criterion for criterion in contract.criteria}
    expected = {item.criterion_id: item for item in brief.evidence_expected}
    if set(expected) != set(criteria):
        raise WorkBriefContractRefusal(
            "evidence_expected criterion ids must exactly match CompletionContract")
    for criterion_id, expectation in expected.items():
        if expectation.kind != criteria[criterion_id].evidence.kind:
            raise WorkBriefContractRefusal(
                f"evidence_expected kind mismatch for criterion {criterion_id!r}")
        if expectation.text is not None and expectation.text != criteria[criterion_id].text:
            raise WorkBriefContractRefusal(
                f"evidence_expected text mismatch for criterion {criterion_id!r}")
        if (expectation.source is not None
                and expectation.source.to_dict() != criteria[criterion_id].source.to_dict()):
            raise WorkBriefContractRefusal(
                f"evidence_expected source mismatch for criterion {criterion_id!r}")


def parse_work_brief_bytes(
    content: bytes,
    *,
    artifact_store: ArtifactStore | None = None,
    completion_contract: CompletionContract | None = None,
    context_resume: bool = False,
    _seen: frozenset[str] = frozenset(),
) -> WorkBrief:
    """Validate canonical bytes and recursively verify any declared predecessor from CAS."""
    if not isinstance(content, bytes):
        raise TypeError("WorkBrief content must be bytes")
    payload = _decode_canonical_json(content)
    fields = {
        "schema", "brief_id", "task_id", "revision", "supersedes_digest",
        "resolves_context_request_digest", "lifecycle_stage", "objective", "current_state",
        "decisions", "constraints", "non_goals", "known_failures", "evidence_expected",
        "references", "open_questions",
    }
    _exact_fields(payload, fields, "WorkBrief")
    if payload["schema"] != WORK_BRIEF_SCHEMA:
        raise WorkBriefSchemaRefusal(f"schema must be {WORK_BRIEF_SCHEMA}")
    try:
        brief_id = require_uuid7(payload["brief_id"])
    except WorkflowError as error:
        raise WorkBriefSchemaRefusal(f"brief_id: {error}") from error
    task_id = _string(payload["task_id"], "task_id")
    if TASK_ID_RE.fullmatch(task_id) is None:
        raise WorkBriefSchemaRefusal("task_id: must be a canonical task id")
    revision = payload["revision"]
    if type(revision) is not int or revision < 1:
        raise WorkBriefSchemaRefusal("revision: must be a positive integer")
    supersedes = payload["supersedes_digest"]
    if revision == 1:
        if supersedes is not None:
            raise WorkBriefLineageRefusal("revision 1 must have supersedes_digest: null")
    else:
        if supersedes is None:
            raise WorkBriefLineageRefusal("revision > 1 requires supersedes_digest")
        supersedes = _digest(supersedes, "supersedes_digest")
    context_digest = payload["resolves_context_request_digest"]
    if context_digest is not None:
        context_digest = _digest(context_digest, "resolves_context_request_digest")
    if revision == 1 and context_digest is not None:
        raise WorkBriefLineageRefusal("revision 1 cannot resolve a context request")
    if context_resume and context_digest is None:
        raise WorkBriefLineageRefusal("context resume requires resolves_context_request_digest")
    stage = payload["lifecycle_stage"]
    if stage not in LIFECYCLE_STAGES:
        raise WorkBriefSchemaRefusal("lifecycle_stage must be explore, evaluate, or promote")

    brief = WorkBrief(
        brief_id=brief_id,
        task_id=task_id,
        revision=revision,
        supersedes_digest=supersedes,
        resolves_context_request_digest=context_digest,
        lifecycle_stage=stage,
        objective=_objective(payload["objective"]),
        current_state=_semantic_list(payload["current_state"], "current_state"),
        decisions=_decisions(payload["decisions"]),
        constraints=_semantic_list(payload["constraints"], "constraints"),
        non_goals=_semantic_list(payload["non_goals"], "non_goals"),
        known_failures=_semantic_list(payload["known_failures"], "known_failures"),
        evidence_expected=_expectations(payload["evidence_expected"]),
        references=_references(payload["references"]),
        open_questions=_semantic_list(payload["open_questions"], "open_questions"),
    )
    if brief.lifecycle_stage == "evaluate" and any(
            item.text is None or item.source is None for item in brief.evidence_expected):
        raise WorkBriefSchemaRefusal(
            "evaluate evidence_expected items require criterion text and frozen source")

    if revision > 1:
        if artifact_store is None:
            raise WorkBriefLineageRefusal(
                "revision > 1 requires CAS access to validate the actual predecessor")
        assert supersedes is not None
        if supersedes in _seen:
            raise WorkBriefLineageRefusal("WorkBrief supersedes lineage contains a cycle")
        try:
            predecessor_bytes = artifact_store.read(supersedes)
        except WorkflowError as error:
            raise WorkBriefLineageRefusal(
                f"cannot load declared predecessor {supersedes}: {error}") from error
        predecessor = parse_work_brief_bytes(
            predecessor_bytes,
            artifact_store=artifact_store,
            _seen=_seen | {supersedes},
        )
        if predecessor.revision != revision - 1:
            raise WorkBriefLineageRefusal(
                "supersedes_digest must name the immediately previous revision")
        if predecessor.brief_id != brief.brief_id or predecessor.task_id != brief.task_id:
            raise WorkBriefLineageRefusal(
                "supersedes_digest names a different WorkBrief lineage")
        if predecessor.lifecycle_stage != brief.lifecycle_stage:
            raise WorkBriefLineageRefusal("WorkBrief revision cannot change lifecycle stage")

    if completion_contract is not None:
        _validate_contract(brief, completion_contract)
    return brief


def _regular_file_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise OwnerSourceIngressRefusal(f"cannot inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OwnerSourceIngressRefusal(f"{label} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise OwnerSourceIngressRefusal(f"cannot read {label} {path}: {error}") from error


def import_owner_source_bytes(
    root: Path,
    content: bytes,
    *,
    reference_id: str,
    declared_digest: str,
    artifact_store: ArtifactStore | None = None,
) -> CASIngress:
    """Import exact owner bytes before validating their declared digest."""
    if not isinstance(content, bytes) or not content:
        raise OwnerSourceIngressRefusal("owner source must contain exact non-empty bytes")
    if not isinstance(reference_id, str) or not reference_id.strip():
        raise OwnerSourceIngressRefusal("reference_id must be non-empty")
    store = artifact_store or ArtifactStore(Path(root))
    artifact = store.write(content)
    try:
        expected = validate_sha256_digest(declared_digest)
    except ValueError as error:
        raise OwnerSourceIngressRefusal(str(error)) from error
    if artifact.digest != expected:
        raise OwnerSourceIngressRefusal(
            f"owner source digest mismatch: declared {expected}, observed {artifact.digest}")
    return CASIngress(reference_id, artifact)


def import_owner_source_file(
    root: Path,
    source: Path,
    *,
    reference_id: str,
    declared_digest: str,
    artifact_store: ArtifactStore | None = None,
) -> CASIngress:
    content = _regular_file_bytes(Path(source), "owner source")
    return import_owner_source_bytes(
        root,
        content,
        reference_id=reference_id,
        declared_digest=declared_digest,
        artifact_store=artifact_store,
    )


def publish_work_brief(
    root: Path,
    content: bytes,
    *,
    declared_digest: str | None = None,
    artifact_store: ArtifactStore | None = None,
    completion_contract: CompletionContract | None = None,
    context_resume: bool = False,
) -> PublishedWorkBrief:
    """Import exact canonical WorkBrief bytes, then validate schema, digest, and lineage."""
    store = artifact_store or ArtifactStore(Path(root))
    artifact = store.write(content)
    if declared_digest is not None:
        try:
            expected = validate_sha256_digest(declared_digest)
        except ValueError as error:
            raise WorkBriefDigestRefusal(str(error)) from error
        if artifact.digest != expected:
            raise WorkBriefDigestRefusal(
                f"declared {expected}, observed {artifact.digest}")
    brief = parse_work_brief_bytes(
        content,
        artifact_store=store,
        completion_contract=completion_contract,
        context_resume=context_resume,
    )
    return PublishedWorkBrief(
        brief,
        f"work-brief:{brief.brief_id}:{brief.revision}",
        artifact,
    )


_PROVENANCE_LABELS = {
    "owner-source": "Owner source",
    "harness-observation": "Harness observation",
    "coordinator-summary": "Coordinator context — interpretation, not owner authority",
}


def _source_label(source: SemanticSource) -> str:
    return f"{source.payload['kind']} {source.digest}"


def _render_item(item: SemanticItem) -> str:
    sources = ", ".join(_source_label(source) for source in item.sources) or "no authority source"
    return f"- **{_PROVENANCE_LABELS[item.provenance]}** — {item.text} _(sources: {sources})_"


def _render_items(items: tuple[SemanticItem, ...], empty: str) -> list[str]:
    return [_render_item(item) for item in items] or [f"- {empty}"]


def render_semantic_prompt(brief: WorkBrief, contract: CompletionContract) -> str:
    """Render the seven semantic sections without run-engine bookkeeping instructions."""
    _validate_contract(brief, contract)
    lines: list[str] = ["## Why this matters", _render_item(brief.objective.why_now), ""]
    lines.extend(["## Current context"])
    lines.extend(_render_items(brief.current_state, "No additional current-state observation."))
    if brief.known_failures:
        lines.append("Known failures:")
        lines.extend(_render_items(brief.known_failures, "None recorded."))
    lines.extend(["", "## Goal and expected outcome delta", brief.objective.desired_delta])
    objective = brief.objective.ref.to_dict()
    objective_digest = (
        objective.get("fact_digest") or objective.get("item_digest") or objective.get("digest"))
    lines.append(f"Objective authority: {objective['kind']} {objective_digest}")

    lines.extend(["", "## Fixed decisions / worker freedom / escalation boundaries"])
    lines.append("Fixed decisions:")
    lines.extend(_render_items(brief.decisions.fixed, "None recorded."))
    lines.append("Worker may choose:")
    lines.extend(_render_items(brief.decisions.worker_may_choose, "No worker choices recorded."))
    lines.append("Escalate before crossing:")
    lines.extend(_render_items(brief.decisions.requires_escalation, "No additional boundary recorded."))
    if brief.constraints:
        lines.append("Constraints:")
        lines.extend(_render_items(brief.constraints, "None recorded."))
    if brief.non_goals:
        lines.append("Non-goals:")
        lines.extend(_render_items(brief.non_goals, "None recorded."))
    if brief.open_questions:
        lines.append("Open questions — do not guess them closed:")
        lines.extend(_render_items(brief.open_questions, "None recorded."))

    lines.extend(["", "## Acceptance or learning criteria"])
    for criterion in contract.criteria:
        source = criterion.source.to_dict()
        source_digest = (
            source.get("fact_digest") or source.get("item_digest") or source.get("digest"))
        lines.append(
            f"- `{criterion.id}` ({criterion.mode.value}): {criterion.text} "
            f"_[{source['kind']} {source_digest}; expect {criterion.evidence.kind}]_"
        )

    lines.extend(["", "## Relevant sources"])
    if brief.references:
        for reference in brief.references:
            lines.append(
                f"- `{reference.path}` — {reference.anchor}: {reference.purpose} "
                f"_({reference.digest})_"
            )
    else:
        lines.append("- No additional code/document reference.")

    lines.extend([
        "",
        "## Report / context request",
        "Return one honest semantic outcome: `completed`, or `context-requested` with the "
        "question, blocked decision, and why that context is required. Stop when requesting context.",
    ])
    return "\n".join(lines) + "\n"


__all__ = [
    "CASIngress",
    "DecisionSpace",
    "EvidenceExpectation",
    "OwnerSourceIngressRefusal",
    "PROVENANCE_KINDS",
    "ProvenanceRefusal",
    "PublishedWorkBrief",
    "RelevantReference",
    "SemanticItem",
    "SemanticSource",
    "WORK_BRIEF_SCHEMA",
    "WorkBrief",
    "WorkBriefContractRefusal",
    "WorkBriefDigestRefusal",
    "WorkBriefError",
    "WorkBriefLineageRefusal",
    "WorkBriefSchemaRefusal",
    "WorkObjective",
    "import_owner_source_bytes",
    "import_owner_source_file",
    "parse_work_brief_bytes",
    "publish_work_brief",
    "render_semantic_prompt",
]
