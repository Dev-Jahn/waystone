"""Claude Code session-log parse library (imported, no CLI).

Parse core adapted from fabulous-fable/ccmem (same author): util.py, normalize/tools.py,
normalize/events.py, normalize/coalesce.py, parsers/session_jsonl.py, plus scan.py constants
(UUID_RE/SKIP_DIRS). The layout detectors (`detect_kind`/`scope_of`) are NEW here: fable assumed a
pre-organized `<server>/<project>/` mirror, whereas this reads the real live `~/.claude/projects`
layout. Adaptations to the ported core, all minimal and additive (documented inline):
  * parse_transcript_file distinguishes a truncated final line of an active session
    (`partial_tail_lines`) from a genuine mid-file `parse_error`.
  * classify_record surfaces an `is_api_error` extra (isApiErrorMessage records).
  * _tool_call_row surfaces `model_requested`; _tool_result_rows surface the delegation
    `toolUseResult` fields (tur_agent_id/tur_resolved_model/tur_status/tur_is_async).
Everything else preserves fable's verified behavior (uuid replay dedup, tool_result actor
correction, cli_control exclusion, thinking-not-extracted, per-group usage dedup).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

PARSER_VERSION = "jw-trace-1"


# --------------------------------------------------------------------------- util
def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{prefix}_{digest}"


_MODEL_DATE_SUFFIX = re.compile(r"-20\d{6}$")


def normalize_model(raw: str | None) -> str | None:
    """claude-opus-4-8 -> opus-4-8, claude-haiku-4-5-20251001 -> haiku-4-5."""
    if not raw:
        return None
    name = raw.strip()
    if name.startswith("<") and name.endswith(">"):
        return name.strip("<>")
    name = _MODEL_DATE_SUFFIX.sub("", name)
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    return name


def compact_json(obj, max_len: int = 4096) -> str | None:
    """Compact JSON dump truncated to max_len (marker appended when cut)."""
    if obj is None:
        return None
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > max_len:
        s = s[:max_len] + "...<truncated>"
    return s


def truncate_text(text: str | None, max_len: int) -> tuple[str | None, int]:
    """Return (possibly truncated text, original length)."""
    if text is None:
        return None, 0
    n = len(text)
    if n > max_len:
        return text[:max_len] + "\n...<truncated>", n
    return text, n


# ----------------------------------------------------------------------- tool cat
FILE_READ = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
FILE_WRITE = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
AGENT_SPAWN = {"Agent", "Task", "TaskCreate", "Workflow"}
AGENT_CONTROL = {"TaskGet", "TaskOutput", "TaskStop", "TaskUpdate", "TaskList", "SendMessage", "Monitor"}
PLANNING = {"EnterPlanMode", "ExitPlanMode", "TodoWrite", "TodoRead"}
WEB = {"WebFetch", "WebSearch"}
MEMORY_TOOLS = {"Memory", "MemoryWrite", "MemoryRead"}


def tool_category(tool_name: str, tool_input: dict | None = None) -> str:
    if tool_name == "Bash" or tool_name == "BashOutput" or tool_name == "KillShell":
        return "shell"
    if tool_name in FILE_READ:
        return "file_read"
    if tool_name in FILE_WRITE:
        return "file_write"
    if tool_name in AGENT_SPAWN:
        return "agent_spawn"
    if tool_name in AGENT_CONTROL:
        return "agent_control"
    if tool_name in PLANNING:
        return "planning"
    if tool_name in WEB:
        return "web"
    if tool_name in MEMORY_TOOLS:
        return "memory"
    if tool_name.startswith("mcp__"):
        return "mcp_external"
    if tool_name in {"Skill", "SlashCommand"}:
        return "skill"
    if tool_name in {"AskUserQuestion", "ExitWorktree", "EnterWorktree"}:
        return "session_control"
    if tool_name in {"ToolSearch"}:
        return "tool_discovery"
    if tool_name in {"StructuredOutput"}:
        return "structured_output"
    return "other"


# -------------------------------------------------------------------- classify
EVENT_TEXT_CAP = 32_768

SESSION_STATE_TYPES = {
    "last-prompt": "last_prompt",
    "mode": "mode",
    "permission-mode": "permission_mode",
    "bridge-session": "bridge_session",
    "ai-title": "ai_title",
    "custom-title": "custom_title",
    "queue-operation": "queue_operation",
    "pr-link": "pr_link",
    "agent-setting": "agent_setting",
    "summary": "summary",
}

ACTION_SPACE_ATTACHMENTS = {
    "deferred_tools_delta",
    "agent_listing_delta",
    "skill_listing",
    "command_permissions",
    "dynamic_skill",
}

_COMMAND_NAME_RE = re.compile(r"<command-name>")
_LOCAL_STDOUT_RE = re.compile(r"<local-command-stdout>")
_LOCAL_CAVEAT_RE = re.compile(r"<local-command-caveat>")


def _content_blocks(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _join_text_blocks(blocks: list) -> str:
    parts = []
    for b in blocks:
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "\n".join(parts)


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _join_text_blocks([b for b in content if isinstance(b, dict)])
    return ""


def classify_record(record: dict[str, Any], *, is_sidechain_file: bool, seen_user_instruction: bool) -> dict[str, Any]:
    """Classify one raw JSONL record into canonical (actor, event_type, subtype, text, extras)."""
    raw_type = record.get("type")
    ev: dict[str, Any] = {
        "raw_type": raw_type if isinstance(raw_type, str) else ("attachment" if "attachment" in record else None),
        "actor": "system",
        "event_type": "unknown_raw",
        "event_subtype": None,
        "text": None,
        "uuid": record.get("uuid"),
        "parent_uuid": record.get("parentUuid"),
        "timestamp": record.get("timestamp"),
        "is_meta": bool(record.get("isMeta")),
        "is_sidechain": bool(record.get("isSidechain", is_sidechain_file)),
        "cwd": record.get("cwd"),
        "git_branch": record.get("gitBranch"),
        "request_id": None,
        "message_id": None,
        "model_raw": None,
        "stop_reason": None,
        "content_types": None,
        "tool_use_blocks": [],
        "tool_result_blocks": [],
        "usage": None,
        "extras": {},
    }
    # adaptation: mark API-error records so callers can count errors.api explicitly
    if record.get("isApiErrorMessage"):
        ev["extras"]["is_api_error"] = True

    attachment = record.get("attachment")
    if isinstance(attachment, dict):
        atype = attachment.get("type") or "unknown"
        ev["raw_type"] = "attachment"
        ev["event_subtype"] = atype
        ev["actor"] = "harness"
        if atype in ACTION_SPACE_ATTACHMENTS:
            ev["event_type"] = "action_space_update"
            ev["extras"]["added_names"] = attachment.get("addedNames") or attachment.get("addedTypes")
        else:
            ev["event_type"] = "context_injection"
        for key in ("content", "context", "stdout", "additionalContext"):
            val = attachment.get(key)
            if isinstance(val, str) and val:
                ev["text"], _ = truncate_text(val, EVENT_TEXT_CAP)
                break
        return ev

    if raw_type in SESSION_STATE_TYPES:
        ev["event_type"] = "session_state"
        ev["event_subtype"] = SESSION_STATE_TYPES[raw_type]
        for key in ("mode", "permissionMode", "lastPrompt", "title", "prLink"):
            if isinstance(record.get(key), str):
                ev["extras"][key] = record[key]
        if raw_type == "last-prompt" and isinstance(record.get("lastPrompt"), str):
            ev["text"], _ = truncate_text(record["lastPrompt"], EVENT_TEXT_CAP)
        return ev

    if raw_type == "file-history-snapshot":
        ev["event_type"] = "file_history"
        ev["event_subtype"] = "snapshot"
        ev["actor"] = "harness"
        return ev

    if raw_type == "system":
        ev["event_type"] = "system_event"
        ev["event_subtype"] = record.get("subtype") or "unknown"
        if isinstance(record.get("durationMs"), (int, float)):
            ev["extras"]["duration_ms"] = record["durationMs"]
        content = record.get("content")
        if isinstance(content, str):
            ev["text"], _ = truncate_text(content, EVENT_TEXT_CAP)
        return ev

    if raw_type == "fork-context-ref":
        ev["event_type"] = "subagent_event"
        ev["event_subtype"] = "fork_context_ref"
        ev["actor"] = "harness"
        ev["extras"]["agent_id"] = record.get("agentId")
        ev["extras"]["parent_session_id"] = record.get("parentSessionId")
        ev["extras"]["parent_last_uuid"] = record.get("parentLastUuid")
        ev["extras"]["context_length"] = record.get("contextLength")
        return ev

    if raw_type == "assistant":
        message = record.get("message") or {}
        blocks = _content_blocks(message)
        ctypes = []
        for b in blocks:
            bt = b.get("type")
            ctypes.append("thinking_marker" if bt in ("thinking", "redacted_thinking") else bt)
            if bt == "tool_use":
                ev["tool_use_blocks"].append(b)
        ev["actor"] = "assistant"
        ev["event_type"] = "assistant_fragment"
        ev["event_subtype"] = "+".join(ctypes) if ctypes else "empty"
        ev["content_types"] = ctypes
        # thinking content is an opaque stub: never extract it
        ev["text"], _ = truncate_text(_join_text_blocks(blocks) or None, EVENT_TEXT_CAP)
        ev["request_id"] = record.get("requestId")
        ev["message_id"] = message.get("id")
        ev["model_raw"] = message.get("model")
        ev["stop_reason"] = message.get("stop_reason")
        usage = message.get("usage")
        if isinstance(usage, dict):
            ev["usage"] = usage
        for key in ("attributionSkill", "attributionPlugin",
                    "attributionMcpServer", "attributionMcpTool"):
            if isinstance(record.get(key), str):
                ev["extras"][key] = record[key]
        if isinstance(record.get("version"), str):
            ev["extras"]["cli_version"] = record["version"]
        return ev

    if raw_type == "user":
        message = record.get("message") or {}
        blocks = _content_blocks(message)
        result_blocks = [b for b in blocks if b.get("type") == "tool_result"]
        if result_blocks:
            ev["actor"] = "tool"
            ev["event_type"] = "tool_result"
            ev["event_subtype"] = "error" if any(b.get("is_error") for b in result_blocks) else "output"
            ev["tool_result_blocks"] = result_blocks
            ev["extras"]["source_tool_assistant_uuid"] = record.get("sourceToolAssistantUUID")
            ev["extras"]["tool_use_result"] = record.get("toolUseResult")
            joined = "\n".join(filter(None, (_tool_result_text(b) for b in result_blocks)))
            ev["text"], _ = truncate_text(joined or None, EVENT_TEXT_CAP)
            return ev

        text = _join_text_blocks(blocks)
        ev["text"], _ = truncate_text(text or None, EVENT_TEXT_CAP)
        # a compaction summary is harness-authored context, not a user instruction
        if record.get("isCompactSummary"):
            ev["actor"] = "harness"
            ev["event_type"] = "context_injection"
            ev["event_subtype"] = "compact_summary"
            if isinstance(record.get("logicalParentUuid"), str):
                ev["extras"]["logical_parent_uuid"] = record["logicalParentUuid"]
            return ev
        if _COMMAND_NAME_RE.search(text):
            ev["actor"] = "cli"
            ev["event_type"] = "cli_control"
            ev["event_subtype"] = "slash_command"
            m = re.search(r"<command-name>([^<]*)</command-name>", text)
            if m:
                ev["extras"]["command_name"] = m.group(1).strip()
            return ev
        if _LOCAL_STDOUT_RE.search(text):
            ev["actor"] = "cli"
            ev["event_type"] = "cli_control"
            ev["event_subtype"] = "local_command_stdout"
            return ev
        if _LOCAL_CAVEAT_RE.search(text):
            ev["actor"] = "cli"
            ev["event_type"] = "cli_control"
            ev["event_subtype"] = "local_command_caveat"
            return ev
        if ev["is_meta"]:
            ev["actor"] = "cli"
            ev["event_type"] = "cli_control"
            ev["event_subtype"] = "meta"
            return ev
        if text.lstrip().startswith("<system-reminder>"):
            ev["actor"] = "harness"
            ev["event_type"] = "context_injection"
            ev["event_subtype"] = "system_reminder"
            return ev
        ev["actor"] = "user"
        ev["event_type"] = "user_instruction"
        if is_sidechain_file and not seen_user_instruction:
            ev["event_subtype"] = "worker_directive"
        elif seen_user_instruction:
            ev["event_subtype"] = "followup"
        else:
            ev["event_subtype"] = "natural_language"
        return ev

    ev["event_type"] = "unknown_raw"
    ev["event_subtype"] = str(raw_type) if raw_type else "no_type"
    return ev


def model_norm_of(ev: dict[str, Any]) -> str | None:
    return normalize_model(ev.get("model_raw"))


# -------------------------------------------------------------------- coalesce
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

GROUP_TEXT_CAP = 32_768


def coalesce_messages(events: list[dict[str, Any]], tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build message_group rows from one file's event rows (log order preserved)."""
    calls_by_event: dict[str, list[dict]] = {}
    for tc in tool_calls:
        calls_by_event.setdefault(tc["event_id"], []).append(tc)

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in events:
        if ev["event_type"] != "assistant_fragment":
            continue
        key = ev.get("message_id") or ev.get("request_id") or ev["event_id"]
        if key not in groups:
            order.append(key)
            groups[key] = {
                "message_group_id": stable_id("msggrp", ev["file_id"], key),
                "message_id": ev.get("message_id"),
                "request_id": ev.get("request_id"),
                "file_id": ev["file_id"],
                "server": ev["server"],
                "project": ev["project"],
                "session_id": ev["session_id"],
                "agent_id": ev["agent_id"],
                "workflow_id": ev["workflow_id"],
                "is_sidechain": ev["is_sidechain"],
                "model_norm": ev.get("model_norm"),
                "model_raw": ev.get("model_raw"),
                "first_event_id": ev["event_id"],
                "first_ordinal": ev["ordinal"],
                "last_ordinal": ev["ordinal"],
                "timestamp_first": ev.get("timestamp"),
                "timestamp_last": ev.get("timestamp"),
                "turn_index": ev["turn_index"],
                "fragment_count": 0,
                "content_sequence": [],
                "has_thinking": False,
                "_texts": [],
                "tool_call_ids": [],
                "stop_reason_group": None,
                "_usages": [],
            }
        g = groups[key]
        g["fragment_count"] += 1
        g["last_ordinal"] = ev["ordinal"]
        g["timestamp_last"] = ev.get("timestamp") or g["timestamp_last"]
        if ev.get("content_types"):
            g["content_sequence"].extend(ev["content_types"].split("+"))
        if ev.get("content_types") and "thinking_marker" in ev["content_types"]:
            g["has_thinking"] = True
        if ev.get("text"):
            g["_texts"].append(ev["text"])
        if ev.get("stop_reason"):
            g["stop_reason_group"] = ev["stop_reason"]
        for tc in calls_by_event.get(ev["event_id"], []):
            g["tool_call_ids"].append(tc["tool_call_id"])
            tc["message_group_id"] = g["message_group_id"]
        if ev.get("usage_json"):
            g["_usages"].append(ev["usage_json"])

    rows = []
    for key in order:
        g = groups[key]
        usage, consistent = _dedupe_usage(g.pop("_usages"))
        text, text_len = truncate_text("\n".join(g.pop("_texts")) or None, GROUP_TEXT_CAP)
        g["text"] = text
        g["text_len"] = text_len
        g["content_sequence"] = "+".join(g["content_sequence"]) if g["content_sequence"] else None
        g["tool_call_count"] = len(g["tool_call_ids"])
        g["tool_call_ids"] = ",".join(filter(None, g["tool_call_ids"])) or None
        for k in USAGE_KEYS:
            g[k] = usage.get(k)
        g["usage_consistent"] = consistent
        rows.append(g)
    return rows


def _dedupe_usage(usage_jsons: list[str]) -> tuple[dict, bool]:
    """Pick one representative usage per group; report cross-fragment consistency."""
    parsed = []
    for s in usage_jsons:
        try:
            u = json.loads(s)
            if isinstance(u, dict):
                parsed.append(u)
        except json.JSONDecodeError:
            continue
    if not parsed:
        return {}, True
    rep = parsed[-1]
    keyset = [tuple(u.get(k) for k in ("input_tokens", "cache_read_input_tokens")) for u in parsed]
    consistent = len(set(keyset)) == 1
    return {k: rep.get(k) for k in USAGE_KEYS if isinstance(rep.get(k), (int, float))}, consistent


# --------------------------------------------------------------------- parser
RESULT_TEXT_CAP = 16_384


def parse_transcript_file(
    path: Path,
    *,
    file_id: str,
    server: str | None,
    project: str | None,
    session_id: str | None,
    agent_id: str | None,
    workflow_id: str | None,
    is_sidechain_file: bool,
) -> dict[str, Any]:
    """Parse one transcript file into events / tool_calls / tool_results rows.

    Returns those lists plus `replayed_skipped` (resume/compaction re-emissions dropped) and
    `partial_tail_lines` (a truncated final line of an active session — NOT a parse_error)."""
    events: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    turn_index = 0
    seen_user_instruction = False
    seen_uuids: set[str] = set()
    replayed_skipped = 0
    partial_tail_lines = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, start=1):
            # a truncated final line of an active session has no trailing newline; only the last
            # line of a file can lack one, so "decode failed AND no newline" == partial tail.
            had_newline = raw.endswith("\n")
            line = raw.strip()
            if not line:
                continue
            event_id = stable_id("evt", file_id, str(line_no))
            base = {
                "event_id": event_id,
                "file_id": file_id,
                "line_no": line_no,
                "ordinal": line_no,
                "server": server,
                "project": project,
                "session_id": session_id,
                "agent_id": agent_id,
                "workflow_id": workflow_id,
                "parse_status": "ok",
            }
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
            except (json.JSONDecodeError, ValueError) as exc:
                if not had_newline:
                    partial_tail_lines += 1
                    continue
                events.append(
                    base
                    | {
                        "raw_type": "parse_error",
                        "actor": "system",
                        "event_type": "unknown_raw",
                        "event_subtype": "parse_error",
                        "text": None,
                        "uuid": None,
                        "parent_uuid": None,
                        "timestamp": None,
                        "is_meta": False,
                        "is_sidechain": is_sidechain_file,
                        "cwd": None,
                        "git_branch": None,
                        "request_id": None,
                        "message_id": None,
                        "model_raw": None,
                        "model_norm": None,
                        "stop_reason": None,
                        "content_types": None,
                        "turn_index": turn_index,
                        "extras_json": None,
                        "usage_json": None,
                        "parse_status": f"error:{exc}"[:200],
                    }
                )
                continue

            rec_uuid = record.get("uuid")
            if isinstance(rec_uuid, str) and rec_uuid:
                if rec_uuid in seen_uuids:
                    replayed_skipped += 1
                    continue
                seen_uuids.add(rec_uuid)

            ev = classify_record(
                record,
                is_sidechain_file=is_sidechain_file,
                seen_user_instruction=seen_user_instruction,
            )
            if ev["event_type"] == "user_instruction":
                seen_user_instruction = True
                turn_index += 1

            if ev["event_subtype"] == "fork_context_ref" and ev["extras"].get("agent_id"):
                base["agent_id"] = base["agent_id"] or ev["extras"]["agent_id"]

            row = base | {
                "raw_type": ev["raw_type"],
                "actor": ev["actor"],
                "event_type": ev["event_type"],
                "event_subtype": ev["event_subtype"],
                "text": ev["text"],
                "uuid": ev["uuid"],
                "parent_uuid": ev["parent_uuid"],
                "timestamp": ev["timestamp"],
                "is_meta": ev["is_meta"],
                "is_sidechain": ev["is_sidechain"],
                "cwd": ev["cwd"],
                "git_branch": ev["git_branch"],
                "request_id": ev["request_id"],
                "message_id": ev["message_id"],
                "model_raw": ev["model_raw"],
                "model_norm": model_norm_of(ev),
                "stop_reason": ev["stop_reason"],
                "content_types": "+".join(ev["content_types"]) if ev["content_types"] else None,
                "turn_index": turn_index,
                "extras_json": compact_json(ev["extras"]) if ev["extras"] else None,
            }
            row["usage_json"] = compact_json(ev["usage"]) if ev["usage"] else None
            events.append(row)

            for block in ev["tool_use_blocks"]:
                tool_calls.append(_tool_call_row(block, row, ev))
            if ev["tool_result_blocks"]:
                tool_results.extend(_tool_result_rows(ev, row))

    return {"events": events, "tool_calls": tool_calls, "tool_results": tool_results,
            "replayed_skipped": replayed_skipped, "partial_tail_lines": partial_tail_lines}


def _tool_call_row(block: dict, event_row: dict, ev: dict) -> dict[str, Any]:
    tool_name = block.get("name") or "unknown"
    raw_input = block.get("input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else None
    description = tool_input.get("description") if isinstance(tool_input.get("description"), str) else None
    subagent_type = tool_input.get("subagent_type") if isinstance(tool_input.get("subagent_type"), str) else None
    # adaptation: the requested model for an Agent/Task spawn (delegations schema, explicit)
    model_requested = tool_input.get("model") if isinstance(tool_input.get("model"), str) else None
    prompt = tool_input.get("prompt") if isinstance(tool_input.get("prompt"), str) else None
    file_path = tool_input.get("file_path") if isinstance(tool_input.get("file_path"), str) else None
    return {
        "tool_call_id": block.get("id"),
        "event_id": event_row["event_id"],
        "file_id": event_row["file_id"],
        "server": event_row["server"],
        "project": event_row["project"],
        "session_id": event_row["session_id"],
        "agent_id": event_row["agent_id"],
        "workflow_id": event_row["workflow_id"],
        "ordinal": event_row["ordinal"],
        "timestamp": event_row["timestamp"],
        "turn_index": event_row["turn_index"],
        "is_sidechain": event_row["is_sidechain"],
        "message_id": ev["message_id"],
        "model_norm": event_row["model_norm"],
        "tool_name_raw": tool_name,
        "tool_category": tool_category(tool_name, tool_input),
        "command": command,
        "description": description,
        "subagent_type": subagent_type,
        "model_requested": model_requested,
        "prompt_head": truncate_text(prompt, 2000)[0] if prompt else None,
        "file_path": file_path,
        "input_json": compact_json(tool_input, 8192),
    }


def _tool_result_rows(ev: dict, event_row: dict) -> list[dict[str, Any]]:
    rows = []
    tur = ev["extras"].get("tool_use_result")
    stdout = stderr = None
    interrupted = None
    is_image = None
    # adaptation: delegation routing evidence lives on the toolUseResult of an agent_spawn result
    tur_agent_id = tur_resolved_model = tur_status = tur_is_async = None
    if isinstance(tur, dict):
        stdout = tur.get("stdout") if isinstance(tur.get("stdout"), str) else None
        stderr = tur.get("stderr") if isinstance(tur.get("stderr"), str) else None
        interrupted = bool(tur.get("interrupted")) if "interrupted" in tur else None
        is_image = bool(tur.get("isImage")) if "isImage" in tur else None
        tur_agent_id = tur.get("agentId") if isinstance(tur.get("agentId"), str) else None
        tur_resolved_model = tur.get("resolvedModel") if isinstance(tur.get("resolvedModel"), str) else None
        tur_status = tur.get("status") if isinstance(tur.get("status"), str) else None
        tur_is_async = tur.get("isAsync") if isinstance(tur.get("isAsync"), bool) else None
    for block in ev["tool_result_blocks"]:
        content = block.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = ""
        text_trunc, text_len = truncate_text(text or None, RESULT_TEXT_CAP)
        stderr_head, stderr_len = truncate_text(stderr, 2000)
        rows.append(
            {
                "tool_use_id": block.get("tool_use_id"),
                "event_id": event_row["event_id"],
                "file_id": event_row["file_id"],
                "server": event_row["server"],
                "project": event_row["project"],
                "session_id": event_row["session_id"],
                "agent_id": event_row["agent_id"],
                "workflow_id": event_row["workflow_id"],
                "ordinal": event_row["ordinal"],
                "timestamp": event_row["timestamp"],
                "turn_index": event_row["turn_index"],
                "is_sidechain": event_row["is_sidechain"],
                "is_error": bool(block.get("is_error")),
                "interrupted": interrupted,
                "is_image": is_image,
                "content_text": text_trunc,
                "content_len": text_len,
                "stdout_len": len(stdout) if stdout is not None else None,
                "stderr_len": stderr_len if stderr is not None else None,
                "stderr_head": stderr_head,
                "source_tool_assistant_uuid": ev["extras"].get("source_tool_assistant_uuid"),
                "tur_agent_id": tur_agent_id,
                "tur_resolved_model": tur_resolved_model,
                "tur_status": tur_status,
                "tur_is_async": tur_is_async,
            }
        )
    return rows


# ------------------------------------------------------------- layout detectors
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SKIP_DIRS = {".git", ".ccmem", "__pycache__"}

# file kind -> session-row kind (only transcript kinds have a session row)
TRANSCRIPT_KINDS = {
    "main_transcript": "main",
    "subagent_transcript": "subagent",
    "workflow_subagent_transcript": "workflow_subagent",
}

_AGENT_NAME_RE = re.compile(r"^agent-([0-9a-zA-Z-]+?)(?:\.meta)?\.(?:jsonl|json)$")


def _agent_id_from_name(name: str) -> str | None:
    m = _AGENT_NAME_RE.match(name)
    return m.group(1) if m else None


def detect_kind(rel_parts: tuple[str, ...]) -> str:
    """Classify a file by its path segments relative to the `projects` source root.

    Real layout:
        <slug>/<uuid>.jsonl                                       main transcript
        <slug>/<uuid>/subagents/agent-<hex>.jsonl                 subagent transcript
        <slug>/<uuid>/subagents/agent-<hex>.meta.json             subagent meta
        <slug>/<uuid>/subagents/workflows/wf_*/agent-<hex>.jsonl  workflow-subagent transcript
        <slug>/<uuid>/workflows/wf_*.json                         workflow json (manifest only)
        <slug>/<uuid>/workflows/scripts/*.js                      workflow script (manifest only)
        <slug>/<uuid>/tool-results/*.txt                          tool result (manifest only)
        <slug>/memory/*.md                                        memory (manifest only)
    """
    if not rel_parts:
        return "unknown_other"
    name = rel_parts[-1]
    parents = rel_parts[:-1]

    in_subagents = "subagents" in parents
    in_workflows = "workflows" in parents

    if len(parents) >= 1 and parents[-1] == "memory":
        return "memory" if name.endswith(".md") else "unknown_other"

    if name == "journal.jsonl" and in_workflows:
        return "workflow_journal"
    if name.startswith("agent-") and name.endswith(".meta.json"):
        return "subagent_meta"
    if name.startswith("agent-") and name.endswith(".jsonl"):
        if in_subagents and in_workflows:
            return "workflow_subagent_transcript"
        if in_subagents:
            return "subagent_transcript"
        return "unknown_jsonl"
    if name.endswith(".jsonl"):
        stem = name[: -len(".jsonl")]
        if len(parents) == 1 and not in_subagents and UUID_RE.match(stem):
            return "main_transcript"
        return "unknown_jsonl"
    if name.endswith(".json"):
        if in_workflows and not in_subagents and name.startswith("wf_"):
            return "workflow_json"
        return "unknown_json"
    if name.endswith(".js") and len(parents) >= 1 and parents[-1] == "scripts" and in_workflows:
        return "workflow_script"
    if len(parents) >= 1 and parents[-1] == "tool-results":
        return "tool_result"
    return "unknown_other"


def scope_of(rel_parts: tuple[str, ...]) -> dict[str, str | None]:
    """Derive (project, session_id, agent_id, workflow_id) from a file's path segments."""
    kind = detect_kind(rel_parts)
    project = rel_parts[0] if rel_parts else None
    name = rel_parts[-1] if rel_parts else ""
    session_id = agent_id = workflow_id = None

    if kind == "main_transcript":
        session_id = name[: -len(".jsonl")]
    elif len(rel_parts) >= 2 and UUID_RE.match(rel_parts[1]):
        session_id = rel_parts[1]

    if kind in ("subagent_transcript", "workflow_subagent_transcript", "subagent_meta"):
        agent_id = _agent_id_from_name(name)
    if kind == "workflow_subagent_transcript":
        for part in rel_parts:
            if part.startswith("wf_"):
                workflow_id = part
                break
    if kind == "workflow_json":
        workflow_id = name[: -len(".json")]

    return {"project": project, "session_id": session_id, "agent_id": agent_id, "workflow_id": workflow_id}
