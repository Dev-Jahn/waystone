#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Deterministic advisory evidence projections for ideate realignment."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import WorkflowError, find_project_root, project_state_path, write_text_atomic  # noqa: E402
from waystone.project.context import resolve_project_context  # noqa: E402
from waystone.runs.observe import project_status_json, project_status_projection  # noqa: E402


def _root(value: str | None) -> Path:
    root = Path(value).resolve() if value else find_project_root(Path.cwd())
    if root is None:
        raise WorkflowError("no initialized project")
    return root


def _out(root: Path, user_wide: bool = False) -> Path:
    if user_wide:
        return Path.home() / ".waystone" / "improve"
    path = project_state_path(root) / "improve"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: object) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def trace(root: Path, out: Path) -> int:
    _write_json(out / "facts.json", {"schema": "waystone-improve-facts-2", "source": "status-read-model"})
    print(f"improve trace: advisory input initialized at {out}")
    return 0


def reviews(root: Path, out: Path) -> int:
    claims_root = root / "docs" / "reviews" / "runs"
    rows = [str(path.relative_to(root)) for path in sorted(claims_root.glob("*/findings/*/claim.yaml"))]
    _write_json(out / "reviews.json", {"schema": "waystone-improve-reviews-2", "claims": rows})
    return 0


def evidence(root: Path, out: Path) -> int:
    context = resolve_project_context(root)
    projection = project_status_projection(context.canonical_root)
    _write_json(out / "evidence.json", project_status_json(projection))
    return 0


def audit(root: Path, out: Path) -> int:
    evidence_path = out / "evidence.json"
    facts = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else {}
    _write_json(out / "facts.json", {
        "schema": "waystone-improve-facts-2",
        "maturity": {"stage": "advisory", "recommendation_strength": "soft"},
        "status": facts,
        "coverage_caveats": [],
    })
    return 0


def metrics(root: Path, out: Path) -> int:
    facts = json.loads((out / "facts.json").read_text(encoding="utf-8")) if (out / "facts.json").is_file() else {}
    row = {"at": datetime.now(timezone.utc).isoformat(), "schema": "waystone-improve-metrics-2",
           "source": "facts.json", "facts": facts}
    with (out / "metrics.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def decide(root: Path, out: Path, args: list[str]) -> int:
    if len(args) < 2 or args[1] not in {"accept", "reject"}:
        print("waystone improve decide: expected <rec-id> accept|reject", file=sys.stderr)
        return 1
    row = {"rec_id": args[0], "decision": args[1],
           "at": datetime.now(timezone.utc).isoformat()}
    with (out / "decisions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"recorded advisory decision: {args[0]}={args[1]}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("waystone improve: expected trace|reviews|evidence|audit|metrics|decide", file=sys.stderr)
        return 1
    user_wide = "--user-wide" in argv
    positional = [item for item in argv[1:] if not item.startswith("-")]
    try:
        root = _root(None)
        out = _out(root, user_wide)
        command = argv[0]
        if command == "trace": return trace(root, out)
        if command == "reviews": return reviews(root, out)
        if command == "evidence": return evidence(root, out)
        if command == "audit": return audit(root, out)
        if command == "metrics": return metrics(root, out)
        if command == "decide": return decide(root, out, positional)
    except (OSError, ValueError, WorkflowError) as error:
        print(f"waystone improve: {error}", file=sys.stderr)
        return 1
    print(f"waystone improve: unknown command {argv[0]!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
