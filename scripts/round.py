#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Atomic round closeout — the deterministic ritual the round skill used to hand-run.

`close` performs, in one command and in order:
  1. flip the given tasks to done and stamp every worked task with the round id
     (surgical, comment-preserving edits to tasks.yaml — never a full rewrite),
  2. validate the registry and regenerate ROADMAP.md (and SSOT views if configured),
  3. set state.last_round_commit to the round's tip.

The text-surgery helpers (set_task_field, set_config_scalar) are pure and tested.

Usage (also `jw round close`):
  round.py close [root] --round <id> [--done id,id] [--touched id,id] [--commit HEAD]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ROUND_RE, WorkflowError, find_project_root, git_full_sha, load_config,
)


# ---- structure-bounded text surgery ------------------------------------------
# Edits are scoped by the YAML AST (yaml.compose) — a `- id:` under `metadata:` or a nested
# `state:` must NOT be mistaken for the real `tasks:`/top-level `state:`. We find the exact node
# LINE RANGE from the AST, then do comment-preserving text surgery only inside that range.
def _compose_mapping(text: str) -> "yaml.MappingNode":
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        raise WorkflowError("document top level must be a mapping")
    return node


def _top_level(root: "yaml.MappingNode", key: str):
    """Value node of the UNIQUE top-level mapping key (None if absent; WorkflowError on duplicate)."""
    found = [v for k, v in root.value if isinstance(k, yaml.ScalarNode) and k.value == key]
    if len(found) > 1:
        raise WorkflowError(f"ambiguous document: {len(found)} top-level {key!r} keys")
    return found[0] if found else None


def _task_item_span(text: str, task_id: str) -> tuple[int, int]:
    """(start_line, end_line) of the task whose DIRECT `id` == task_id, located only inside the
    top-level `tasks:` sequence. Raises KeyError if absent, WorkflowError if ambiguous (duplicate
    `tasks`/id/`id`-key)."""
    tasks = _top_level(_compose_mapping(text), "tasks")
    if tasks is None:
        raise KeyError("no top-level 'tasks' sequence")
    if not isinstance(tasks, yaml.SequenceNode):
        raise WorkflowError("'tasks' is not a list")
    matches = []
    for item in tasks.value:
        if not isinstance(item, yaml.MappingNode):
            continue
        ids = [v.value for k, v in item.value
               if isinstance(k, yaml.ScalarNode) and k.value == "id" and isinstance(v, yaml.ScalarNode)]
        if len(ids) > 1:
            raise WorkflowError(f"task near line {item.start_mark.line + 1} has multiple id keys")
        if ids and ids[0] == task_id:
            matches.append((item.start_mark.line, item.end_mark.line))
    if len(matches) > 1:
        raise WorkflowError(f"ambiguous registry: duplicate task id {task_id!r}")
    if not matches:
        raise KeyError(f"task id not found in registry: {task_id}")
    start, end = matches[0]
    # PyYAML reports end_mark.line one short for the FINAL node of a document with no trailing
    # newline (it lands ON the task's last content line instead of past it). Left uncorrected, the
    # consumers' `range(start+1, end)` / `del lines[s:e]` would skip that last line — silently
    # dropping a field update or orphaning a field onto the previous task. Extend the span to EOF.
    lines = text.splitlines(keepends=True)
    if lines and end == len(lines) - 1 and not lines[-1].endswith("\n"):
        end = len(lines)
    return (start, end)


def set_task_field(text: str, task_id: str, field: str, value: str) -> str:
    """Set `field: value` inside the task block (located via the AST, never a decoy elsewhere),
    preserving all other content/comments. Updates the field if present, else inserts it right
    after the id line. Raises if the task is absent or the structure is ambiguous."""
    lines = text.splitlines(keepends=True)
    start, end = _task_item_span(text, task_id)
    end = min(end, len(lines))
    nl = "\n" if not lines[start].endswith("\r\n") else "\r\n"
    field_indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    # match ONLY a task-level field at the exact sibling indent — never a deeper nested key.
    field_re = re.compile(rf"^ {{{field_indent}}}{re.escape(field)}:\s*.*$")
    for k in range(start + 1, end):
        if field_re.match(lines[k]):
            # the value may span multiple lines (a block list/mapping): consume the contiguous run
            # of more-indented continuation lines, so replacing with a flow value doesn't orphan
            # them. An inline value has no such run, so only line k is replaced (unchanged path).
            j = k + 1
            while j < end and lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) > field_indent:
                j += 1
            lines[k:j] = [f"{' ' * field_indent}{field}: {value}{nl}"]
            return "".join(lines)
    lines.insert(start + 1, f"{' ' * field_indent}{field}: {value}{nl}")
    return "".join(lines)


def set_config_scalar(text: str, key: str, value: str, section: str | None = None) -> str:
    """Replace the value of a `<key>:` line preserving indent/comments. When `section` is given
    (e.g. 'state'), only a key INSIDE that block is matched — so `last_round_commit` can't be
    confused with a same-named key elsewhere. Raises if absent."""
    lines = text.splitlines(keepends=True)
    key_re = re.compile(r"^(\s*)" + re.escape(key) + r":\s*.*$")

    def replace_at(i: int, indent: str) -> str:
        nl = "\r\n" if lines[i].endswith("\r\n") else "\n"
        lines[i] = f"{indent}{key}: {value}{nl}"
        return "".join(lines)

    if section is None:
        for i, ln in enumerate(lines):
            m = key_re.match(ln)
            if m:
                return replace_at(i, m.group(1))
        raise KeyError(f"config key not found: {key}")

    # AST-bounded: the UNIQUE top-level `section` mapping, then its DIRECT child `key` only — a
    # nested `foo.state.<key>` or a duplicate top-level `state` can't be edited by mistake.
    sec = _top_level(_compose_mapping(text), section)
    if sec is None:
        raise KeyError(f"config section not found: {section}")
    if not isinstance(sec, yaml.MappingNode):
        raise WorkflowError(f"config section {section!r} is not a mapping")
    child_lines = [k.start_mark.line for k, v in sec.value
                   if isinstance(k, yaml.ScalarNode) and k.value == key]
    if len(child_lines) > 1:
        raise WorkflowError(f"ambiguous config: {len(child_lines)} {key!r} keys under {section!r}")
    if not child_lines:
        raise KeyError(f"config key {key!r} not found under section {section!r}")
    i = child_lines[0]
    indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
    return replace_at(i, indent)


# ---- orchestration -----------------------------------------------------------
def _parse_ids(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def close(root: Path, round_id: str, done: list[str], touched: list[str], commit: str) -> int:
    """Fail-closed: resolve the commit and confirm the watermark slot up front, apply edits in
    memory and validate BEFORE writing anything, then write tasks.yaml → views → watermark."""
    import shutil
    import tempfile
    import yaml
    import roadmap
    import validate

    if not ROUND_RE.match(round_id):
        print(f"round close: --round must match YYYY-MM-DD-<slug>, got {round_id!r}", file=sys.stderr)
        return 1
    cfg = load_config(root)
    cfg_path = root / ".jahns-workflow.yml"
    tasks_path = root / "tasks.yaml"

    # --- preflight (no writes) ---
    full = git_full_sha(root, commit)
    if full is None:
        print(f"round close: --commit {commit!r} does not resolve to a commit", file=sys.stderr)
        return 1
    ctext = cfg_path.read_text(encoding="utf-8")
    try:
        ctext_new = set_config_scalar(ctext, "last_round_commit", full, section="state")
    except KeyError:
        print("round close: state.last_round_commit is missing from .jahns-workflow.yml — "
              "add it (under `state:`) before closing rounds.", file=sys.stderr)
        return 1
    except WorkflowError as e:
        print(f"round close: cannot safely edit .jahns-workflow.yml — {e}", file=sys.stderr)
        return 1

    orig_tasks_text = tasks_path.read_text(encoding="utf-8")
    text = orig_tasks_text
    data0 = yaml.safe_load(text) or {}
    by_id = {t.get("id"): t for t in data0.get("tasks", []) if isinstance(t, dict)}
    # done tasks must have all deps done — evaluated against the FINAL state (a dependency closed
    # in the SAME round counts), so closing a dependency and its dependent together is allowed.
    final_done = {tid for tid, t in by_id.items() if t.get("status") == "done"} | set(done)
    dep_problems = []
    for tid in done:
        for dep in (by_id.get(tid, {}).get("deps") or []):
            if dep not in final_done:
                dep_problems.append(f"{tid} cannot be done — dependency {dep} is not done "
                                    f"(and is not being closed in this round)")
    if dep_problems:
        for p in dep_problems:
            print(f"round close: {p}", file=sys.stderr)
        return 1

    try:
        for tid in done:
            text = set_task_field(text, tid, "status", "done")
        for tid in dict.fromkeys(done + touched):
            text = set_task_field(text, tid, "round", round_id)
    except (KeyError, WorkflowError) as e:
        print(f"round close: {e}", file=sys.stderr)
        return 1

    errs = validate.validate(yaml.safe_load(text))
    if errs:
        print(f"round close: edits would make tasks.yaml invalid ({len(errs)} issue(s)) — aborted, "
              f"nothing written:", file=sys.stderr)
        for e in errs[:10]:
            print(f"  - {e}", file=sys.stderr)
        return 2

    # --- commit phase (all preflight checks passed): write with rollback ---
    # ROADMAP.render reads tasks.yaml from disk, so the new registry must be written first. If any
    # later step raises, restore the primary mutated files (tasks.yaml, cfg, ROADMAP) AND the whole
    # generated SSOT dir from a snapshot — split/index/.hash/DIGEST must stay mutually consistent,
    # else `ssot.check()` (which only diffs .hash) would report "up to date" over a stale digest.
    roadmap_path = root / "ROADMAP.md"
    orig_roadmap = roadmap_path.read_text(encoding="utf-8") if roadmap_path.exists() else None
    gen_dir = (root / cfg["generated_dir"]) if cfg.get("ssot") else None
    gen_existed = bool(gen_dir and gen_dir.exists())
    gen_backup = None
    if gen_existed:
        gen_backup = Path(tempfile.mkdtemp(prefix="jw-ssot-bak-")) / "g"
        shutil.copytree(gen_dir, gen_backup)

    try:
        tasks_path.write_text(text, encoding="utf-8")
        cfg_path.write_text(ctext_new, encoding="utf-8")
        roadmap_path.write_text(roadmap.render(root), encoding="utf-8")
        if cfg.get("ssot"):
            import ssot
            ssot.regenerate(root)  # one full regen; raises WorkflowError (caught below) not sys.exit
    except Exception as e:  # noqa: BLE001 — any failure must roll every written artifact back
        tasks_path.write_text(orig_tasks_text, encoding="utf-8")
        cfg_path.write_text(ctext, encoding="utf-8")
        if orig_roadmap is None:
            roadmap_path.unlink(missing_ok=True)
        else:
            roadmap_path.write_text(orig_roadmap, encoding="utf-8")
        if gen_dir is not None:
            shutil.rmtree(gen_dir, ignore_errors=True)
            if gen_existed:
                shutil.copytree(gen_backup, gen_dir)
        if gen_backup is not None:
            shutil.rmtree(gen_backup.parent, ignore_errors=True)
        print(f"round close: closeout failed mid-write and was rolled back — {e}", file=sys.stderr)
        return 1
    if gen_backup is not None:
        shutil.rmtree(gen_backup.parent, ignore_errors=True)

    # report the watermark move so the review request can name the diff base (prev tip → this tip).
    # `prev_wm` is the watermark BEFORE this close advanced it — the previous round's tip; resolve it
    # to a full sha (it may be stored short) so it can be copied verbatim as the review base.
    prev_wm = git_full_sha(root, str(prev_raw)) if (prev_raw := (cfg.get("state") or {}).get("last_round_commit")) else None
    print(f"round {round_id} closed: {len(done)} done, {len(set(done + touched))} stamped; "
          f"watermark {(prev_wm[:12] if prev_wm else '(root)')} → {full[:12]}")
    print(f"  review diff base = {prev_wm or '(root)'}  (previous round tip; head = {full})")

    # M2 §9/§6: record the round exposure and evaluate overlay warns at the round-close boundary.
    # Both are best-effort — an exposure/warn failure must NOT roll back an already-completed close
    # (S11/S5). The exposure guard names its failure so it stays visible.
    try:
        import overlay
        overlay.write_round_exposure(root, round_id, git_full_sha(root, "HEAD"), full)
    except Exception as e:  # noqa: BLE001
        print(f"round close: round exposure not recorded ({e}) — close still succeeded",
              file=sys.stderr)
    try:
        import overlay
        overlay.evaluate_boundary(
            root, "round-close", {"round_id": round_id, "closing_task_ids": done})
    except Exception as e:  # noqa: BLE001
        print(f"round close: overlay warning unavailable ({e}) — close still succeeded",
              file=sys.stderr)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "close":
        print(__doc__, file=sys.stderr)
        return 1
    rest = argv[1:]

    def opt(name):
        return rest[rest.index(name) + 1] if name in rest and rest.index(name) < len(rest) - 1 else None
    positional = [a for a in rest if not a.startswith("--") and (rest.index(a) == 0 or rest[rest.index(a) - 1] not in ("--round", "--done", "--touched", "--commit"))]
    root = Path(positional[0]).resolve() if positional else find_project_root(Path.cwd())
    if root is None:
        print("round: no initialized project", file=sys.stderr)
        return 1
    round_id = opt("--round")
    if not round_id:
        print("round close: --round <id> is required", file=sys.stderr)
        return 1
    return close(root, round_id, _parse_ids(opt("--done")), _parse_ids(opt("--touched")), opt("--commit") or "HEAD")


if __name__ == "__main__":
    sys.exit(main())
