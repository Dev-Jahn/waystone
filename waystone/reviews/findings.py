"""Immutable review finding claim, validation, and disposition chains.

The module deliberately keeps the three records separate. A reviewer claim never decides truth,
a validation never decides priority, and a disposition never rewrites either predecessor.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from waystone.adapters.git import git_full_sha
from waystone.core import WorkflowError
from waystone.features import review_layout
from waystone.jobs.completion import (
    AuthorityRefRefusal,
    AuthorityResolver,
    CompletionError,
    ProjectFactObjectiveRef,
    parse_authority_ref,
    parse_objective_ref,
)
from waystone.project.brief import ProjectBriefError, read_project_frame_at_commit
from waystone.runs.artifacts import ArtifactError


CLAIM_SCHEMA = "waystone-review-finding-1"
VALIDATION_SCHEMA = "waystone-finding-validation-1"
DISPOSITION_SCHEMA = "waystone-finding-disposition-1"

VALIDITIES = frozenset(("confirmed", "rejected", "unresolved"))
IMPACTS = frozenset(("blocker", "major", "minor"))
EXPOSURES = frozenset(("common", "edge", "adversarial", "unknown"))
RELEVANCES = frozenset(("current-objective", "promotion-bound", "future", "out-of-scope"))
DISPOSITIONS = frozenset((
    "fix-now", "fix-before-promotion", "backlog", "accept-risk", "no-action",
))
REMEDIATION_SCOPES = frozenset(("local", "bounded", "architectural"))
COSTS = frozenset(("low", "medium", "high", "unknown"))
LIFECYCLE_STAGES = frozenset(("explore", "evaluate", "promote"))
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}/[a-z0-9](?:[a-z0-9-]{2,47})$")
_TYPED_AUTHORITY_KINDS = frozenset((
    "project-fact", "owner-request", "milestone", "accepted-adr",
    "evaluation-spec", "evaluation-evidence",
))


class FindingError(WorkflowError):
    """Base class for typed finding workflow refusals."""


class ArtifactValidationError(FindingError):
    code = "invalid-finding-artifact"


class ImmutableArtifactConflict(FindingError):
    code = "finding-artifact-immutable-conflict"


class ChainConflict(FindingError):
    code = "finding-chain-conflict"


class DivergentHeadConflict(ChainConflict):
    code = "divergent-finding-head"


class OwnerDecisionRequired(FindingError):
    code = "finding-owner-decision-required"


class StaleDisposition(FindingError):
    code = "stale-finding-disposition"


class AuthorityValidationRefusal(FindingError):
    code = "finding-authority-validation-refusal"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class ObjectiveSuperseded(FindingError):
    code = "objective-superseded"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class Artifact:
    payload: dict[str, Any]
    bytes: bytes
    digest: str
    path: Path | None = None

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]


def artifact_digest(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise TypeError("finding artifact content must be bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _yaml_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return yaml.safe_dump(
            dict(payload), sort_keys=True, allow_unicode=True,
            default_flow_style=False, width=120,
        ).encode("utf-8")
    except (TypeError, ValueError, yaml.YAMLError) as error:
        raise ArtifactValidationError(f"finding artifact cannot be serialized: {error}") from error


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the deterministic YAML bytes used for all Git-tracked finding records."""
    return _yaml_bytes(payload)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArtifactValidationError(f"{field}: must be a mapping")
    return dict(value)


def _string(value: Any, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ArtifactValidationError(f"{field}: must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _string(value, field)
    if _DIGEST_RE.fullmatch(value) is None:
        raise ArtifactValidationError(f"{field}: must be sha256:<64 lowercase hex digits>")
    return value


def _uuid7(value: Any, field: str) -> str:
    value = _string(value, field)
    try:
        return review_layout.require_uuid7(value)
    except review_layout.ReviewLayoutError as error:
        raise ArtifactValidationError(f"{field}: {error}") from error


def _revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ArtifactValidationError("revision: must be a positive integer")
    return value


def _role(value: Any, field: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    row = _mapping(value, field)
    if row.get("role") not in allowed:
        raise ArtifactValidationError(f"{field}.role: must be one of {allowed}")
    _digest(row.get("binding_digest"), f"{field}.binding_digest")
    if row.get("principal") is not None and not isinstance(row["principal"], str):
        raise ArtifactValidationError(f"{field}.principal: must be a string or null")
    return row


def _target(value: Any) -> dict[str, Any]:
    row = _mapping(value, "target")
    for field in ("run_spec_digest", "result_digest", "review_artifact_digest"):
        _digest(row.get(field), f"target.{field}")
    return row


def _allowed(row: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    unknown = sorted(set(row) - fields)
    if unknown:
        raise ArtifactValidationError(f"{label}: unknown field(s): {', '.join(unknown)}")


def validate_claim(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(payload, "claim")
    _allowed(row, frozenset({
        "schema", "finding_id", "review_run_id", "target", "source_finding_id", "claim",
        "evidence", "reviewer_assessment", "reported_by",
    }), "claim")
    if row.get("schema") != CLAIM_SCHEMA:
        raise ArtifactValidationError(f"schema: expected {CLAIM_SCHEMA}")
    _uuid7(row.get("finding_id"), "finding_id")
    _uuid7(row.get("review_run_id"), "review_run_id")
    _target(row.get("target"))
    if row.get("source_finding_id") is not None:
        _string(row["source_finding_id"], "source_finding_id")
    _string(row.get("claim"), "claim")
    evidence = row.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(item, str) or not item.strip() for item in evidence):
        raise ArtifactValidationError("evidence: must be a list of non-empty strings")
    assessment = _mapping(row.get("reviewer_assessment"), "reviewer_assessment")
    if assessment.get("impact") not in IMPACTS:
        raise ArtifactValidationError(f"reviewer_assessment.impact: must be one of {sorted(IMPACTS)}")
    suggestion = assessment.get("suggested_remediation")
    if suggestion is not None:
        _string(suggestion, "reviewer_assessment.suggested_remediation")
    _role(row.get("reported_by"), "reported_by", ("reviewer",))
    return row


def validate_validation(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(payload, "validation")
    _allowed(row, frozenset({
        "schema", "finding_id", "finding_digest", "revision", "supersedes_digest",
        "validity", "failure_mechanism", "evidence_refs", "validated_by",
    }), "validation")
    if row.get("schema") != VALIDATION_SCHEMA:
        raise ArtifactValidationError(f"schema: expected {VALIDATION_SCHEMA}")
    _uuid7(row.get("finding_id"), "finding_id")
    _digest(row.get("finding_digest"), "finding_digest")
    _revision(row.get("revision"))
    if row.get("supersedes_digest") is not None:
        _digest(row["supersedes_digest"], "supersedes_digest")
    if row.get("validity") not in VALIDITIES:
        raise ArtifactValidationError(f"validity: must be one of {sorted(VALIDITIES)}")
    _string(row.get("failure_mechanism"), "failure_mechanism")
    refs = row.get("evidence_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, Mapping) for ref in refs):
        raise ArtifactValidationError("evidence_refs: must be a list of mappings")
    for i, ref in enumerate(refs):
        kind = _string(ref.get("kind"), f"evidence_refs[{i}].kind")
        if kind in _TYPED_AUTHORITY_KINDS:
            try:
                parse_authority_ref(ref, f"evidence_refs[{i}]")
            except AuthorityRefRefusal as error:
                raise ArtifactValidationError(str(error)) from error
        else:
            _allowed(ref, frozenset(("kind", "digest")), f"evidence_refs[{i}]")
            _digest(ref.get("digest"), f"evidence_refs[{i}].digest")
    _role(row.get("validated_by"), "validated_by", ("coordinator",))
    return row


def validate_disposition(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(payload, "disposition")
    _allowed(row, frozenset({
        "schema", "finding_id", "finding_digest", "confirmed_validation_digest", "revision",
        "supersedes_digest", "objective_ref", "lifecycle_stage", "applies_to", "impact",
        "exposure", "relevance", "disposition", "remediation_scope", "estimated_cost",
        "rationale", "clearance", "decided_by", "materialized_task_id", "risk",
    }), "disposition")
    if row.get("schema") != DISPOSITION_SCHEMA:
        raise ArtifactValidationError(f"schema: expected {DISPOSITION_SCHEMA}")
    _uuid7(row.get("finding_id"), "finding_id")
    _digest(row.get("finding_digest"), "finding_digest")
    _digest(row.get("confirmed_validation_digest"), "confirmed_validation_digest")
    _revision(row.get("revision"))
    if row.get("supersedes_digest") is not None:
        _digest(row["supersedes_digest"], "supersedes_digest")

    objective = _mapping(row.get("objective_ref"), "objective_ref")
    try:
        objective_ref = parse_objective_ref(objective, "objective_ref")
    except AuthorityRefRefusal as error:
        raise ArtifactValidationError(str(error)) from error
    if not isinstance(objective_ref, ProjectFactObjectiveRef):
        raise ArtifactValidationError("objective_ref.kind: must be project-fact")
    stage = _string(row.get("lifecycle_stage"), "lifecycle_stage")
    if stage not in LIFECYCLE_STAGES:
        raise ArtifactValidationError(f"lifecycle_stage: must be one of {sorted(LIFECYCLE_STAGES)}")
    applies = _mapping(row.get("applies_to"), "applies_to")
    _uuid7(applies.get("promotion_lineage_id"), "applies_to.promotion_lineage_id")
    _digest(applies.get("candidate_digest"), "applies_to.candidate_digest")
    _digest(applies.get("result_digest"), "applies_to.result_digest")
    if row.get("impact") not in IMPACTS:
        raise ArtifactValidationError(f"impact: must be one of {sorted(IMPACTS)}")
    if row.get("exposure") not in EXPOSURES:
        raise ArtifactValidationError(f"exposure: must be one of {sorted(EXPOSURES)}")
    if row.get("relevance") not in RELEVANCES:
        raise ArtifactValidationError(f"relevance: must be one of {sorted(RELEVANCES)}")
    disposition = row.get("disposition")
    if disposition not in DISPOSITIONS:
        raise ArtifactValidationError(f"disposition: must be one of {sorted(DISPOSITIONS)}")
    if row.get("remediation_scope") not in REMEDIATION_SCOPES:
        raise ArtifactValidationError(
            f"remediation_scope: must be one of {sorted(REMEDIATION_SCOPES)}")
    if row.get("estimated_cost") not in COSTS:
        raise ArtifactValidationError(f"estimated_cost: must be one of {sorted(COSTS)}")
    _string(row.get("rationale"), "rationale")
    clearance = row.get("clearance")
    if clearance is not None:
        if disposition != "fix-before-promotion":
            raise ArtifactValidationError("clearance: only valid for fix-before-promotion")
        clearance = _mapping(clearance, "clearance")
        for field in ("candidate_digest", "supersedes_candidate_digest", "verification_evidence_digest"):
            _digest(clearance.get(field), f"clearance.{field}")
    decided = _role(row.get("decided_by"), "decided_by", ("coordinator", "owner"))
    task_id = row.get("materialized_task_id")
    if task_id is not None and (not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None):
        raise ArtifactValidationError("materialized_task_id: must be a valid task id or null")
    if disposition not in ("fix-now", "fix-before-promotion") and task_id is not None:
        raise ArtifactValidationError(
            "materialized_task_id: must be null for backlog, accept-risk, or no-action")

    risk = row.get("risk")
    if risk is not None:
        _string(risk, "risk")
    owner_only = (
        disposition == "accept-risk"
        and (row["impact"] == "blocker"
             or row["relevance"] in ("current-objective", "promotion-bound")
             or risk in ("trust", "public-contract", "trust/public-contract"))
    )
    if owner_only and decided["role"] != "owner":
        raise OwnerDecisionRequired(
            "accept-risk for blocker/current-objective/promotion-bound/trust-public-contract "
            "risk requires decided_by.role: owner")
    if disposition == "fix-now" and row["remediation_scope"] == "architectural" \
            and decided["role"] != "owner":
        raise OwnerDecisionRequired("architectural fix-now requires decided_by.role: owner")
    return row


def parse_artifact(source: bytes | bytearray | str | Path, schema: str) -> dict[str, Any]:
    if isinstance(source, (str, Path)):
        try:
            content = Path(source).read_bytes()
        except OSError as error:
            raise FindingError(f"finding artifact unavailable {source}: {error}") from error
    elif isinstance(source, (bytes, bytearray)):
        content = bytes(source)
    else:
        raise TypeError("finding artifact source must be bytes or a path")
    try:
        payload = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ArtifactValidationError(f"finding artifact YAML is invalid: {error}") from error
    validators = {
        CLAIM_SCHEMA: validate_claim,
        VALIDATION_SCHEMA: validate_validation,
        DISPOSITION_SCHEMA: validate_disposition,
    }
    validator = validators.get(schema)
    if validator is None:
        raise ArtifactValidationError(f"unknown finding schema: {schema}")
    row = validator(payload)
    return row


def parse_claim(source: bytes | bytearray | str | Path) -> dict[str, Any]:
    return parse_artifact(source, CLAIM_SCHEMA)


def parse_validation(source: bytes | bytearray | str | Path) -> dict[str, Any]:
    return parse_artifact(source, VALIDATION_SCHEMA)


def parse_disposition(source: bytes | bytearray | str | Path) -> dict[str, Any]:
    return parse_artifact(source, DISPOSITION_SCHEMA)


def _artifact(payload: Mapping[str, Any], path: Path | None = None) -> Artifact:
    content = canonical_bytes(payload)
    return Artifact(dict(payload), content, artifact_digest(content), path)


def _publish(
        reviews_dir: Path, run_id: str, finding_id: str, kind: str, revision: int | None,
        payload: Mapping[str, Any], validator,
) -> Artifact:
    row = validator(payload)
    artifact = _artifact(row)
    try:
        path = review_layout.publish_finding_yaml(
            reviews_dir, run_id, finding_id, kind, revision, artifact.bytes)
    except review_layout.ArtifactConflict as error:
        raise ImmutableArtifactConflict(str(error)) from error
    return Artifact(artifact.payload, artifact.bytes, artifact.digest, path)


def write_claim(reviews_dir: Path, payload: Mapping[str, Any]) -> Artifact:
    row = validate_claim(payload)
    return _publish(
        Path(reviews_dir), row["review_run_id"], row["finding_id"],
        review_layout.FINDING_CLAIM, None, row, validate_claim)


def read_claim(reviews_dir: Path, run_id: str, finding_id: str) -> Artifact:
    raw = review_layout.read_finding_artifact(
        Path(reviews_dir), run_id, finding_id, review_layout.FINDING_CLAIM, None)
    row = parse_artifact(raw["bytes"], CLAIM_SCHEMA)
    if row["review_run_id"] != run_id or row["finding_id"] != finding_id:
        raise ChainConflict("claim payload identity does not match its canonical path")
    return _artifact(row, raw["path"])


def _chain(
        reviews_dir: Path, run_id: str, finding_id: str, kind: str, schema: str,
) -> tuple[Artifact, ...]:
    parent = review_layout.canonical_finding_directory(Path(reviews_dir), run_id, finding_id) / (
        "validations" if kind == review_layout.FINDING_VALIDATION else "dispositions")
    if parent.is_symlink():
        raise ChainConflict(f"finding chain directory is a symlink: {parent}")
    if not parent.is_dir():
        return ()
    records: list[Artifact] = []
    for path in sorted(parent.glob("[0-9][0-9][0-9][0-9].yaml")):
        if path.is_symlink():
            raise ChainConflict(f"finding chain artifact is a symlink: {path}")
        try:
            revision = int(path.stem)
        except ValueError:
            continue
        raw = review_layout.read_finding_artifact(
            Path(reviews_dir), run_id, finding_id, kind, revision)
        row = parse_artifact(raw["bytes"], schema)
        if row["finding_id"] != finding_id:
            raise ChainConflict(f"finding chain payload identity does not match {path}")
        records.append(_artifact(row, path))
    return tuple(records)


def _head(records: tuple[Artifact, ...], label: str) -> Artifact | None:
    if not records:
        return None
    by_digest = {record.digest: record for record in records}
    superseded: set[str] = set()
    for record in records:
        row = record.payload
        if row["revision"] == 1:
            if row.get("supersedes_digest") is not None:
                raise ChainConflict(f"{label} revision 1 must not supersede another record")
        elif row.get("supersedes_digest") is None:
            raise ChainConflict(f"{label} revision > 1 must supersede a prior digest")
        if row.get("supersedes_digest") is not None:
            parent = by_digest.get(row["supersedes_digest"])
            if parent is None or parent.payload["revision"] + 1 != row["revision"]:
                raise ChainConflict(f"{label} supersedes_digest does not name an earlier revision")
            superseded.add(row["supersedes_digest"])
    heads = [record for record in records if record.digest not in superseded]
    if len(heads) != 1:
        raise DivergentHeadConflict(
            f"{label} has {len(heads)} chain heads; append a revision that names one head")
    return heads[0]


def validation_records(reviews_dir: Path, run_id: str, finding_id: str) -> tuple[Artifact, ...]:
    return _chain(Path(reviews_dir), run_id, finding_id,
                  review_layout.FINDING_VALIDATION, VALIDATION_SCHEMA)


def validation_head(reviews_dir: Path, run_id: str, finding_id: str) -> Artifact | None:
    return _head(validation_records(reviews_dir, run_id, finding_id), "validation")


def disposition_records(reviews_dir: Path, run_id: str, finding_id: str) -> tuple[Artifact, ...]:
    return _chain(Path(reviews_dir), run_id, finding_id,
                  review_layout.FINDING_DISPOSITION, DISPOSITION_SCHEMA)


def disposition_head(reviews_dir: Path, run_id: str, finding_id: str) -> Artifact | None:
    return _head(disposition_records(reviews_dir, run_id, finding_id), "disposition")


def _append(
        reviews_dir: Path, run_id: str, finding_id: str, kind: str, schema: str,
        payload: Mapping[str, Any], validator,
) -> Artifact:
    records = _chain(Path(reviews_dir), run_id, finding_id, kind, schema)
    head = _head(records, "validation" if kind == review_layout.FINDING_VALIDATION else "disposition")
    row = dict(payload)
    expected_revision = (head.payload["revision"] + 1) if head else 1
    if row.get("revision") is None:
        row["revision"] = expected_revision
    if row["revision"] != expected_revision:
        raise ChainConflict(f"revision must be the next append-only revision ({expected_revision})")
    expected_parent = head.digest if head else None
    if row.get("supersedes_digest") != expected_parent:
        raise ChainConflict("supersedes_digest must name the current chain head")
    return _publish(Path(reviews_dir), run_id, finding_id, kind, row["revision"], row, validator)


def validate_validation_authority(root: Path, payload: Mapping[str, Any]) -> None:
    """Require every validation evidence ref to resolve to rehashed Git or CAS bytes."""
    row = validate_validation(payload)
    resolver = AuthorityResolver(Path(root))
    for index, raw_ref in enumerate(row["evidence_refs"]):
        ref = dict(raw_ref)
        try:
            if ref["kind"] in _TYPED_AUTHORITY_KINDS:
                resolver.validate(parse_authority_ref(ref, f"evidence_refs[{index}]"))
            else:
                resolver.artifact_store.read(ref["digest"])
        except (ArtifactError, CompletionError, ProjectBriefError, KeyError, TypeError, ValueError) as error:
            raise AuthorityValidationRefusal(
                f"evidence_refs[{index}] does not resolve to authoritative bytes: {error}"
            ) from error


def validate_disposition_authority(root: Path, payload: Mapping[str, Any]) -> None:
    """Require the disposition objective to match exact committed project authority."""
    row = validate_disposition(payload)
    root = Path(root)
    try:
        objective = parse_objective_ref(row["objective_ref"], "objective_ref")
        AuthorityResolver(root).validate(objective)
    except (CompletionError, ProjectBriefError, KeyError, TypeError, ValueError) as error:
        raise AuthorityValidationRefusal(
            f"objective_ref does not resolve to authoritative bytes: {error}"
        ) from error
    current_head = git_full_sha(root)
    if current_head is None:
        raise AuthorityValidationRefusal("current HEAD cannot be resolved")
    try:
        read_project_frame_at_commit(
            root, objective.commit, current_commit=current_head)
        current_frame = read_project_frame_at_commit(root, current_head)
        current_fact = current_frame.fact(objective.fact_id)
    except ProjectBriefError as error:
        raise ObjectiveSuperseded(
            f"objective_ref is not current project authority: {error}"
        ) from error
    if (current_frame.path != objective.path
            or current_fact.digest != objective.fact_digest
            or current_fact.binding != objective.binding):
        raise ObjectiveSuperseded(
            f"objective_ref {objective.fact_id} digest/binding differs from current HEAD")


def append_validation(
        reviews_dir: Path, run_id: str, finding_id: str, payload: Mapping[str, Any], *,
        root: Path,
) -> Artifact:
    claim = read_claim(Path(reviews_dir), run_id, finding_id)
    row = dict(payload)
    if row.get("finding_digest") != claim.digest:
        raise ChainConflict("validation.finding_digest must equal the immutable claim digest")
    validate_validation_authority(Path(root), row)
    return _append(
        Path(reviews_dir), run_id, finding_id, review_layout.FINDING_VALIDATION,
        VALIDATION_SCHEMA, row, validate_validation)


def append_disposition(
        reviews_dir: Path, run_id: str, finding_id: str, payload: Mapping[str, Any], *,
        root: Path,
) -> Artifact:
    claim = read_claim(Path(reviews_dir), run_id, finding_id)
    validation = validation_head(Path(reviews_dir), run_id, finding_id)
    if validation is None or validation.payload["validity"] != "confirmed":
        raise FindingError("disposition requires a confirmed validation head")
    row = dict(payload)
    if row.get("finding_digest") != claim.digest:
        raise ChainConflict("disposition.finding_digest must equal the immutable claim digest")
    if row.get("confirmed_validation_digest") != validation.digest:
        raise StaleDisposition(
            "disposition.confirmed_validation_digest must equal the current confirmed validation head")
    validate_disposition_authority(Path(root), row)
    return _append(
        Path(reviews_dir), run_id, finding_id, review_layout.FINDING_DISPOSITION,
        DISPOSITION_SCHEMA, row, validate_disposition)


def load_finding(reviews_dir: Path, run_id: str, finding_id: str) -> dict[str, Artifact | None]:
    """Return the three current projections without treating directory order as authority."""
    return {
        "claim": read_claim(Path(reviews_dir), run_id, finding_id),
        "validation": validation_head(Path(reviews_dir), run_id, finding_id),
        "disposition": disposition_head(Path(reviews_dir), run_id, finding_id),
    }


record_claim = write_claim
record_validation = append_validation
record_disposition = append_disposition
