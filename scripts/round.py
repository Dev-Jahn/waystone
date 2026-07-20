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

`reclose` records only a new immutable exposure at the current committed HEAD. It is the PR-mode
self-reference break: the request is rendered after the closeout commit without mutating that HEAD.

Usage (also `waystone round close`):
  round.py close [root] --round <id> [--done id,id] [--touched id,id] [--commit HEAD]
  round.py reclose [root] --round <id>
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ROUND_RE, WorkflowError, canonical_scope_prefixes, find_project_root, git_full_sha, git_rc,
    hold_project_lock, is_ancestor, load_config, migrate_project_state, write_text_atomic,
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


def _parse_route_notes(values: list[str]) -> list[dict]:
    """Parse repeatable ``role,execution,backend`` host-guided route observations."""
    import delegate

    routes = []
    for value in values:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 3 or any(not part for part in parts):
            raise WorkflowError(
                "--route-note must be role,execution,backend (backend is <runner>:<model>)")
        role, execution, backend = parts
        if role not in delegate.PROFILE_ROLES:
            raise WorkflowError(
                f"--route-note role must be one of {', '.join(delegate.PROFILE_ROLES)}")
        if execution not in delegate.HOST_GUIDED_EXECUTIONS:
            raise WorkflowError(
                "--route-note records host-guided execution only; external-runner is already "
                "recorded by delegate exposure")
        delegate._validate_profile_binding(
            role, {"execution": execution, "backend": backend})
        routes.append({
            "role": role, "execution": execution, "backend": backend,
            "provenance": "main-session",
        })
    unique = {(row["role"], row["execution"], row["backend"]): row for row in routes}
    return [unique[key] for key in sorted(unique)]


def _validate_recorded_routes(root: Path, routes: list[dict]) -> None:
    if not routes:
        return
    import delegate

    profile, _fingerprint = delegate._load_profile(root)
    bindings = profile.get("bindings") or {}
    for route in routes:
        binding = bindings.get(route["role"])
        if not isinstance(binding, dict):
            raise WorkflowError(
                f"--route-note role {route['role']!r} has no profile binding")
        execution = delegate._validate_profile_binding(route["role"], binding)
        if (execution, binding.get("backend")) != (route["execution"], route["backend"]):
            raise WorkflowError(
                f"--route-note {route['role']} route does not match the current profile binding")


def _current_date() -> date:
    """Local calendar clock seam; tests pin it so close contracts cannot race midnight."""
    return date.today()


def _round_initial_closeout(root: Path, round_id: str, review) -> dict | None:
    """Return a previously minted round's validated generation-1 exposure, if present."""
    initial = review.read_initial_round_closeout_exposure(root, round_id, missing_ok=True)
    return initial[1] if initial is not None else None


def close(root: Path, round_id: str, done: list[str], touched: list[str], commit: str,
          routes: list[dict] | None = None) -> int:
    """Fail-closed: resolve the commit and confirm the watermark slot up front, apply edits in
    memory and validate BEFORE writing anything, then write tasks.yaml → views → watermark."""
    import shutil
    import tempfile
    import yaml
    import roadmap
    import review
    import validate

    if not ROUND_RE.match(round_id):
        print(f"round close: --round must match YYYY-MM-DD-<slug>, got {round_id!r}", file=sys.stderr)
        return 1
    try:
        round_date = date.fromisoformat(round_id[:10])
    except ValueError:
        print(f"round close: --round has no real calendar date, got {round_id!r}",
              file=sys.stderr)
        return 1
    cfg = load_config(root)
    current_date = _current_date()
    try:
        initial_closeout = _round_initial_closeout(root, round_id, review)
    except WorkflowError as e:
        print(f"round close: cannot read existing round exposure — {e}", file=sys.stderr)
        return 1
    if round_date != current_date and initial_closeout is None:
        print(
            f"round close: --round date must be today ({current_date.isoformat()}), "
            f"got {round_date.isoformat()}", file=sys.stderr)
        return 1
    routes = list(routes or [])
    try:
        _validate_recorded_routes(root, routes)
    except WorkflowError as e:
        print(f"round close: cannot record host-guided route — {e}", file=sys.stderr)
        return 1
    try:
        review_reviewers = review.resolve_reviewers(root, cfg["review"]["reviewers"])
    except WorkflowError as e:
        print(f"round close: cannot resolve review request reviewers — {e}", file=sys.stderr)
        return 1
    cfg_path = root / ".waystone.yml"
    tasks_path = root / "tasks.yaml"

    # --- preflight (no writes) ---
    full = git_full_sha(root, commit)
    if full is None:
        print(f"round close: --commit {commit!r} does not resolve to a commit", file=sys.stderr)
        return 1
    head_sha = full if commit == "HEAD" else git_full_sha(root, "HEAD")
    ctext = cfg_path.read_text(encoding="utf-8")
    prev_raw = (cfg.get("state") or {}).get("last_round_commit")
    prev_wm = git_full_sha(root, str(prev_raw)) if prev_raw else None
    review_base = (initial_closeout.get("base_sha")
                   if initial_closeout is not None else prev_wm)
    created_event_paths: list[Path] = []
    try:
        ctext_new = set_config_scalar(ctext, "last_round_commit", full, section="state")
    except KeyError:
        print("round close: state.last_round_commit is missing from .waystone.yml — "
              "add it (under `state:`) before closing rounds.", file=sys.stderr)
        return 1
    except WorkflowError as e:
        print(f"round close: cannot safely edit .waystone.yml — {e}", file=sys.stderr)
        return 1

    orig_tasks_text = tasks_path.read_text(encoding="utf-8")
    text = orig_tasks_text
    session_id = (os.environ.get("CLAUDE_CODE_SESSION_ID")
                  or os.environ.get("CODEX_THREAD_ID") or None)
    data0 = yaml.safe_load(text) or {}
    by_id = {t.get("id"): t for t in data0.get("tasks", []) if isinstance(t, dict)}
    done_transitions = [task_id for task_id in done
                        if by_id.get(task_id, {}).get("status") != "done"]
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
            # tasks.yaml is the round registry: bind every round-stamped entry to the host session.
            # An absent env value is recorded as YAML null; no timestamp/path heuristic is invented.
            text = set_task_field(text, tid, "session_id", json.dumps(session_id))
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
    final_data = yaml.safe_load(text) or {}
    final_by_id = {task.get("id"): task for task in final_data.get("tasks", [])
                   if isinstance(task, dict)}
    task_scopes: dict[str, list[str]] = {}
    task_scope_coverage: dict[str, str] = {}
    for task_id in dict.fromkeys(done + touched):
        raw_scope = final_by_id.get(task_id, {}).get("scope")
        if raw_scope in (None, []):
            task_scope_coverage[task_id] = "task-scope-unknown"
            continue
        try:
            task_scopes[task_id] = canonical_scope_prefixes(raw_scope)
        except WorkflowError:
            task_scope_coverage[task_id] = "task-scope-invalid"
        else:
            task_scope_coverage[task_id] = (
                "explicit" if task_scopes[task_id] else "task-scope-unknown")

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
        gen_backup = Path(tempfile.mkdtemp(prefix="waystone-ssot-bak-")) / "g"
        shutil.copytree(gen_dir, gen_backup)

    round_exposure = None
    try:
        write_text_atomic(tasks_path, text)
        write_text_atomic(cfg_path, ctext_new)
        write_text_atomic(roadmap_path, roadmap.render(root))
        if cfg.get("ssot"):
            import ssot
            ssot.regenerate(root)  # one full regen; raises WorkflowError (caught below) not sys.exit
        try:
            import overlay
            exposure_path, round_exposure = overlay.write_round_exposure(
                root, round_id, head_sha, full, session_id=session_id,
                base_sha=review_base, task_scopes=task_scopes,
                task_scope_coverage=task_scope_coverage, done_task_ids=done_transitions,
                routes=routes, reviewers=review_reviewers)
            created_event_paths.append(exposure_path)
        except Exception as e:  # noqa: BLE001 — exposure is part of the close transaction
            raise WorkflowError(f"round exposure not recorded: {e}") from e
    except Exception as e:  # noqa: BLE001 — any failure must roll every written artifact back
        write_text_atomic(tasks_path, orig_tasks_text)
        write_text_atomic(cfg_path, ctext)
        if orig_roadmap is None:
            roadmap_path.unlink(missing_ok=True)
        else:
            write_text_atomic(roadmap_path, orig_roadmap)
        if gen_dir is not None:
            shutil.rmtree(gen_dir, ignore_errors=True)
            if gen_existed:
                shutil.copytree(gen_backup, gen_dir)
        for event_path in reversed(created_event_paths):
            event_path.unlink(missing_ok=True)
        if gen_backup is not None:
            shutil.rmtree(gen_backup.parent, ignore_errors=True)
        print(f"round close: closeout failed mid-write and was rolled back — {e}", file=sys.stderr)
        return 1
    if gen_backup is not None:
        shutil.rmtree(gen_backup.parent, ignore_errors=True)

    # Report the actual watermark move separately from the round-bound review base. On a same-round
    # close retry, the watermark has already advanced but generation 1 still names the prior round.
    print(f"round {round_id} closed: {len(done)} done, {len(set(done + touched))} stamped; "
          f"watermark {(prev_wm[:12] if prev_wm else '(root)')} → {full[:12]}")
    print(f"  review diff base = {review_base or '(root)'}  (previous round tip; head = {full})")
    print(f"  review reviewers = {', '.join(review_reviewers)}")

    # Boundary warnings remain advisory. Unlike exposure (part of the transaction above), a warning
    # engine failure is visible but does not invalidate a close whose policy record is durable.
    try:
        import overlay
        try:
            overlay.evaluate_boundary(
                root, "round-close", {"round_id": round_id, "closing_task_ids": done,
                                      "task_ids": list(dict.fromkeys(done + touched)),
                                      "round_record": round_exposure})
        finally:
            # A round override applies through its close boundary, then expires even when the
            # advisory evaluator reports an internal failure.
            overlay.expire_round_overrides(root, round_id)
    except Exception as e:  # noqa: BLE001
        print(f"round close: overlay warning/override expiry unavailable ({e}) — close still succeeded",
              file=sys.stderr)
    try:
        pending = review.pending_reviews(root)
        if pending:
            print(f"round close: reminder: {len(pending)} pending review(s) remain",
                  file=sys.stderr)
            for row in pending:
                print(f"  {review.format_pending_review(row)}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — reminders never invalidate a durable close
        print(f"round close: pending review reminder unavailable ({e}) — close still succeeded",
              file=sys.stderr)
    return 0


def reclose(root: Path, round_id: str) -> int:
    """Record a host-local exposure at a clean descendant HEAD, preserving the round diff base."""
    import overlay
    import review

    if not ROUND_RE.match(round_id):
        print(f"round reclose: --round must match YYYY-MM-DD-<slug>, got {round_id!r}",
              file=sys.stderr)
        return 1
    try:
        previous_path, previous = review.read_round_closeout_exposure(root, round_id)
        head = git_full_sha(root, "HEAD")
        if head is None:
            raise WorkflowError("current HEAD does not resolve to a commit")
        status_rc, status, status_error = git_rc(
            root, "status", "--porcelain=v1", "--untracked-files=no")
        if status_rc != 0:
            raise WorkflowError(f"cannot inspect tracked worktree state: {status_error or 'git failed'}")
        if status:
            # Checked before the same-HEAD no-op: a dirty tree means the closeout is not yet
            # committed, so "already matches" would be a false success.
            raise WorkflowError("tracked worktree changes remain; commit the closeout before reclose")
        if head == previous["head_sha"]:
            print(f"round {round_id} exposure already matches HEAD {head[:12]}")
            return 0
        if not is_ancestor(root, previous["head_sha"], head):
            raise WorkflowError(
                f"current HEAD {head} is not a descendant of prior closeout "
                f"{previous['head_sha']}; close with a new round id")
        mode = previous["review_mode"]
        request = review.prepared_request_path(root, round_id, mode=mode)
        if any(request.parent.glob(f"{round_id}-request.binding*.json")):
            raise WorkflowError(
                "an immutable request sidecar already exists; close with a new round id")
        current_mode = (load_config(root).get("review") or {}).get("mode", "packet")
        if current_mode != mode:
            raise WorkflowError(
                f"review.mode changed from {mode!r} to {current_mode!r}; close with a new round id")
        _path, exposure = overlay.reclose_round_exposure(root, previous_path, previous, head)
    except (OSError, WorkflowError, ValueError) as e:
        print(f"round reclose: {e}", file=sys.stderr)
        return 1
    print(f"round {round_id} reclosed at {head[:12]} (diff base "
          f"{(exposure.get('base_sha') or '(root)')})")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("close", "reclose"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]

    def opt(name):
        return rest[rest.index(name) + 1] if name in rest and rest.index(name) < len(rest) - 1 else None

    def repeated(name):
        values = []
        for index, value in enumerate(rest):
            if value == name:
                if index + 1 >= len(rest) or rest[index + 1].startswith("--"):
                    raise WorkflowError(f"{name} requires a value")
                values.append(rest[index + 1])
        return values

    value_flags = ("--round", "--done", "--touched", "--commit", "--route-note")
    positional = [a for i, a in enumerate(rest) if not a.startswith("--")
                  and (i == 0 or rest[i - 1] not in value_flags)]
    root = Path(positional[0]).resolve() if positional else find_project_root(Path.cwd())
    if root is None:
        print("round: no initialized project", file=sys.stderr)
        return 1
    round_id = opt("--round")
    if not round_id:
        print("round close: --round <id> is required", file=sys.stderr)
        return 1
    try:
        routes = _parse_route_notes(repeated("--route-note")) if sub == "close" else []
        # Phase-2 migration is a separate pre-verb span; close then owns one project-lock span from
        # preflight through exposure/warn recording. Nested libraries must not acquire it again.
        with hold_project_lock(root):
            migrate_project_state(root)
        with hold_project_lock(root):
            if sub == "reclose":
                return reclose(root, round_id)
            return close(root, round_id, _parse_ids(opt("--done")),
                         _parse_ids(opt("--touched")), opt("--commit") or "HEAD", routes)
    except WorkflowError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
