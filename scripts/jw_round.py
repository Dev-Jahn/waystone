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
  3. set state.last_round_commit to the round's tip,
  4. report the SSOT churn since the previous round watermark (bulk-edit quarantine signal).

The text-surgery helpers (set_task_field, set_config_scalar) are pure and tested.

Usage (also `jw round close`):
  jw_round.py close [root] --round <id> [--done id,id] [--touched id,id] [--commit HEAD]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jw_common import (  # noqa: E402
    ROUND_RE, find_project_root, git, git_full_sha, load_config,
)


# ---- pure text surgery -------------------------------------------------------
def _task_block_span(lines: list[str], task_id: str) -> tuple[int, int] | None:
    """Return (start, end) line indices of the `- id: <task_id>` block, end exclusive."""
    id_re = re.compile(r'^(\s*)-\s+id:\s*["\']?' + re.escape(task_id) + r'["\']?\s*$')
    start = None
    indent = 0
    for i, ln in enumerate(lines):
        m = id_re.match(ln)
        if m:
            start = i
            indent = len(m.group(1))
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        cur = len(ln) - len(ln.lstrip())
        if cur <= indent and (ln.lstrip().startswith("- ") or cur < indent):
            return (start, j)
    return (start, len(lines))


def set_task_field(text: str, task_id: str, field: str, value: str) -> str:
    """Set `field: value` inside a task block, preserving all other content/comments.
    Updates the field if present, else inserts it right after the id line. Raises if the
    task is absent (a round must not silently no-op)."""
    lines = text.splitlines(keepends=True)
    span = _task_block_span(lines, task_id)
    if span is None:
        raise KeyError(f"task id not found in registry: {task_id}")
    start, end = span
    nl = "\n" if not lines[start].endswith("\r\n") else "\r\n"
    field_indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    field_re = re.compile(r"^(\s*)" + re.escape(field) + r":\s*.*$")
    for k in range(start + 1, end):
        if field_re.match(lines[k]):
            ind = lines[k][: len(lines[k]) - len(lines[k].lstrip())]
            lines[k] = f"{ind}{field}: {value}{nl}"
            return "".join(lines)
    lines.insert(start + 1, f"{' ' * field_indent}{field}: {value}{nl}")
    return "".join(lines)


def set_config_scalar(text: str, key: str, value: str) -> str:
    """Replace the value of a `<key>:` line (e.g. last_round_commit) preserving indent/comments.
    Matches the first such key line at any indent. Raises if absent."""
    line_re = re.compile(r"^(\s*)" + re.escape(key) + r":\s*.*$")
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        m = line_re.match(ln)
        if m:
            nl = "\r\n" if ln.endswith("\r\n") else "\n"
            lines[i] = f"{m.group(1)}{key}: {value}{nl}"
            return "".join(lines)
    raise KeyError(f"config key not found: {key}")


# ---- orchestration -----------------------------------------------------------
def _parse_ids(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def close(root: Path, round_id: str, done: list[str], touched: list[str], commit: str) -> int:
    if not ROUND_RE.match(round_id):
        print(f"jw_round close: --round must match YYYY-MM-DD-<slug>, got {round_id!r}", file=sys.stderr)
        return 1
    cfg = load_config(root)
    tasks_path = root / "tasks.yaml"
    text = tasks_path.read_text(encoding="utf-8")
    try:
        for tid in done:
            text = set_task_field(text, tid, "status", "done")
        for tid in dict.fromkeys(done + touched):  # stamp round on everything worked, de-duped
            text = set_task_field(text, tid, "round", round_id)
    except KeyError as e:
        print(f"jw_round close: {e}", file=sys.stderr)
        return 1
    tasks_path.write_text(text, encoding="utf-8")

    # validate + regenerate views (import the deterministic siblings)
    import yaml
    import jw_roadmap
    import jw_validate
    errs = jw_validate.validate(yaml.safe_load(tasks_path.read_text(encoding="utf-8")))
    if errs:
        print(f"jw_round close: tasks.yaml invalid after edits ({len(errs)} issue(s)) — review/revert:",
              file=sys.stderr)
        for e in errs[:10]:
            print(f"  - {e}", file=sys.stderr)
        return 2
    (root / "ROADMAP.md").write_text(jw_roadmap.render(root), encoding="utf-8")
    if cfg.get("ssot"):
        import jw_ssot
        jw_ssot.split(root)
        jw_ssot.digest(root)

    # churn since the previous round watermark (before we overwrite it)
    cfg_path = root / ".jahns-workflow.yml"
    prev = (cfg.get("state") or {}).get("last_round_commit")
    full = git_full_sha(root, commit) or commit
    churn = None
    if prev and cfg.get("ssot"):
        stat = git(root, "diff", "--numstat", f"{prev}..{full}", "--", cfg["ssot"])
        if stat:
            adds = sum(int(p.split("\t")[0]) for p in stat.splitlines() if p.split("\t")[0].isdigit())
            dels = sum(int(p.split("\t")[1]) for p in stat.splitlines() if p.split("\t")[1].isdigit())
            churn = adds + dels

    # advance the watermark
    ctext = cfg_path.read_text(encoding="utf-8")
    try:
        ctext = set_config_scalar(ctext, "last_round_commit", full)
        cfg_path.write_text(ctext, encoding="utf-8")
        wm = "set"
    except KeyError:
        wm = "MISSING state.last_round_commit — add it to .jahns-workflow.yml"

    print(f"round {round_id} closed: {len(done)} done, {len(set(done + touched))} stamped; "
          f"watermark {wm} @ {full[:12]}")
    if churn is not None:
        flag = "  ⚠ BULK EDIT (>100 lines) — run /jahns-workflow:audit on changed sections" if churn > 100 else ""
        print(f"SSOT churn since last round: {churn} lines{flag}")
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
        print("jw_round: no initialized project", file=sys.stderr)
        return 1
    round_id = opt("--round")
    if not round_id:
        print("jw_round close: --round <id> is required", file=sys.stderr)
        return 1
    return close(root, round_id, _parse_ids(opt("--done")), _parse_ids(opt("--touched")), opt("--commit") or "HEAD")


if __name__ == "__main__":
    sys.exit(main())
