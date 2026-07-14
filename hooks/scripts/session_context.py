#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""SessionStart hook body: emit additionalContext (SSOT digest + active tasks + branch).

Called by session_context.sh with the project root as argv[1]; hook JSON on stdin (unused
beyond what the wrapper extracted). Output is capped to keep per-session token cost low.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from common import git_branch_info, git_full_sha, load_config, load_tasks, next_actionable, resume_path, start_here_path  # noqa: E402

MAX_CHARS = 8000
MAX_TASK_LINES = 8
MAX_START_HERE = 2560  # ~2.5KB cap on the injected re-entry narrative (read-time, never truncates the file)
MAX_CONTRACT = 1200
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "references" / "main-contract.md"


def _routing_line() -> str:
    import delegate

    path = delegate._profile_path()
    if not path.is_file():
        return "routing: no profile — jw delegate will guide setup"
    try:
        profile, _fingerprint = delegate._load_profile()
        bindings = profile.get("bindings")
        if not isinstance(bindings, dict):
            raise ValueError("bindings is not a mapping")
        roles = sorted(bindings, key=lambda role: (role != "implementer", role))
        rendered = []
        for role in roles:
            binding = bindings[role]
            if isinstance(binding, dict) and isinstance(binding.get("backend"), str):
                rendered.append(f"{role}→{binding['backend']}")
        if not rendered:
            raise ValueError("no readable bindings")
        return "routing: " + " · ".join(rendered)
    except Exception:  # noqa: BLE001 — one damaged live input must not break SessionStart
        return "routing: — unreadable"


def _overlay_line(root: Path) -> str:
    try:
        import overlay
        deltas = overlay.list_deltas(root)
        unreadable = any(d.get("corrupt") for d in deltas)
        active = [d for d in deltas if not d.get("corrupt")
                  and d.get("status") in ("observing", "warning")]
        budget = 5
        parts = []
        for status in ("warning", "observing"):
            ids = sorted(d["id"] for d in active if d.get("status") == status)
            shown = ids[:budget]
            budget -= len(shown)
            suffix = f" ({' '.join(shown)}{' …' if len(ids) > len(shown) else ''})" if shown else ""
            parts.append(f"{status} {len(ids)}{suffix}")
        return "overlay: " + " · ".join(parts) + (" · — unreadable" if unreadable else "")
    except Exception:  # noqa: BLE001
        return "overlay: — unreadable"


def _delegation_summary(root: Path) -> str:
    try:
        import delegate
        ids = []
        unreadable = False
        for did, rec in delegate._iter_delegations(root):
            status = delegate._read_status_raw(rec)
            if status is None:
                unreadable = True
            elif status.get("state") == "needs-review":
                ids.append(did)
        ids.sort()
        shown = ids[:5]
        suffix = f" ({' '.join(shown)}{' …' if len(ids) > len(shown) else ''})" if shown else ""
        return f"needs-review delegations {len(ids)}{suffix}" + (" · — unreadable" if unreadable else "")
    except Exception:  # noqa: BLE001
        return "needs-review delegations — unreadable"


def _evidence_summary(root: Path) -> str | None:
    path = Path.home() / ".claude" / "jahns-workflow" / "improve" / "evidence.jsonl"
    if not path.is_file():
        return None
    try:
        aliases = {root.name}
        data = load_tasks(root)
        if isinstance(data.get("project"), str):
            aliases.add(data["project"])
        registry = Path.home() / ".claude" / "jahns-workflow" / "projects.json"
        if registry.is_file():
            reg = json.loads(registry.read_text(encoding="utf-8"))
            for entry in reg.get("projects", []):
                if (isinstance(entry, dict) and entry.get("path")
                        and Path(entry["path"]).expanduser().resolve() == root.resolve()
                        and isinstance(entry.get("name"), str)):
                    aliases.add(entry["name"])
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not row.get("task_id") or row.get("project") not in aliases:
                continue
            if row.get("findings") and any(
                    d.get("verification_present") is False
                    for d in row.get("delegations") or [] if isinstance(d, dict)):
                count += 1
        return f"evidence.jsonl: unverified+finding tasks {count}"
    except Exception:  # noqa: BLE001
        return "evidence.jsonl: — unreadable"


def _operating_contract(root: Path) -> list[str]:
    """Bounded best-effort contract block. Constitution absence omits the block; each live input is
    independently degradable, and an unexpected assembly failure returns no block (R10a)."""
    try:
        if not CONTRACT_PATH.is_file():
            return []
        constitution = CONTRACT_PATH.read_text(encoding="utf-8").strip()
        if not constitution:
            return []
        lines = ["◆ OPERATING CONTRACT (jahns-workflow)", *constitution.splitlines(),
                 _routing_line(), _overlay_line(root)]
        live = "live: " + _delegation_summary(root)
        evidence = _evidence_summary(root)
        if evidence:
            live += " · " + evidence
        lines.append(live)
        text = "\n".join(lines)
        if len(text) > MAX_CONTRACT:
            text = text[:MAX_CONTRACT - 1].rstrip() + "…"
        return text.splitlines()
    except Exception:  # noqa: BLE001 — hook availability outranks the optional block
        return []


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    try:
        cfg = load_config(root)
        data = load_tasks(root)
    except Exception as e:  # malformed config must not break session start
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"[jahns-workflow] config/tasks unreadable: {e}",
        }}))
        return 0

    g = git_branch_info(root)
    tasks = [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    done = sum(1 for t in tasks if t.get("status") == "done")
    active = [t for t in tasks if t.get("status") == "active"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    decisions = [t for t in tasks if t.get("id", "").startswith("decision/") and t.get("status") not in ("done", "dropped")]
    rounds = sorted({t["round"] for t in active if t.get("round")})

    lines = [
        f"[jahns-workflow] project: {data.get('project', root.name)} | branch: {g['branch']}"
        f" ({'dirty +' + str(g['dirty']) if g['dirty'] else 'clean'}) | tasks: {done}/{len(tasks)} done",
    ]
    lines.extend(_operating_contract(root))

    # persistent re-entry pointer (model-authored at round close / after review) — surfaced FIRST so a
    # new or post-compaction session picks up the live frontier without a manual "pick up". Read-time
    # capped; the file itself is never truncated. Authoritative state still lives in tasks.yaml/PROGRESS.
    sh = start_here_path(root)
    if sh.is_file():
        try:
            body = sh.read_text(encoding="utf-8").strip()
        except OSError:
            body = ""
        if body:
            if len(body) > MAX_START_HERE:
                body = body[:MAX_START_HERE].rstrip() + "\n…[START_HERE truncated — keep it ≤~35 lines]"
            lines.append("▶ START HERE (re-entry pointer — rewritten at round close / after review):")
            lines.append(body)

    if rounds:
        lines.append(f"active round: {', '.join(rounds)}")
    for label, group in (("active", active), ("blocked", blocked), ("pending decision", decisions)):
        for t in group[:MAX_TASK_LINES]:
            lines.append(f"  {label}: {t['id']} — {t.get('title', '')}")
    nxt = next_actionable(data, cap=5)
    if nxt:
        lines.append("next actionable (deps satisfied):")
        for tid, title in nxt:
            lines.append(f"  → {tid} — {title}")
    lines.append(f"Task registry: tasks.yaml | Roadmap: ROADMAP.md | Conventions: see CLAUDE.md workflow section")

    # consume a PreCompact/SessionEnd resume pointer if one was left, flagging staleness
    rp = resume_path(root)
    if rp.is_file():
        try:
            snap = rp.read_text(encoding="utf-8")
            captured = next((ln.split(":", 1)[1].strip() for ln in snap.splitlines()
                             if ln.startswith("captured_head:")), "")
            at = next((ln.split(":", 1)[1].strip() for ln in snap.splitlines()
                       if ln.startswith("captured_at:")), "")
            cur = git_full_sha(root, "HEAD") or ""
            stale = " [STALE: HEAD has moved since]" if captured and cur and captured != cur else ""
            lines.append(f"last checkpoint: {at} @ {captured[:12]}{stale}")
            rp.unlink()  # consume — a fresh one is written at the next PreCompact/SessionEnd
        except OSError:
            pass

    digest = root / cfg["generated_dir"] / "DIGEST.md"
    if digest.is_file():
        lines.append("")
        lines.append(digest.read_text(encoding="utf-8").rstrip())
    elif cfg.get("ssot"):
        lines.append(f"SSOT: {cfg['ssot']} (no digest generated yet — run /jahns-workflow:round or ssot.py digest)")

    ctx = "\n".join(lines)
    if len(ctx) > MAX_CHARS:
        ctx = ctx[:MAX_CHARS] + "\n…[truncated by jahns-workflow cap]"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
