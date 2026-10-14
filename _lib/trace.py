"""Codex JSONL trace parsing, rollout provenance lookup, subagent rollout capture, and skill snapshot reading."""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from pathlib import Path
from typing import Any

from _lib.common import concatenate_files, write_json


COLLAB_EVENT_TYPES = frozenset(
    {"collab_tool_call", "CollabAgentToolCall", "collab_agent_tool_call"}
)
COLLAB_WAIT_TOOLS = frozenset({"wait", "wait_agent"})


def _strip_encrypted_content(value: Any) -> Any:
    """Recursively strip opaque `encrypted_content` fields while preserving all other data."""
    if isinstance(value, dict):
        return {
            k: _strip_encrypted_content(v)
            for k, v in value.items()
            if k != "encrypted_content"
        }
    if isinstance(value, list):
        return [_strip_encrypted_content(item) for item in value]
    return value


def sanitize_trace_jsonl(source: Path, dest: Path) -> None:
    """Copy a JSONL trace or rollout from `source` to `dest`, stripping only
    opaque `encrypted_content` ciphertext while preserving every record and other field.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    out_lines: list[str] = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        cleaned = _strip_encrypted_content(parsed)
        out_lines.append(json.dumps(cleaned, ensure_ascii=False))
    dest.write_text(
        ("\n".join(out_lines) + "\n") if out_lines else "",
        encoding="utf-8",
    )


def capture_main_rollout(
    turn_traces: list[Path],
    dest_trace: Path,
    thread_id: str | None,
    sessions_root: Path,
) -> Path | None:
    """Write `dest_trace` from the persisted session rollout for `thread_id`
    (stripping `encrypted_content`), or fall back to concatenating `turn_traces`
    when no persisted rollout exists. Strictly READ-ONLY on `sessions_root`.
    Returns the source rollout path when found, or None when falling back.
    """
    dest_trace.parent.mkdir(parents=True, exist_ok=True)
    rollout = find_rollout(thread_id, sessions_root)
    if rollout is not None and rollout.is_file():
        sanitize_trace_jsonl(rollout, dest_trace)
        return rollout
    concatenate_files(turn_traces, dest_trace)
    if dest_trace.is_file():
        sanitize_trace_jsonl(dest_trace, dest_trace)
    return None


def _empty_token_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "billed_output_tokens": 0,
    }


def _finalize_token_usage(
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "uncached_input_tokens": max(input_tokens - cached_input_tokens, 0),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "billed_output_tokens": output_tokens + reasoning_output_tokens,
    }


def trace_thread_id(trace: Path) -> str | None:
    """Return the persisted Codex thread ID recorded in a JSONL trace."""
    if not trace.is_file():
        return None
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                return payload["id"]
    return None


def read_turn_context(rollout: Path) -> dict[str, str | None]:
    """Extract the model and reasoning effort from a persisted Codex rollout.

    Pure (reads one file, no network/subprocess). Codex records these in
    `turn_context` records as `payload.model` and `payload.effort`. A multi-turn
    session has several; the LAST one reflects the final turn's settings. Returns
    `{"model": None, "reasoning_effort": None}` when the file is absent or has no
    `turn_context` record, so callers never crash on an ephemeral/missing rollout.
    """
    model: str | None = None
    effort: str | None = None
    if not rollout.is_file():
        return {"model": None, "reasoning_effort": None}
    for line in rollout.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn_context":
            continue
        payload = event.get("payload", event)
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("model"), str):
            model = payload["model"]
        if isinstance(payload.get("effort"), str):
            effort = payload["effort"]
    return {"model": model, "reasoning_effort": effort}


def find_rollout(thread_id: str | None, sessions_root: Path) -> Path | None:
    """Locate the persisted rollout file for a thread id. READ-ONLY.

    Codex names rollouts `rollout-<timestamp>-<thread-id>.jsonl` under a
    date-nested tree. This only READS the sessions directory to find a match; it
    never writes, moves, or deletes anything there. Returns None when the thread
    id is unknown, the directory is absent, or no file matches.
    """
    if not thread_id or not sessions_root.is_dir():
        return None
    matches = sorted(sessions_root.rglob(f"rollout-*{thread_id}*.jsonl"))
    return matches[0] if matches else None


def read_skill_snapshot_text(run_dir: Path, metadata: dict[str, Any]) -> str | None:
    """Read the captured SKILL.md text from a run's immutable skill snapshot.

    READ-ONLY. Resolves the `skill_snapshot` path recorded in run.json relative
    to its run dir and returns the snapshot's SKILL.md content, or None when no
    snapshot was captured (e.g. a baseline trial) or the file is absent. Pure
    enough to unit-test with a temporary directory.
    """
    relative = metadata.get("skill_snapshot")
    if not isinstance(relative, str) or not relative:
        return None
    skill_md = run_dir / relative / "SKILL.md"
    if not skill_md.is_file():
        return None
    return skill_md.read_text(encoding="utf-8", errors="replace")


def _normalize_command_text(raw_command: Any) -> str:
    if isinstance(raw_command, list):
        return " ".join(str(part) for part in raw_command)
    if raw_command is None:
        return ""
    return str(raw_command)


def _extract_command_item(event: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a command execution event across live (`command_execution`) and
    persisted rollout (`event_msg:item_completed` `CommandExecution` / `exec_command_end`)
    Codex trace schemas.

    Skips `item.started` events so live `--json` streams that emit both
    `item.started` and `item.completed` for the same command are counted once.
    """
    ev_type = event.get("type")
    if ev_type == "item.started":
        return None
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in (
        "command_execution",
        "CommandExecution",
    ):
        status = item.get("status")
        return {
            "command": _normalize_command_text(item.get("command")),
            "status": str(status).lower() if isinstance(status, str) else "completed",
            "exit_code": item.get("exit_code"),
        }
    if ev_type == "event_msg":
        payload = event.get("payload")
        if isinstance(payload, dict):
            p_type = payload.get("type")
            if p_type == "item_completed":
                p_item = payload.get("item")
                if isinstance(p_item, dict) and p_item.get("type") in (
                    "CommandExecution",
                    "command_execution",
                ):
                    status = p_item.get("status")
                    return {
                        "command": _normalize_command_text(p_item.get("command")),
                        "status": (
                            str(status).lower()
                            if isinstance(status, str)
                            else "completed"
                        ),
                        "exit_code": p_item.get("exit_code"),
                    }
            elif p_type == "exec_command_end":
                return {
                    "command": _normalize_command_text(payload.get("command")),
                    "status": "completed",
                    "exit_code": payload.get("exit_code"),
                }
    return None


def _extract_collab_item(event: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a collab tool call event across live (`collab_tool_call`) and
    persisted (`CollabAgentToolCall`) Codex trace schemas.

    Skips `item.started` events so live `--json` streams that emit both
    `item.started` and `item.completed` for the same tool call are counted once.
    """
    if event.get("type") == "item.started":
        return None
    payload = event.get("payload")
    payload_item = payload.get("item") if isinstance(payload, dict) else None
    for candidate in (event.get("item"), payload_item, payload, event):
        if not isinstance(candidate, dict):
            continue
        cand_type = candidate.get("type")
        has_collab_signature = isinstance(candidate.get("tool"), str) and (
            isinstance(candidate.get("receiver_thread_ids"), list)
            or isinstance(candidate.get("targets"), list)
            or isinstance(candidate.get("target_thread_id"), str)
        )
        if cand_type in COLLAB_EVENT_TYPES or has_collab_signature:
            raw_receivers = (
                candidate.get("receiver_thread_ids")
                if isinstance(candidate.get("receiver_thread_ids"), list)
                else (
                    candidate.get("targets")
                    if isinstance(candidate.get("targets"), list)
                    else (
                        [candidate["target_thread_id"]]
                        if isinstance(candidate.get("target_thread_id"), str)
                        else []
                    )
                )
            )
            receiver_ids = [
                tid for tid in raw_receivers if isinstance(tid, str) and tid
            ]
            prompt = candidate.get("prompt")
            if not isinstance(prompt, str):
                prompt = (
                    candidate.get("message")
                    if isinstance(candidate.get("message"), str)
                    else None
                )
            agents_states = (
                candidate.get("agents_states")
                if isinstance(candidate.get("agents_states"), dict)
                else {}
            )
            return {
                "id": candidate.get("id") if isinstance(candidate.get("id"), str) else None,
                "tool": candidate.get("tool")
                if isinstance(candidate.get("tool"), str)
                else None,
                "status": candidate.get("status")
                if isinstance(candidate.get("status"), str)
                else None,
                "sender_thread_id": candidate.get("sender_thread_id")
                if isinstance(candidate.get("sender_thread_id"), str)
                else None,
                "receiver_thread_ids": receiver_ids,
                "prompt": prompt,
                "agents_states": agents_states,
            }
    return None


def _summarize_collab_calls(
    collab_calls: list[dict[str, Any]],
    default_parent_tid: str | None = None,
) -> dict[str, Any]:
    """Summarize normalized collab tool calls into spawn/wait counts and per-child records."""
    spawn_count = 0
    wait_count = 0
    thread_ids: list[str] = []
    seen_tids: set[str] = set()
    child_meta: dict[str, dict[str, Any]] = {}

    for call in collab_calls:
        tool = call.get("tool")
        receivers = call.get("receiver_thread_ids") or []
        sender = call.get("sender_thread_id") or default_parent_tid
        if tool == "spawn_agent":
            spawn_count += 1
        elif tool in COLLAB_WAIT_TOOLS:
            wait_count += 1

        for tid in receivers:
            if tid not in seen_tids:
                seen_tids.add(tid)
                thread_ids.append(tid)
                child_meta[tid] = {
                    "thread_id": tid,
                    "parent_thread_id": sender,
                    "prompt": None,
                    "waited": False,
                    "final_status": None,
                    "final_message": None,
                }
            entry = child_meta[tid]
            if sender and not entry.get("parent_thread_id"):
                entry["parent_thread_id"] = sender
            if tool == "spawn_agent" and call.get("prompt") and not entry.get("prompt"):
                entry["prompt"] = call["prompt"]
            if tool in COLLAB_WAIT_TOOLS:
                entry["waited"] = True

        agents_states = call.get("agents_states") or {}
        for tid, state in agents_states.items():
            if not isinstance(tid, str) or not tid:
                continue
            if not isinstance(state, (dict, str)):
                continue
            if tid not in seen_tids:
                seen_tids.add(tid)
                thread_ids.append(tid)
                child_meta[tid] = {
                    "thread_id": tid,
                    "parent_thread_id": sender,
                    "prompt": None,
                    "waited": tool in COLLAB_WAIT_TOOLS,
                    "final_status": None,
                    "final_message": None,
                }
            entry = child_meta[tid]
            if isinstance(state, str) and state:
                entry["final_status"] = state
            elif isinstance(state, dict):
                if isinstance(state.get("status"), str):
                    entry["final_status"] = state["status"]
                    if isinstance(state.get("message"), str) and state["message"]:
                        entry["final_message"] = state["message"]
                elif len(state) == 1:
                    # Persisted rollout Rust-enum shape, e.g. {"completed": "<msg>"}
                    st_key, st_val = next(iter(state.items()))
                    if isinstance(st_key, str) and st_key:
                        entry["final_status"] = st_key
                    if isinstance(st_val, str) and st_val:
                        entry["final_message"] = st_val

    return {
        "subagent_spawn_count": spawn_count,
        "subagent_wait_count": wait_count,
        "subagent_thread_ids": thread_ids,
        "subagents": [child_meta[tid] for tid in thread_ids],
    }


def trace_metrics(trace: Path, skill_name: str | None = None) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    parse_errors = 0
    if trace.is_file():
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(value, dict):
                events.append(value)

    commands: list[dict[str, Any]] = []
    collab_calls: list[dict[str, Any]] = []
    turn_completed_seen = False
    input_tokens = 0
    cached_input_tokens = 0
    cache_write_input_tokens = 0
    output_tokens = 0
    reasoning_output_tokens = 0
    latest_total_usage: dict[str, Any] | None = None
    parent_tid: str | None = None

    for event in events:
        ev_type = event.get("type")
        if ev_type == "thread.started" and isinstance(event.get("thread_id"), str):
            parent_tid = event["thread_id"]
        elif ev_type == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                parent_tid = payload["id"]

        command_item = _extract_command_item(event)
        if command_item is not None:
            commands.append(command_item)

        collab = _extract_collab_item(event)
        if collab is not None:
            collab_calls.append(collab)

        if ev_type == "turn.completed" and isinstance(event.get("usage"), dict):
            turn_completed_seen = True
            usage = event["usage"]
            input_tokens += int(usage.get("input_tokens", 0) or 0)
            cached_input_tokens += int(usage.get("cached_input_tokens", 0) or 0)
            cache_write_input_tokens += int(
                usage.get("cache_write_input_tokens", 0) or 0
            )
            output_tokens += int(usage.get("output_tokens", 0) or 0)
            reasoning_output_tokens += int(
                usage.get("reasoning_output_tokens", 0) or 0
            )
        elif ev_type == "event_msg":
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(
                    info.get("total_token_usage"), dict
                ):
                    latest_total_usage = info["total_token_usage"]
                elif isinstance(payload.get("total_token_usage"), dict):
                    latest_total_usage = payload["total_token_usage"]

    command_texts = [str(command.get("command", "")) for command in commands]
    repeated = sum(count - 1 for count in Counter(command_texts).values() if count > 1)
    prohibited = [
        command
        for command in command_texts
        if re.search(r"(^|\s)git\s+push(\s|$)|git\s+reset\s+--hard", command)
    ]
    skill_read_observed = None
    if skill_name:
        skill_read_observed = any(
            skill_name.lower() in command.lower() and "skill.md" in command.lower()
            for command in command_texts
        )

    collab_summary = _summarize_collab_calls(collab_calls, default_parent_tid=parent_tid)
    if turn_completed_seen:
        token_usage = _finalize_token_usage(
            input_tokens,
            cached_input_tokens,
            cache_write_input_tokens,
            output_tokens,
            reasoning_output_tokens,
        )
    elif latest_total_usage is not None:
        token_usage = _finalize_token_usage(
            int(latest_total_usage.get("input_tokens", 0) or 0),
            int(latest_total_usage.get("cached_input_tokens", 0) or 0),
            int(latest_total_usage.get("cache_write_input_tokens", 0) or 0),
            int(latest_total_usage.get("output_tokens", 0) or 0),
            int(latest_total_usage.get("reasoning_output_tokens", 0) or 0),
        )
    else:
        token_usage = _empty_token_usage()

    return {
        "event_count": len(events),
        "parse_errors": parse_errors,
        "command_count": len(commands),
        "failed_command_count": sum(
            command.get("status") == "completed"
            and command.get("exit_code") not in (None, 0)
            for command in commands
        ),
        "repeated_command_count": repeated,
        "prohibited_commands": prohibited,
        "skill_read_observed": skill_read_observed,
        **token_usage,
        **collab_summary,
    }


def read_rollout_metrics(rollout: Path) -> dict[str, Any]:
    """Read lineage, model/effort, command counts, nested spawns, and token usage
    from a persisted Codex rollout JSONL file. Strictly READ-ONLY.
    """
    if not rollout.is_file():
        return {
            "thread_id": None,
            "parent_thread_id": None,
            "depth": None,
            "agent_nickname": None,
            "agent_role": None,
            "model": None,
            "reasoning_effort": None,
            "final_status": None,
            "final_message": None,
            "event_count": 0,
            "parse_errors": 0,
            "command_count": 0,
            "failed_command_count": 0,
            "collab_calls": [],
            "spawned_thread_ids": [],
            "token_usage": _empty_token_usage(),
        }

    thread_id: str | None = None
    parent_thread_id: str | None = None
    depth: int | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    model: str | None = None
    effort: str | None = None
    final_status: str | None = None
    final_message: str | None = None
    event_count = 0
    parse_errors = 0
    command_count = 0
    failed_command_count = 0
    collab_calls: list[dict[str, Any]] = []

    turn_completed_seen = False
    tc_input = 0
    tc_cached = 0
    tc_cache_write = 0
    tc_output = 0
    tc_reasoning = 0
    latest_total_usage: dict[str, Any] | None = None

    for line in rollout.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(event, dict):
            continue
        event_count += 1
        ev_type = event.get("type")

        if ev_type == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        elif ev_type == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict):
                if isinstance(payload.get("id"), str):
                    thread_id = payload["id"]
                if isinstance(payload.get("parent_thread_id"), str):
                    parent_thread_id = payload["parent_thread_id"]
                if isinstance(payload.get("agent_nickname"), str):
                    agent_nickname = payload["agent_nickname"]
                if isinstance(payload.get("agent_role"), str):
                    agent_role = payload["agent_role"]
                source = payload.get("source")
                if isinstance(source, dict):
                    sub = source.get("subagent")
                    if isinstance(sub, dict):
                        spawn = sub.get("thread_spawn")
                        if isinstance(spawn, dict):
                            if isinstance(spawn.get("parent_thread_id"), str):
                                parent_thread_id = spawn["parent_thread_id"]
                            if isinstance(spawn.get("depth"), int) and not isinstance(
                                spawn.get("depth"), bool
                            ):
                                depth = spawn["depth"]
                            if isinstance(spawn.get("agent_nickname"), str):
                                agent_nickname = spawn["agent_nickname"]
                            if isinstance(spawn.get("agent_role"), str):
                                agent_role = spawn["agent_role"]
        elif ev_type == "turn_context":
            payload = event.get("payload", event)
            if isinstance(payload, dict):
                if isinstance(payload.get("model"), str):
                    model = payload["model"]
                if isinstance(payload.get("effort"), str):
                    effort = payload["effort"]

        command_item = _extract_command_item(event)
        if command_item is not None:
            command_count += 1
            if command_item.get("status") == "completed" and command_item.get(
                "exit_code"
            ) not in (None, 0):
                failed_command_count += 1

        if ev_type == "event_msg":
            payload = event.get("payload")
            if isinstance(payload, dict):
                if payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict) and isinstance(
                        info.get("total_token_usage"), dict
                    ):
                        latest_total_usage = info["total_token_usage"]
                    elif isinstance(payload.get("total_token_usage"), dict):
                        latest_total_usage = payload["total_token_usage"]
                elif payload.get("type") == "task_complete":
                    final_status = "completed"
                    if (
                        isinstance(payload.get("last_agent_message"), str)
                        and payload["last_agent_message"]
                    ):
                        final_message = payload["last_agent_message"]

        if ev_type == "turn.completed" and isinstance(event.get("usage"), dict):
            turn_completed_seen = True
            usage = event["usage"]
            tc_input += int(usage.get("input_tokens", 0) or 0)
            tc_cached += int(usage.get("cached_input_tokens", 0) or 0)
            tc_cache_write += int(usage.get("cache_write_input_tokens", 0) or 0)
            tc_output += int(usage.get("output_tokens", 0) or 0)
            tc_reasoning += int(usage.get("reasoning_output_tokens", 0) or 0)

        collab = _extract_collab_item(event)
        if collab is not None:
            collab_calls.append(collab)

    if turn_completed_seen:
        token_usage = _finalize_token_usage(
            tc_input, tc_cached, tc_cache_write, tc_output, tc_reasoning
        )
    elif latest_total_usage is not None:
        token_usage = _finalize_token_usage(
            int(latest_total_usage.get("input_tokens", 0) or 0),
            int(latest_total_usage.get("cached_input_tokens", 0) or 0),
            int(latest_total_usage.get("cache_write_input_tokens", 0) or 0),
            int(latest_total_usage.get("output_tokens", 0) or 0),
            int(latest_total_usage.get("reasoning_output_tokens", 0) or 0),
        )
    else:
        token_usage = _empty_token_usage()

    collab_summary = _summarize_collab_calls(collab_calls, default_parent_tid=thread_id)
    return {
        "thread_id": thread_id,
        "parent_thread_id": parent_thread_id,
        "depth": depth,
        "agent_nickname": agent_nickname,
        "agent_role": agent_role,
        "model": model,
        "reasoning_effort": effort,
        "final_status": final_status,
        "final_message": final_message,
        "event_count": event_count,
        "parse_errors": parse_errors,
        "command_count": command_count,
        "failed_command_count": failed_command_count,
        "collab_calls": collab_calls,
        "spawned_thread_ids": collab_summary["subagent_thread_ids"],
        "subagent_spawn_count": collab_summary["subagent_spawn_count"],
        "subagent_wait_count": collab_summary["subagent_wait_count"],
        "subagents": collab_summary["subagents"],
        "token_usage": token_usage,
    }


def capture_subagent_rollouts(
    parent_trace: Path,
    subagents_dir: Path,
    sessions_root: Path,
) -> dict[str, Any]:
    """Discover spawned subagent threads in `parent_trace` (and recursively in
    captured child rollouts), copy their persisted rollouts read-only from
    `sessions_root` into `subagents_dir` (stripping `encrypted_content`), and
    return aggregated subagent metrics.

    Strictly READ-ONLY on `sessions_root`. If no subagents were spawned, does not
    create `subagents_dir`. Missing rollout files degrade gracefully with
    `rollout_captured: False`.
    """
    parent_metrics = trace_metrics(parent_trace)
    parent_tid = trace_thread_id(parent_trace)
    total_spawn_count = int(parent_metrics.get("subagent_spawn_count", 0))
    total_wait_count = int(parent_metrics.get("subagent_wait_count", 0))
    initial_children = list(parent_metrics.get("subagent").values()) if isinstance(parent_metrics.get("subagent"), dict) else list(parent_metrics.get("subagents", []))

    if total_spawn_count == 0 and not initial_children:
        return {
            "parent_thread_id": parent_tid,
            "subagent_spawn_count": 0,
            "subagent_wait_count": 0,
            "subagent_thread_ids": [],
            "subagent_token_usage": _empty_token_usage(),
            "subagents": [],
        }

    subagents_dir.mkdir(parents=True, exist_ok=True)
    queue: deque[dict[str, Any]] = deque(initial_children)
    visited: set[str] = set()
    if parent_tid:
        visited.add(parent_tid)

    all_thread_ids: list[str] = []
    subagents_list: list[dict[str, Any]] = []
    depth_by_tid: dict[str, int] = {}
    if parent_tid:
        depth_by_tid[parent_tid] = 0

    tot_input = 0
    tot_cached = 0
    tot_cache_write = 0
    tot_output = 0
    tot_reasoning = 0

    while queue:
        stub = queue.popleft()
        tid = stub.get("thread_id")
        if not isinstance(tid, str) or not tid or tid in visited:
            continue
        visited.add(tid)
        all_thread_ids.append(tid)

        inferred_parent = stub.get("parent_thread_id") or parent_tid
        inferred_depth = depth_by_tid.get(inferred_parent, 0) + 1 if inferred_parent else 1

        source_rollout = find_rollout(tid, sessions_root)
        if source_rollout is not None and source_rollout.is_file():
            dest_rollout = subagents_dir / f"{tid}.jsonl"
            sanitize_trace_jsonl(source_rollout, dest_rollout)
            info = read_rollout_metrics(dest_rollout)
            resolved_parent = info["parent_thread_id"] or inferred_parent
            resolved_depth = (
                info["depth"]
                if info["depth"] is not None
                else (
                    depth_by_tid.get(resolved_parent, 0) + 1
                    if resolved_parent
                    else inferred_depth
                )
            )
            depth_by_tid[tid] = resolved_depth
            usage = info["token_usage"]
            tot_input += usage["input_tokens"]
            tot_cached += usage["cached_input_tokens"]
            tot_cache_write += usage["cache_write_input_tokens"]
            tot_output += usage["output_tokens"]
            tot_reasoning += usage["reasoning_output_tokens"]

            record = {
                "thread_id": tid,
                "parent_thread_id": resolved_parent,
                "depth": resolved_depth,
                "agent_nickname": info["agent_nickname"],
                "agent_role": info["agent_role"],
                "model": info["model"],
                "reasoning_effort": info["reasoning_effort"],
                "prompt": stub.get("prompt"),
                "waited": bool(stub.get("waited", False)),
                "final_status": stub.get("final_status") or info.get("final_status"),
                "final_message": stub.get("final_message") or info.get("final_message"),
                "rollout_captured": True,
                "rollout_file": f"subagents/{tid}.jsonl",
                "command_count": info["command_count"],
                "failed_command_count": info["failed_command_count"],
                "token_usage": usage,
            }
            subagents_list.append(record)

            # Enqueue any nested sub-subagents spawned by this child rollout
            total_spawn_count += int(info.get("subagent_spawn_count", 0))
            total_wait_count += int(info.get("subagent_wait_count", 0))
            for nested_stub in info.get("subagents", []):
                nested_tid = nested_stub.get("thread_id")
                if isinstance(nested_tid, str) and nested_tid and nested_tid not in visited:
                    if not nested_stub.get("parent_thread_id"):
                        nested_stub["parent_thread_id"] = tid
                    queue.append(nested_stub)
        else:
            depth_by_tid[tid] = inferred_depth
            record = {
                "thread_id": tid,
                "parent_thread_id": inferred_parent,
                "depth": inferred_depth,
                "agent_nickname": None,
                "agent_role": None,
                "model": None,
                "reasoning_effort": None,
                "prompt": stub.get("prompt"),
                "waited": bool(stub.get("waited", False)),
                "final_status": stub.get("final_status"),
                "final_message": stub.get("final_message"),
                "rollout_captured": False,
                "rollout_file": None,
                "command_count": 0,
                "failed_command_count": 0,
                "token_usage": _empty_token_usage(),
            }
            subagents_list.append(record)

    subagent_token_usage = _finalize_token_usage(
        tot_input, tot_cached, tot_cache_write, tot_output, tot_reasoning
    )
    manifest = {
        "parent_thread_id": parent_tid,
        "subagent_spawn_count": total_spawn_count,
        "subagent_wait_count": total_wait_count,
        "subagent_thread_ids": all_thread_ids,
        "subagent_token_usage": subagent_token_usage,
        "subagents": subagents_list,
    }
    write_json(subagents_dir / "manifest.json", manifest)
    return manifest


def merge_subagent_metrics(
    metrics: dict[str, Any],
    subagent_capture: dict[str, Any],
) -> dict[str, Any]:
    """Roll up parent + subagent token usage in `metrics` while preserving both
    `parent_token_usage` and `subagent_token_usage` breakdowns.
    """
    merged = dict(metrics)
    parent_tokens = _finalize_token_usage(
        int(merged.get("input_tokens", 0) or 0),
        int(merged.get("cached_input_tokens", 0) or 0),
        int(merged.get("cache_write_input_tokens", 0) or 0),
        int(merged.get("output_tokens", 0) or 0),
        int(merged.get("reasoning_output_tokens", 0) or 0),
    )
    sub_tokens_raw = subagent_capture.get("subagent_token_usage") or _empty_token_usage()
    sub_tokens = _finalize_token_usage(
        int(sub_tokens_raw.get("input_tokens", 0) or 0),
        int(sub_tokens_raw.get("cached_input_tokens", 0) or 0),
        int(sub_tokens_raw.get("cache_write_input_tokens", 0) or 0),
        int(sub_tokens_raw.get("output_tokens", 0) or 0),
        int(sub_tokens_raw.get("reasoning_output_tokens", 0) or 0),
    )
    total_tokens = _finalize_token_usage(
        parent_tokens["input_tokens"] + sub_tokens["input_tokens"],
        parent_tokens["cached_input_tokens"] + sub_tokens["cached_input_tokens"],
        parent_tokens["cache_write_input_tokens"]
        + sub_tokens["cache_write_input_tokens"],
        parent_tokens["output_tokens"] + sub_tokens["output_tokens"],
        parent_tokens["reasoning_output_tokens"]
        + sub_tokens["reasoning_output_tokens"],
    )

    merged.update(total_tokens)
    merged["parent_token_usage"] = parent_tokens
    merged["subagent_token_usage"] = sub_tokens
    merged["subagent_spawn_count"] = int(
        subagent_capture.get(
            "subagent_spawn_count", merged.get("subagent_spawn_count", 0)
        )
        or 0
    )
    merged["subagent_wait_count"] = int(
        subagent_capture.get(
            "subagent_wait_count", merged.get("subagent_wait_count", 0)
        )
        or 0
    )
    merged["subagent_thread_ids"] = list(
        subagent_capture.get(
            "subagent_thread_ids", merged.get("subagent_thread_ids", [])
        )
    )
    merged["subagents"] = list(
        subagent_capture.get("subagents", merged.get("subagents", []))
    )
    return merged


def classify_invocation(
    has_skill: bool, prompt_texts: list[str], skill_name: str
) -> str:
    """Classify how the target skill was invoked, from the worker prompts.

    Pure (no I/O). Returns one of: "no-skill-baseline" (skill not injected),
    "explicit" (a prompt names the skill as `$name` or `/name`), or
    "implicit-candidate" (skill present but not named in any prompt).
    """
    if not has_skill:
        return "no-skill-baseline"
    explicit_names = (f"${skill_name}", f"/{skill_name}")
    explicitly_named = any(
        name in prompt for prompt in prompt_texts for name in explicit_names
    )
    return "explicit" if explicitly_named else "implicit-candidate"

