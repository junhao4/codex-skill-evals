"""Per-project Codex session usage tracking, incremental rollout cache, and HTML/Markdown reports."""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

from _lib.common import _fmt, markdown_cell, now, read_json, write_json
from _lib.pricing import (
    LONG_CONTEXT_THRESHOLD,
    PRICING_AS_OF,
    PRICING_SOURCE,
    estimate_api_cost,
    format_cost,
)
from _lib.trace import (
    _collab_calls,
    _empty_token_usage,
    _extract_command_item,
    _finalize_token_usage,
    _summarize_collab_calls,
)


CACHE_SCHEMA_VERSION = 1
ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)\.jsonl$"
)
SKILL_BLOCK_RE = re.compile(
    r"<skill>\s*.*?<name>\s*([a-zA-Z0-9][a-zA-Z0-9_-]*)\s*</name>.*?</skill>",
    re.DOTALL,
)
SKILL_PATH_RE = re.compile(
    r"(?:^|[/\s\"'])skills/([a-zA-Z0-9][a-zA-Z0-9_-]*)/SKILL\.md\b",
    re.IGNORECASE,
)
EXPLICIT_SKILL_TOKEN_RE = re.compile(r"(?:^|[\s(])[\$/]([a-z0-9][a-z0-9-]*)\b")


def default_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def resolve_project_root(evals_root: Path) -> Path:
    """Resolve the host repository root from `.agents/evals`, `workflow/evals`, or a standalone checkout."""
    resolved = evals_root.resolve()
    if resolved.parent.name in (".agents", "workflow"):
        return resolved.parents[1]
    return resolved


def usage_output_dir(evals_root: Path) -> Path:
    return evals_root / "_usage"


def _is_subpath(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_eval_run_cwd(cwd: str | Path | None, evals_root: Path) -> bool:
    """Return True when `cwd` is inside an evaluation trial directory (`<skill>/runs/...`)."""
    if not cwd:
        return False
    cwd_path = Path(cwd)
    try:
        rel = cwd_path.resolve().relative_to(evals_root.resolve())
    except ValueError:
        return False
    parts = rel.parts
    return "runs" in parts or "_usage" in parts


def is_project_work_cwd(
    cwd: str | Path | None, project_root: Path, evals_root: Path
) -> bool:
    """Return True when `cwd` belongs to `project_root` and is not a synthetic eval trial workspace."""
    if not cwd:
        return False
    cwd_path = Path(cwd)
    if not _is_subpath(cwd_path, project_root):
        return False
    if is_eval_run_cwd(cwd_path, evals_root):
        return False
    return True


def _extract_timestamp_from_filename(path: Path) -> str | None:
    match = ROLLOUT_FILENAME_RE.match(path.name)
    if not match:
        return None
    raw = match.group(1)
    # Convert YYYY-MM-DDTHH-MM-SS to ISO-like YYYY-MM-DDTHH:MM:SSZ
    date_part, time_part = raw.split("T", 1)
    return f"{date_part}T{time_part.replace('-', ':')}Z"


def _clean_user_prompt(text: str) -> str:
    """Strip injected XML blocks (`<skill>`, `<environment_context>`, etc.) from a user prompt for display."""
    cleaned = re.sub(r"<skill>.*?</skill>", " ", text, flags=re.DOTALL)
    cleaned = re.sub(
        r"<environment_context>.*?</environment_context>",
        " ",
        cleaned,
        flags=re.DOTALL,
    )
    cleaned = re.sub(r"<permissions[^>]*>.*?</permissions>", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _extract_user_texts(event: dict[str, Any]) -> list[str]:
    """Extract raw user message strings from a rollout or live trace event."""
    texts: list[str] = []
    ev_type = event.get("type")
    payload = event.get("payload")
    if ev_type == "event_msg" and isinstance(payload, dict):
        if payload.get("type") == "user_message" and isinstance(
            payload.get("message"), str
        ):
            texts.append(payload["message"])
    if isinstance(payload, dict) and payload.get("type") == "message":
        if payload.get("role") == "user" and isinstance(
            payload.get("content"), list
        ):
            for block in payload["content"]:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    return texts


def parse_session_rollout(rollout: Path) -> dict[str, Any]:
    """Parse a single Codex rollout JSONL file into a compact, cacheable session summary.

    Strictly READ-ONLY. Handles both interactive session rollouts and `codex exec` rollouts.
    """
    thread_id: str | None = None
    parent_thread_id: str | None = None
    depth: int | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    cwd: str | None = None
    started_at: str | None = _extract_timestamp_from_filename(rollout)
    model: str | None = None
    effort: str | None = None
    final_status: str | None = None

    event_count = 0
    parse_errors = 0
    turn_completed_count = 0
    task_complete_count = 0
    turn_context_count = 0
    user_message_count = 0

    commands: list[dict[str, Any]] = []
    user_texts: list[str] = []
    injected_skills: set[str] = set()
    command_read_skills: set[str] = set()

    turn_completed_seen = False
    tc_input = 0
    tc_cached = 0
    tc_cache_write = 0
    tc_output = 0
    tc_reasoning = 0
    latest_total_usage: dict[str, Any] | None = None

    events: list[dict[str, Any]] = []
    if rollout.is_file():
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
            events.append(event)
            event_count += 1
            ev_type = event.get("type")

            if started_at is None and isinstance(event.get("timestamp"), str):
                started_at = event["timestamp"]

            if ev_type == "thread.started" and isinstance(event.get("thread_id"), str):
                thread_id = event["thread_id"]
            elif ev_type == "session_meta":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    if isinstance(payload.get("id"), str):
                        thread_id = payload["id"]
                    if isinstance(payload.get("timestamp"), str):
                        started_at = payload["timestamp"]
                    if isinstance(payload.get("cwd"), str) and payload["cwd"]:
                        cwd = payload["cwd"]
                    if isinstance(payload.get("parent_thread_id"), str):
                        parent_thread_id = payload["parent_thread_id"]
                    if isinstance(payload.get("agent_nickname"), str):
                        agent_nickname = payload["agent_nickname"]
                    if isinstance(payload.get("agent_role"), str):
                        agent_role = payload["agent_role"]
                    elif isinstance(payload.get("agent_type"), str):
                        agent_role = payload["agent_type"]
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
                                elif isinstance(spawn.get("agent_type"), str):
                                    agent_role = spawn["agent_type"]
            elif ev_type == "turn_context":
                turn_context_count += 1
                payload = event.get("payload", event)
                if isinstance(payload, dict):
                    if isinstance(payload.get("cwd"), str) and payload["cwd"] and not cwd:
                        cwd = payload["cwd"]
                    if isinstance(payload.get("model"), str):
                        model = payload["model"]
                    if isinstance(payload.get("effort"), str):
                        effort = payload["effort"]

            for utext in _extract_user_texts(event):
                user_texts.append(utext)
                for match in SKILL_BLOCK_RE.finditer(utext):
                    injected_skills.add(match.group(1).lower())

            if ev_type == "event_msg":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    p_type = payload.get("type")
                    if p_type == "user_message":
                        user_message_count += 1
                    elif p_type == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict) and isinstance(
                            info.get("total_token_usage"), dict
                        ):
                            latest_total_usage = info["total_token_usage"]
                        elif isinstance(payload.get("total_token_usage"), dict):
                            latest_total_usage = payload["total_token_usage"]
                    elif p_type == "task_complete":
                        task_complete_count += 1
                        final_status = "completed"

            if ev_type == "turn.completed" and isinstance(event.get("usage"), dict):
                turn_completed_seen = True
                turn_completed_count += 1
                usage = event["usage"]
                tc_input += int(usage.get("input_tokens", 0) or 0)
                tc_cached += int(usage.get("cached_input_tokens", 0) or 0)
                tc_cache_write += int(usage.get("cache_write_input_tokens", 0) or 0)
                tc_output += int(usage.get("output_tokens", 0) or 0)
                tc_reasoning += int(usage.get("reasoning_output_tokens", 0) or 0)

            command_item = _extract_command_item(event)
            if command_item is not None:
                commands.append(command_item)
                if command_item.get("exit_code") in (None, 0):
                    cmd_str = str(command_item.get("command", ""))
                    for match in SKILL_PATH_RE.finditer(cmd_str):
                        command_read_skills.add(match.group(1).lower())

    if thread_id is None:
        match = ROLLOUT_FILENAME_RE.match(rollout.name)
        if match:
            thread_id = match.group(2)

    if started_at is None and rollout.is_file():
        started_at = datetime.fromtimestamp(
            rollout.stat().st_mtime, tz=timezone.utc
        ).isoformat()

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

    collab_calls = _collab_calls(events)
    seen_call_ids = {
        c.get("id") for c in collab_calls if isinstance(c.get("id"), str)
    }
    for event in events:
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        p_type = payload.get("type")
        call_id = payload.get("call_id")
        if p_type == "collab_agent_spawn_end":
            new_tid = payload.get("new_thread_id")
            if isinstance(new_tid, str) and new_tid and call_id not in seen_call_ids:
                collab_calls.append(
                    {
                        "id": call_id if isinstance(call_id, str) else None,
                        "tool": "spawn_agent",
                        "status": "completed",
                        "sender_thread_id": payload.get("sender_thread_id")
                        if isinstance(payload.get("sender_thread_id"), str)
                        else thread_id,
                        "receiver_thread_ids": [new_tid],
                        "prompt": payload.get("prompt")
                        if isinstance(payload.get("prompt"), str)
                        else None,
                        "agent_role": (
                            payload.get("new_agent_role")
                            or payload.get("agent_role")
                            or payload.get("agent_type")
                        ),
                        "agents_states": (
                            {new_tid: payload["status"]}
                            if isinstance(payload.get("status"), (dict, str))
                            else {}
                        ),
                    }
                )
    collab_summary = _summarize_collab_calls(
        collab_calls, default_parent_tid=thread_id
    )

    cleaned_prompts = [
        cleaned for raw in user_texts if (cleaned := _clean_user_prompt(raw))
    ]
    first_prompt = cleaned_prompts[0][:160] if cleaned_prompts else ""

    explicit_tokens: set[str] = set()
    for text in cleaned_prompts:
        for match in EXPLICIT_SKILL_TOKEN_RE.finditer(text.lower()):
            explicit_tokens.add(match.group(1).lower())

    all_skills = sorted(injected_skills | command_read_skills)
    skill_invocations: dict[str, str] = {}
    for skill in all_skills:
        if skill in explicit_tokens or skill in injected_skills:
            skill_invocations[skill] = "explicit"
        else:
            skill_invocations[skill] = "implicit"

    turn_count = max(
        turn_completed_count,
        task_complete_count,
        turn_context_count,
        user_message_count,
        1 if event_count > 0 else 0,
    )
    failed_command_count = sum(
        cmd.get("status") == "completed" and cmd.get("exit_code") not in (None, 0)
        for cmd in commands
    )

    return {
        "thread_id": thread_id,
        "parent_thread_id": parent_thread_id,
        "depth": depth,
        "agent_nickname": agent_nickname,
        "agent_role": agent_role,
        "cwd": cwd,
        "started_at": started_at,
        "model": model,
        "reasoning_effort": effort,
        "final_status": final_status,
        "turn_count": turn_count,
        "event_count": event_count,
        "parse_errors": parse_errors,
        "command_count": len(commands),
        "failed_command_count": failed_command_count,
        "first_prompt": first_prompt,
        "explicit_skill_tokens": sorted(explicit_tokens),
        "skills_used": all_skills,
        "skill_invocations": skill_invocations,
        "spawned_thread_ids": collab_summary["subagent_thread_ids"],
        "subagent_spawn_count": collab_summary["subagent_spawn_count"],
        "subagent_wait_count": collab_summary["subagent_wait_count"],
        "subagents": collab_summary["subagents"],
        "token_usage": token_usage,
    }


def update_rollout_cache(
    sessions_root: Path, cache_path: Path
) -> dict[str, dict[str, Any]]:
    """Incrementally scan `sessions_root` and update `cache_path`.

    Only parses rollout files whose `(mtime_ns, size)` changed since the last scan.
    Returns a map of `rollout_path_str -> parsed_session_dict`.
    """
    existing = read_json(cache_path)
    cached_files: dict[str, dict[str, Any]] = {}
    if (
        isinstance(existing, dict)
        and existing.get("schema_version") == CACHE_SCHEMA_VERSION
        and isinstance(existing.get("files"), dict)
    ):
        cached_files = existing["files"]

    updated_files: dict[str, dict[str, Any]] = {}
    if sessions_root.is_dir():
        for rollout in sorted(sessions_root.rglob("rollout-*.jsonl")):
            if not rollout.is_file():
                continue
            try:
                stat = rollout.stat()
            except OSError:
                continue
            key = str(rollout.resolve())
            prev = cached_files.get(key)
            if (
                isinstance(prev, dict)
                and prev.get("mtime_ns") == stat.st_mtime_ns
                and prev.get("size") == stat.st_size
                and isinstance(prev.get("data"), dict)
            ):
                updated_files[key] = prev
            else:
                parsed = parse_session_rollout(rollout)
                updated_files[key] = {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "data": parsed,
                }

    write_json(
        cache_path,
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "updated_at": now(),
            "files": updated_files,
        },
    )
    return {key: entry["data"] for key, entry in updated_files.items()}


def discover_project_inventory(
    project_root: Path, evals_root: Path | None = None
) -> dict[str, list[str]]:
    """Return the project's installed skill names and `.codex/agents` profile names."""
    skill_dirs = [project_root / ".agents" / "skills"]
    if evals_root is not None:
        sibling_skills = evals_root.resolve().parent / "skills"
        if sibling_skills not in skill_dirs:
            skill_dirs.append(sibling_skills)

    installed_skills_set: set[str] = set()
    for skills_dir in skill_dirs:
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                if child.is_dir() and (child / "SKILL.md").is_file():
                    installed_skills_set.add(child.name)
    installed_skills = sorted(installed_skills_set)

    agents_dir = project_root / ".codex" / "agents"
    installed_agents: list[str] = []
    if agents_dir.is_dir():
        for child in sorted(agents_dir.iterdir()):
            if child.is_file() and child.suffix == ".toml":
                installed_agents.append(child.stem)

    return {
        "skills": installed_skills,
        "agent_profiles": installed_agents,
    }


def _add_token_dicts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return _finalize_token_usage(
        int(a.get("input_tokens", 0) or 0) + int(b.get("input_tokens", 0) or 0),
        int(a.get("cached_input_tokens", 0) or 0)
        + int(b.get("cached_input_tokens", 0) or 0),
        int(a.get("cache_write_input_tokens", 0) or 0)
        + int(b.get("cache_write_input_tokens", 0) or 0),
        int(a.get("output_tokens", 0) or 0) + int(b.get("output_tokens", 0) or 0),
        int(a.get("reasoning_output_tokens", 0) or 0)
        + int(b.get("reasoning_output_tokens", 0) or 0),
    )


def build_project_usage(
    rollouts_by_path: dict[str, dict[str, Any]],
    project_root: Path,
    evals_root: Path,
) -> dict[str, Any]:
    """Filter, link parent/subagent trees, and aggregate usage metrics for `project_root`."""
    inventory = discover_project_inventory(project_root, evals_root)
    installed_skills = set(inventory["skills"])
    installed_agents = set(inventory["agent_profiles"])

    by_tid: dict[str, dict[str, Any]] = {}
    children_by_parent: dict[str, list[str]] = defaultdict(list)

    for path_str, record in rollouts_by_path.items():
        tid = record.get("thread_id") or path_str
        entry = dict(record)
        entry["rollout_path"] = path_str
        by_tid[tid] = entry

    for tid, entry in by_tid.items():
        parent_tid = entry.get("parent_thread_id")
        if isinstance(parent_tid, str) and parent_tid:
            if tid not in children_by_parent[parent_tid]:
                children_by_parent[parent_tid].append(tid)
        for stub in entry.get("subagents", []):
            child_tid = stub.get("thread_id")
            if isinstance(child_tid, str) and child_tid:
                if child_tid not in children_by_parent[tid]:
                    children_by_parent[tid].append(child_tid)
                if child_tid in by_tid and not by_tid[child_tid].get("agent_role"):
                    if stub.get("agent_role"):
                        by_tid[child_tid]["agent_role"] = stub["agent_role"]

    # Identify top-level project work sessions (no parent_thread_id, cwd in project work tree).
    root_sessions: list[dict[str, Any]] = []
    for tid, entry in by_tid.items():
        if entry.get("parent_thread_id"):
            continue
        if is_project_work_cwd(entry.get("cwd"), project_root, evals_root):
            root_sessions.append(entry)

    # Sort sessions chronologically newest first.
    root_sessions.sort(
        key=lambda s: str(s.get("started_at") or ""),
        reverse=True,
    )

    sessions_summary: list[dict[str, Any]] = []
    total_tokens = _empty_token_usage()
    total_cost = 0.0
    priced_sessions = 0
    total_turns = 0
    total_commands = 0
    total_failed_commands = 0
    total_subagent_spawns = 0

    skill_stats: dict[str, dict[str, Any]] = {
        name: {
            "skill": name,
            "installed": True,
            "sessions_triggered": 0,
            "explicit_count": 0,
            "implicit_count": 0,
            "tokens": _empty_token_usage(),
            "est_cost": 0.0,
            "priced_count": 0,
        }
        for name in inventory["skills"]
    }

    agent_stats: dict[str, dict[str, Any]] = {
        role: {
            "role": role,
            "configured": True,
            "spawns": 0,
            "sessions_used": 0,
            "command_count": 0,
            "failed_command_count": 0,
            "tokens": _empty_token_usage(),
            "est_cost": 0.0,
            "priced_count": 0,
        }
        for role in inventory["agent_profiles"]
    }

    model_stats: dict[str, dict[str, Any]] = {}
    daily_stats: dict[str, dict[str, Any]] = {}

    for root in root_sessions:
        root_tid = root.get("thread_id") or root["rollout_path"]
        parent_tokens = dict(root.get("token_usage") or _empty_token_usage())
        parent_cost = estimate_api_cost(parent_tokens, root.get("model"))

        # Traverse descendant subagent tree
        subagent_records: list[dict[str, Any]] = []
        subagent_tokens = _empty_token_usage()
        subagent_cost_sum = 0.0
        subagent_cost_known = False

        stub_by_tid = {
            s.get("thread_id"): s
            for s in root.get("subagents", [])
            if isinstance(s, dict) and isinstance(s.get("thread_id"), str)
        }
        queue = list(children_by_parent.get(root_tid, []))
        visited = {root_tid}
        roles_in_session: set[str] = set()

        while queue:
            child_tid = queue.pop(0)
            if child_tid in visited:
                continue
            visited.add(child_tid)
            child = by_tid.get(child_tid)
            stub = stub_by_tid.get(child_tid, {})
            if child is not None:
                c_tokens = dict(child.get("token_usage") or _empty_token_usage())
                c_model = child.get("model") or root.get("model")
                c_cost = estimate_api_cost(c_tokens, c_model)
                c_role = (
                    child.get("agent_role")
                    or stub.get("agent_role")
                    or "default"
                )
                c_cmd = int(child.get("command_count", 0) or 0)
                c_fail = int(child.get("failed_command_count", 0) or 0)
                subagent_tokens = _add_token_dicts(subagent_tokens, c_tokens)
                if c_cost is not None:
                    subagent_cost_sum += c_cost
                    subagent_cost_known = True
                subagent_records.append(
                    {
                        "thread_id": child_tid,
                        "agent_role": c_role,
                        "agent_nickname": child.get("agent_nickname"),
                        "depth": child.get("depth") or 1,
                        "model": c_model,
                        "reasoning_effort": child.get("reasoning_effort"),
                        "command_count": c_cmd,
                        "failed_command_count": c_fail,
                        "token_usage": c_tokens,
                        "est_cost": c_cost,
                    }
                )
                for nested_stub in child.get("subagents", []):
                    if isinstance(nested_stub, dict) and isinstance(
                        nested_stub.get("thread_id"), str
                    ):
                        stub_by_tid.setdefault(
                            nested_stub["thread_id"], nested_stub
                        )
                queue.extend(children_by_parent.get(child_tid, []))
            else:
                c_role = stub.get("agent_role") or "default"
                c_tokens = _empty_token_usage()
                c_cost = None
                c_cmd = 0
                c_fail = 0
                subagent_records.append(
                    {
                        "thread_id": child_tid,
                        "agent_role": c_role,
                        "agent_nickname": None,
                        "depth": 1,
                        "model": None,
                        "reasoning_effort": None,
                        "command_count": 0,
                        "failed_command_count": 0,
                        "token_usage": c_tokens,
                        "est_cost": None,
                    }
                )

            if c_role not in agent_stats:
                agent_stats[c_role] = {
                    "role": c_role,
                    "configured": c_role in installed_agents,
                    "spawns": 0,
                    "sessions_used": 0,
                    "command_count": 0,
                    "failed_command_count": 0,
                    "tokens": _empty_token_usage(),
                    "est_cost": 0.0,
                    "priced_count": 0,
                }
            astat = agent_stats[c_role]
            astat["spawns"] += 1
            astat["command_count"] += c_cmd
            astat["failed_command_count"] += c_fail
            astat["tokens"] = _add_token_dicts(astat["tokens"], c_tokens)
            if c_cost is not None:
                astat["est_cost"] = round(astat["est_cost"] + c_cost, 6)
                astat["priced_count"] += 1
            roles_in_session.add(c_role)

        for r_name in roles_in_session:
            agent_stats[r_name]["sessions_used"] += 1

        combined_tokens = _add_token_dicts(parent_tokens, subagent_tokens)
        if parent_cost is not None or subagent_cost_known:
            session_cost: float | None = round(
                (parent_cost or 0.0) + subagent_cost_sum, 6
            )
        else:
            session_cost = None

        # Match explicit `$skill` mentions against known project skills too
        session_skills = dict(root.get("skill_invocations") or {})
        for tok in root.get("explicit_skill_tokens", []):
            if tok in installed_skills and tok not in session_skills:
                session_skills[tok] = "explicit"
        for sub in subagent_records:
            sub_entry = by_tid.get(sub["thread_id"], {})
            for sk, mode in (sub_entry.get("skill_invocations") or {}).items():
                session_skills.setdefault(sk, mode)

        for sk, mode in session_skills.items():
            if sk not in skill_stats:
                skill_stats[sk] = {
                    "skill": sk,
                    "installed": sk in installed_skills,
                    "sessions_triggered": 0,
                    "explicit_count": 0,
                    "implicit_count": 0,
                    "tokens": _empty_token_usage(),
                    "est_cost": 0.0,
                    "priced_count": 0,
                }
            sstat = skill_stats[sk]
            sstat["sessions_triggered"] += 1
            if mode == "explicit":
                sstat["explicit_count"] += 1
            else:
                sstat["implicit_count"] += 1
            sstat["tokens"] = _add_token_dicts(sstat["tokens"], combined_tokens)
            if session_cost is not None:
                sstat["est_cost"] = round(sstat["est_cost"] + session_cost, 6)
                sstat["priced_count"] += 1

        turns = int(root.get("turn_count", 1) or 1)
        cmds = int(root.get("command_count", 0) or 0) + sum(
            s["command_count"] for s in subagent_records
        )
        failed_cmds = int(root.get("failed_command_count", 0) or 0) + sum(
            s["failed_command_count"] for s in subagent_records
        )
        spawns = max(
            int(root.get("subagent_spawn_count", 0) or 0), len(subagent_records)
        )

        total_tokens = _add_token_dicts(total_tokens, combined_tokens)
        if session_cost is not None:
            total_cost = round(total_cost + session_cost, 6)
            priced_sessions += 1
        total_turns += turns
        total_commands += cmds
        total_failed_commands += failed_cmds
        total_subagent_spawns += spawns

        model_key = root.get("model") or "unknown"
        effort_key = root.get("reasoning_effort") or "default"
        me_key = f"{model_key} ({effort_key})"
        if me_key not in model_stats:
            model_stats[me_key] = {
                "model": model_key,
                "reasoning_effort": effort_key,
                "sessions": 0,
                "turns": 0,
                "tokens": _empty_token_usage(),
                "est_cost": 0.0,
                "priced_count": 0,
            }
        mstat = model_stats[me_key]
        mstat["sessions"] += 1
        mstat["turns"] += turns
        mstat["tokens"] = _add_token_dicts(mstat["tokens"], combined_tokens)
        if session_cost is not None:
            mstat["est_cost"] = round(mstat["est_cost"] + session_cost, 6)
            mstat["priced_count"] += 1

        started_str = str(root.get("started_at") or "")
        day_key = started_str[:10] if len(started_str) >= 10 else "unknown"
        if day_key not in daily_stats:
            daily_stats[day_key] = {
                "date": day_key,
                "sessions": 0,
                "turns": 0,
                "tokens": _empty_token_usage(),
                "est_cost": 0.0,
                "priced_count": 0,
            }
        dstat = daily_stats[day_key]
        dstat["sessions"] += 1
        dstat["turns"] += turns
        dstat["tokens"] = _add_token_dicts(dstat["tokens"], combined_tokens)
        if session_cost is not None:
            dstat["est_cost"] = round(dstat["est_cost"] + session_cost, 6)
            dstat["priced_count"] += 1

        sessions_summary.append(
            {
                "thread_id": root_tid,
                "started_at": started_str,
                "date": day_key,
                "cwd": root.get("cwd"),
                "model": root.get("model"),
                "reasoning_effort": root.get("reasoning_effort"),
                "turn_count": turns,
                "command_count": cmds,
                "failed_command_count": failed_cmds,
                "first_prompt": root.get("first_prompt") or "",
                "skills_used": sorted(session_skills.keys()),
                "skill_invocations": session_skills,
                "subagent_spawn_count": spawns,
                "subagents": subagent_records,
                "parent_token_usage": parent_tokens,
                "subagent_token_usage": subagent_tokens,
                "token_usage": combined_tokens,
                "est_cost": session_cost,
            }
        )

    total_input = total_tokens["input_tokens"]
    cache_hit_pct = (
        round(total_tokens["cached_input_tokens"] / total_input * 100, 1)
        if total_input > 0
        else 0.0
    )

    return {
        "project_name": project_root.name,
        "project_root": str(project_root),
        "generated_at": now(),
        "totals": {
            "session_count": len(sessions_summary),
            "turn_count": total_turns,
            "command_count": total_commands,
            "failed_command_count": total_failed_commands,
            "subagent_spawn_count": total_subagent_spawns,
            "cache_hit_pct": cache_hit_pct,
            "tokens": total_tokens,
            "est_cost": round(total_cost, 6) if priced_sessions > 0 else None,
            "priced_sessions": priced_sessions,
        },
        "skills": sorted(
            skill_stats.values(),
            key=lambda s: (-s["sessions_triggered"], not s["installed"], s["skill"]),
        ),
        "subagents": sorted(
            agent_stats.values(),
            key=lambda a: (-a["spawns"], not a["configured"], a["role"]),
        ),
        "models": sorted(
            model_stats.values(),
            key=lambda m: (-m["sessions"], m["model"]),
        ),
        "daily": sorted(
            daily_stats.values(),
            key=lambda d: d["date"],
            reverse=True,
        ),
        "sessions": sessions_summary,
    }


def render_usage_markdown(summary: dict[str, Any]) -> str:
    """Render the human-readable `_usage/report.md` for a project."""
    totals = summary["totals"]
    tok = totals["tokens"]
    total_cost_str = (
        format_cost(totals["est_cost"])
        if totals["session_count"] > 0
        else "$0.0000"
    )

    lines = [
        f"# Project usage report: {summary['project_name']}",
        "",
        f"**Project root:** `{summary['project_root']}` · **Updated:** `{summary['generated_at']}`",
        "",
        "## Overview",
        "",
        "| Sessions | Turns | Commands (failed) | Subagent spawns | Uncached input | Cached input (hit %) | Billed output | Est. API cost |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {totals['session_count']} | {totals['turn_count']} | "
            f"{totals['command_count']} ({totals['failed_command_count']}) | "
            f"{totals['subagent_spawn_count']} | "
            f"{tok['uncached_input_tokens']:,} | "
            f"{tok['cached_input_tokens']:,} ({totals['cache_hit_pct']}%) | "
            f"{tok['billed_output_tokens']:,} | {total_cost_str} |"
        ),
        "",
        f"> **Cost note:** `Est. API cost` is an API-equivalent estimate at public OpenAI list prices (as of {PRICING_AS_OF}, {PRICING_SOURCE}; long-context tier at {LONG_CONTEXT_THRESHOLD:,} input tokens). It is not actual subscription or credit spend.",
        "",
        "## Skill usage",
        "",
        "| Skill | Installed in project | Sessions triggered | Explicit / Implicit | Uncached input | Cached input | Billed output | Est. cost |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]

    if summary["skills"]:
        for sk in summary["skills"]:
            stok = sk["tokens"]
            cost_cell = (
                format_cost(sk["est_cost"])
                if sk["priced_count"] > 0
                else ("$0.0000" if sk["sessions_triggered"] == 0 else "n/a")
            )
            lines.append(
                f"| `{markdown_cell(sk['skill'])}` | "
                f"{'Yes' if sk['installed'] else 'No (external)'} | "
                f"{sk['sessions_triggered']} | "
                f"{sk['explicit_count']} / {sk['implicit_count']} | "
                f"{stok['uncached_input_tokens']:,} | "
                f"{stok['cached_input_tokens']:,} | "
                f"{stok['billed_output_tokens']:,} | {cost_cell} |"
            )
    else:
        lines.append("| (none) | — | 0 | 0 / 0 | 0 | 0 | 0 | $0.0000 |")

    lines.extend(
        [
            "",
            "## Subagent usage",
            "",
            "| Agent role | Configured in `.codex/agents` | Spawns | Sessions used | Commands (failed) | Uncached input | Cached input | Billed output | Est. cost |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if summary["subagents"]:
        for ag in summary["subagents"]:
            atok = ag["tokens"]
            cost_cell = (
                format_cost(ag["est_cost"])
                if ag["priced_count"] > 0
                else ("$0.0000" if ag["spawns"] == 0 else "n/a")
            )
            lines.append(
                f"| `{markdown_cell(ag['role'])}` | "
                f"{'Yes' if ag['configured'] else 'Built-in / ad-hoc'} | "
                f"{ag['spawns']} | {ag['sessions_used']} | "
                f"{ag['command_count']} ({ag['failed_command_count']}) | "
                f"{atok['uncached_input_tokens']:,} | "
                f"{atok['cached_input_tokens']:,} | "
                f"{atok['billed_output_tokens']:,} | {cost_cell} |"
            )
    else:
        lines.append("| (none) | — | 0 | 0 | 0 (0) | 0 | 0 | 0 | $0.0000 |")

    lines.extend(
        [
            "",
            "## Recent sessions",
            "",
            "| Started | Thread ID | Model (effort) | Turns | Skills | Subagents | Billed output | Est. cost | First prompt |",
            "|---|---|---|---:|---|---:|---:|---:|---|",
        ]
    )
    if summary["sessions"]:
        for sess in summary["sessions"][:50]:
            stok = sess["token_usage"]
            model_label = sess["model"] or "unknown"
            if sess.get("reasoning_effort"):
                model_label = f"{model_label} ({sess['reasoning_effort']})"
            skills_label = (
                ", ".join(f"`{s}`" for s in sess["skills_used"])
                if sess["skills_used"]
                else "—"
            )
            prompt_preview = markdown_cell(sess["first_prompt"][:80]) or "—"
            short_tid = str(sess["thread_id"])[:12]
            lines.append(
                f"| `{markdown_cell(sess['started_at'][:19])}` | `{short_tid}` | "
                f"`{markdown_cell(model_label)}` | {sess['turn_count']} | "
                f"{skills_label} | {sess['subagent_spawn_count']} | "
                f"{stok['billed_output_tokens']:,} | "
                f"{format_cost(sess['est_cost'])} | {prompt_preview} |"
            )
    else:
        lines.append("| — | — | — | 0 | — | 0 | 0 | $0.0000 | No project sessions recorded yet |")

    lines.append("")
    return "\n".join(lines)


def render_usage_html(summary: dict[str, Any]) -> str:
    """Render a self-contained interactive HTML dashboard (`_usage/report.html`)."""
    totals = summary["totals"]
    tok = totals["tokens"]
    total_cost_str = (
        format_cost(totals["est_cost"])
        if totals["session_count"] > 0
        else "$0.0000"
    )
    project_name = html.escape(summary["project_name"])
    project_root = html.escape(summary["project_root"])
    generated_at = html.escape(summary["generated_at"][:19].replace("T", " ") + " UTC")

    skill_rows_html: list[str] = []
    max_skill_sessions = max(
        (s["sessions_triggered"] for s in summary["skills"]), default=1
    ) or 1
    for sk in summary["skills"]:
        stok = sk["tokens"]
        cost_cell = (
            format_cost(sk["est_cost"])
            if sk["priced_count"] > 0
            else ("$0.0000" if sk["sessions_triggered"] == 0 else "n/a")
        )
        pct = int(round(sk["sessions_triggered"] / max_skill_sessions * 100))
        badge_cls = "badge-ok" if sk["sessions_triggered"] > 0 else "badge-muted"
        status_text = "Installed" if sk["installed"] else "External"
        unused_tag = (
            ' <span class="badge badge-warn">0 triggers</span>'
            if sk["installed"] and sk["sessions_triggered"] == 0
            else ""
        )
        skill_rows_html.append(
            f"<tr>"
            f"<td><code>{html.escape(sk['skill'])}</code>{unused_tag}</td>"
            f"<td><span class='badge {badge_cls}'>{status_text}</span></td>"
            f"<td class='num'><div class='bar-cell'><span>{sk['sessions_triggered']}</span>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{pct}%'></div></div></div></td>"
            f"<td class='num'>{sk['explicit_count']} / {sk['implicit_count']}</td>"
            f"<td class='num'>{stok['uncached_input_tokens']:,}</td>"
            f"<td class='num'>{stok['cached_input_tokens']:,}</td>"
            f"<td class='num'>{stok['billed_output_tokens']:,}</td>"
            f"<td class='num'>{html.escape(cost_cell)}</td>"
            f"</tr>"
        )
    if not skill_rows_html:
        skill_rows_html.append(
            "<tr><td colspan='8' class='empty'>No skills discovered in <code>.agents/skills/</code> or session traces.</td></tr>"
        )

    subagent_rows_html: list[str] = []
    for ag in summary["subagents"]:
        atok = ag["tokens"]
        cost_cell = (
            format_cost(ag["est_cost"])
            if ag["priced_count"] > 0
            else ("$0.0000" if ag["spawns"] == 0 else "n/a")
        )
        unused_tag = (
            ' <span class="badge badge-warn">0 spawns</span>'
            if ag["configured"] and ag["spawns"] == 0
            else ""
        )
        subagent_rows_html.append(
            f"<tr>"
            f"<td><code>{html.escape(ag['role'])}</code>{unused_tag}</td>"
            f"<td>{'Configured' if ag['configured'] else 'Built-in / ad-hoc'}</td>"
            f"<td class='num'>{ag['spawns']}</td>"
            f"<td class='num'>{ag['sessions_used']}</td>"
            f"<td class='num'>{ag['command_count']} ({ag['failed_command_count']})</td>"
            f"<td class='num'>{atok['uncached_input_tokens']:,}</td>"
            f"<td class='num'>{atok['cached_input_tokens']:,}</td>"
            f"<td class='num'>{atok['billed_output_tokens']:,}</td>"
            f"<td class='num'>{html.escape(cost_cell)}</td>"
            f"</tr>"
        )
    if not subagent_rows_html:
        subagent_rows_html.append(
            "<tr><td colspan='9' class='empty'>No subagent profiles or spawns recorded.</td></tr>"
        )

    daily_rows_html: list[str] = []
    max_daily_tokens = max(
        (
            d["tokens"]["input_tokens"] + d["tokens"]["billed_output_tokens"]
            for d in summary["daily"]
        ),
        default=1,
    ) or 1
    for d in summary["daily"]:
        dtok = d["tokens"]
        total_day_tok = dtok["input_tokens"] + dtok["billed_output_tokens"]
        pct = int(round(total_day_tok / max_daily_tokens * 100))
        cost_cell = (
            format_cost(d["est_cost"]) if d["priced_count"] > 0 else "n/a"
        )
        daily_rows_html.append(
            f"<tr>"
            f"<td><code>{html.escape(d['date'])}</code></td>"
            f"<td class='num'>{d['sessions']}</td>"
            f"<td class='num'>{d['turns']}</td>"
            f"<td class='num'>{dtok['uncached_input_tokens']:,}</td>"
            f"<td class='num'>{dtok['cached_input_tokens']:,}</td>"
            f"<td class='num'><div class='bar-cell'><span>{dtok['billed_output_tokens']:,}</span>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{pct}%'></div></div></div></td>"
            f"<td class='num'>{html.escape(cost_cell)}</td>"
            f"</tr>"
        )
    if not daily_rows_html:
        daily_rows_html.append(
            "<tr><td colspan='7' class='empty'>No daily activity yet.</td></tr>"
        )

    session_rows_html: list[str] = []
    all_skill_options = sorted({s["skill"] for s in summary["skills"]})
    all_model_options = sorted(
        {s["model"] or "unknown" for s in summary["sessions"]}
    )

    for idx, sess in enumerate(summary["sessions"]):
        stok = sess["token_usage"]
        model_name = sess["model"] or "unknown"
        model_label = (
            f"{model_name} ({sess['reasoning_effort']})"
            if sess.get("reasoning_effort")
            else model_name
        )
        skills_csv = ",".join(sess["skills_used"])
        skills_badges = (
            " ".join(
                f"<span class='badge badge-skill'>{html.escape(s)}</span>"
                for s in sess["skills_used"]
            )
            if sess["skills_used"]
            else "<span class='muted'>—</span>"
        )
        prompt_text = html.escape(sess["first_prompt"] or "—")
        started_short = html.escape(sess["started_at"][:19].replace("T", " "))
        short_tid = html.escape(str(sess["thread_id"])[:12])
        cost_str = html.escape(format_cost(sess["est_cost"]))

        sub_Toggle = (
            f"<button class='btn-expand' onclick='toggleSub({idx})'>"
            f"{sess['subagent_spawn_count']} subagent(s) ▾</button>"
            if sess["subagents"]
            else str(sess["subagent_spawn_count"])
        )

        search_blob = html.escape(
            f"{sess['thread_id']} {model_label} {skills_csv} {sess['first_prompt']}".lower()
        )
        session_rows_html.append(
            f"<tr class='session-row' data-skills='{html.escape(skills_csv)}' "
            f"data-model='{html.escape(model_name)}' data-search='{search_blob}'>"
            f"<td><code>{started_short}</code></td>"
            f"<td title='{html.escape(str(sess['thread_id']))}'><code>{short_tid}</code></td>"
            f"<td><code>{html.escape(model_label)}</code></td>"
            f"<td class='num'>{sess['turn_count']}</td>"
            f"<td class='num'>{sess['command_count']} ({sess['failed_command_count']})</td>"
            f"<td>{skills_badges}</td>"
            f"<td class='num'>{sub_Toggle}</td>"
            f"<td class='num'>{stok['uncached_input_tokens']:,}</td>"
            f"<td class='num'>{stok['cached_input_tokens']:,}</td>"
            f"<td class='num'>{stok['billed_output_tokens']:,}</td>"
            f"<td class='num'>{cost_str}</td>"
            f"<td class='prompt-cell'>{prompt_text}</td>"
            f"</tr>"
        )
        if sess["subagents"]:
            sub_items: list[str] = []
            for sub in sess["subagents"]:
                sub_tok = sub["token_usage"]
                sub_model = sub.get("model") or "—"
                if sub.get("reasoning_effort"):
                    sub_model = f"{sub_model} ({sub['reasoning_effort']})"
                nick = (
                    f" ({html.escape(sub['agent_nickname'])})"
                    if sub.get("agent_nickname")
                    else ""
                )
                sub_items.append(
                    f"<tr>"
                    f"<td><code>depth {sub.get('depth', 1)}</code></td>"
                    f"<td><code>{html.escape(str(sub.get('agent_role') or 'default'))}</code>{nick}</td>"
                    f"<td><code>{html.escape(str(sub['thread_id'])[:12])}</code></td>"
                    f"<td><code>{html.escape(sub_model)}</code></td>"
                    f"<td class='num'>{sub['command_count']} ({sub['failed_command_count']})</td>"
                    f"<td class='num'>{sub_tok['uncached_input_tokens']:,}</td>"
                    f"<td class='num'>{sub_tok['cached_input_tokens']:,}</td>"
                    f"<td class='num'>{sub_tok['billed_output_tokens']:,}</td>"
                    f"<td class='num'>{html.escape(format_cost(sub.get('est_cost')))}</td>"
                    f"</tr>"
                )
            session_rows_html.append(
                f"<tr id='sub-{idx}' class='subagent-drawer' style='display:none'>"
                f"<td colspan='12'>"
                f"<div class='subagent-box'>"
                f"<strong>Spawned subagents (parent tokens: "
                f"{sess['parent_token_usage']['billed_output_tokens']:,} billed out, "
                f"subagent tokens: {sess['subagent_token_usage']['billed_output_tokens']:,} billed out)</strong>"
                f"<table class='sub-table'>"
                f"<thead><tr><th>Depth</th><th>Role</th><th>Thread ID</th><th>Model</th>"
                f"<th class='num'>Cmds (fail)</th><th class='num'>Uncached in</th>"
                f"<th class='num'>Cached in</th><th class='num'>Billed out</th><th class='num'>Est. cost</th></tr></thead>"
                f"<tbody>{''.join(sub_items)}</tbody></table></div></td></tr>"
            )

    if not session_rows_html:
        session_rows_html.append(
            "<tr><td colspan='12' class='empty'>No Codex sessions recorded for this project yet.</td></tr>"
        )

    skill_opt_html = "".join(
        f"<option value='{html.escape(s)}'>{html.escape(s)}</option>"
        for s in all_skill_options
    )
    model_opt_html = "".join(
        f"<option value='{html.escape(m)}'>{html.escape(m)}</option>"
        for m in all_model_options
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex Usage — {project_name}</title>
<style>
  :root {{
    --bg: #0f172a;
    --panel: #1e293b;
    --panel-alt: #162032;
    --border: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --ok: #22c55e;
    --warn: #f59e0b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 24px 32px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    margin-bottom: 24px;
  }}
  h1 {{ margin: 0; font-size: 1.5rem; }}
  h2 {{ margin: 28px 0 12px; font-size: 1.15rem; color: var(--text); }}
  .meta {{ color: var(--muted); font-size: 0.875rem; }}
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }}
  .card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
  }}
  .card-label {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }}
  .card-val {{ font-size: 1.45rem; font-weight: 600; margin-top: 4px; }}
  .card-sub {{ color: var(--muted); font-size: 0.8rem; margin-top: 2px; }}
  .note {{
    background: var(--panel-alt);
    border-left: 3px solid var(--accent);
    padding: 10px 14px;
    font-size: 0.85rem;
    color: var(--muted);
    margin-bottom: 24px;
    border-radius: 4px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    font-size: 0.875rem;
  }}
  th, td {{
    padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    vertical-align: top;
  }}
  th {{
    background: var(--panel-alt);
    color: var(--muted);
    font-weight: 600;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }}
  th.num, td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  tr:last-child td {{ border-bottom: none; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
    background: rgba(148, 163, 184, 0.12);
    padding: 2px 5px;
    border-radius: 4px;
  }}
  .badge {{
    display: inline-block;
    padding: 1px 7px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 500;
  }}
  .badge-ok {{ background: rgba(34, 197, 94, 0.16); color: #4ade80; }}
  .badge-warn {{ background: rgba(245, 158, 11, 0.18); color: #fbbf24; }}
  .badge-muted {{ background: rgba(148, 163, 184, 0.15); color: var(--muted); }}
  .badge-skill {{ background: rgba(56, 189, 248, 0.16); color: var(--accent); margin-right: 4px; }}
  .bar-cell {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; }}
  .bar-track {{ width: 64px; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }}
  .bar-fill {{ height: 100%; background: var(--accent); }}
  .filters {{
    display: flex;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }}
  .filters input, .filters select {{
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 11px;
    font-size: 0.875rem;
  }}
  .filters input {{ min-width: 240px; }}
  .btn-expand {{
    background: rgba(56, 189, 248, 0.14);
    color: var(--accent);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 4px;
    padding: 2px 8px;
    cursor: pointer;
    font-size: 0.78rem;
  }}
  .subagent-box {{
    background: var(--panel-alt);
    padding: 10px 14px;
    border-radius: 6px;
  }}
  .sub-table {{ margin-top: 8px; }}
  .prompt-cell {{ max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }}
  .empty {{ text-align: center; color: var(--muted); padding: 20px; }}
  .muted {{ color: var(--muted); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Codex Project Usage: {project_name}</h1>
    <div class="meta">Project root: <code>{project_root}</code></div>
  </div>
  <div class="meta">Updated: <code>{generated_at}</code></div>
</header>

<div class="cards">
  <div class="card">
    <div class="card-label">Real Sessions</div>
    <div class="card-val">{totals['session_count']}</div>
    <div class="card-sub">{totals['turn_count']} total turns</div>
  </div>
  <div class="card">
    <div class="card-label">Uncached Input</div>
    <div class="card-val">{tok['uncached_input_tokens']:,}</div>
    <div class="card-sub">Billed at full input rate</div>
  </div>
  <div class="card">
    <div class="card-label">Cached Input</div>
    <div class="card-val">{tok['cached_input_tokens']:,}</div>
    <div class="card-sub">{totals['cache_hit_pct']}% cache hit rate</div>
  </div>
  <div class="card">
    <div class="card-label">Billed Output</div>
    <div class="card-val">{tok['billed_output_tokens']:,}</div>
    <div class="card-sub">{tok['output_tokens']:,} out + {tok['reasoning_output_tokens']:,} reasoning</div>
  </div>
  <div class="card">
    <div class="card-label">Subagent Spawns</div>
    <div class="card-val">{totals['subagent_spawn_count']}</div>
    <div class="card-sub">{totals['command_count']} cmds ({totals['failed_command_count']} failed)</div>
  </div>
  <div class="card">
    <div class="card-label">Est. API Cost</div>
    <div class="card-val">{html.escape(total_cost_str)}</div>
    <div class="card-sub">API-equivalent estimate</div>
  </div>
</div>

<div class="note">
  <strong>Zero-token telemetry:</strong> Computed directly from local <code>~/.codex/sessions/</code> rollouts scoped to this project root (excluding synthetic <code>eval.py run</code> sandbox trials). <code>Est. API cost</code> uses public OpenAI API list prices as of {html.escape(PRICING_AS_OF)} and is not actual subscription spend.
</div>

<h2>Skill Usage</h2>
<table>
  <thead>
    <tr>
      <th>Skill</th>
      <th>Status</th>
      <th class="num">Sessions Triggered</th>
      <th class="num">Explicit / Implicit</th>
      <th class="num">Uncached Input</th>
      <th class="num">Cached Input</th>
      <th class="num">Billed Output</th>
      <th class="num">Est. Cost</th>
    </tr>
  </thead>
  <tbody>
    {''.join(skill_rows_html)}
  </tbody>
</table>

<h2>Subagent Usage</h2>
<table>
  <thead>
    <tr>
      <th>Agent Role</th>
      <th>Source</th>
      <th class="num">Spawns</th>
      <th class="num">Sessions Used</th>
      <th class="num">Commands (Failed)</th>
      <th class="num">Uncached Input</th>
      <th class="num">Cached Input</th>
      <th class="num">Billed Output</th>
      <th class="num">Est. Cost</th>
    </tr>
  </thead>
  <tbody>
    {''.join(subagent_rows_html)}
  </tbody>
</table>

<h2>Daily Activity</h2>
<table>
  <thead>
    <tr>
      <th>Date</th>
      <th class="num">Sessions</th>
      <th class="num">Turns</th>
      <th class="num">Uncached Input</th>
      <th class="num">Cached Input</th>
      <th class="num">Billed Output</th>
      <th class="num">Est. Cost</th>
    </tr>
  </thead>
  <tbody>
    {''.join(daily_rows_html)}
  </tbody>
</table>

<h2>Session History</h2>
<div class="filters">
  <input type="search" id="searchInput" placeholder="Search prompt, thread ID, skill, model..." oninput="filterSessions()">
  <select id="skillFilter" onchange="filterSessions()">
    <option value="">All Skills</option>
    <option value="__none__">No Skill Used</option>
    {skill_opt_html}
  </select>
  <select id="modelFilter" onchange="filterSessions()">
    <option value="">All Models</option>
    {model_opt_html}
  </select>
</div>
<table>
  <thead>
    <tr>
      <th>Started (UTC)</th>
      <th>Thread ID</th>
      <th>Model (Effort)</th>
      <th class="num">Turns</th>
      <th class="num">Cmds (Fail)</th>
      <th>Skills</th>
      <th class="num">Subagents</th>
      <th class="num">Uncached In</th>
      <th class="num">Cached In</th>
      <th class="num">Billed Out</th>
      <th class="num">Est. Cost</th>
      <th>First Prompt</th>
    </tr>
  </thead>
  <tbody id="sessionTableBody">
    {''.join(session_rows_html)}
  </tbody>
</table>

<script>
function toggleSub(idx) {{
  const row = document.getElementById('sub-' + idx);
  if (!row) return;
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}}
function filterSessions() {{
  const q = (document.getElementById('searchInput').value || '').toLowerCase();
  const sk = document.getElementById('skillFilter').value;
  const md = document.getElementById('modelFilter').value;
  const rows = document.querySelectorAll('.session-row');
  rows.forEach((row) => {{
    const rowSkills = (row.getAttribute('data-skills') || '').split(',').filter(Boolean);
    const rowModel = row.getAttribute('data-model') || '';
    const rowSearch = row.getAttribute('data-search') || '';
    let matchSkill = true;
    if (sk === '__none__') matchSkill = rowSkills.length === 0;
    else if (sk) matchSkill = rowSkills.includes(sk);
    const matchModel = !md || rowModel === md;
    const matchSearch = !q || rowSearch.includes(q);
    const show = matchSkill && matchModel && matchSearch;
    row.style.display = show ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


def generate_usage_report(
    evals_root: Path,
    project_root: Path | None = None,
    sessions_root: Path | None = None,
    notify_payload: str | None = None,
) -> dict[str, Any]:
    """Incrementally scan Codex rollouts and write `_usage/report.html`, `report.md`, and `summary.json`."""
    resolved_project = (
        project_root.resolve()
        if project_root is not None
        else resolve_project_root(evals_root)
    )
    resolved_sessions = (
        sessions_root.resolve()
        if sessions_root is not None
        else default_sessions_root()
    )

    if notify_payload:
        try:
            payload = json.loads(notify_payload)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            turn_cwd = payload.get("cwd")
            if isinstance(turn_cwd, str) and turn_cwd:
                if is_eval_run_cwd(turn_cwd, evals_root):
                    return {"skipped": True, "reason": "eval_trial"}
                if not _is_subpath(Path(turn_cwd), resolved_project):
                    return {"skipped": True, "reason": "outside_project"}

    out_dir = usage_output_dir(evals_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "cache.json"
    rollouts = update_rollout_cache(resolved_sessions, cache_path)
    summary = build_project_usage(rollouts, resolved_project, evals_root)

    report_md = out_dir / "report.md"
    report_html = out_dir / "report.html"
    summary_json = out_dir / "summary.json"

    report_md.write_text(render_usage_markdown(summary), encoding="utf-8")
    report_html.write_text(render_usage_html(summary), encoding="utf-8")
    write_json(summary_json, summary)

    totals = summary["totals"]
    return {
        "skipped": False,
        "summary": summary,
        "session_count": totals["session_count"],
        "subagent_count": totals["subagent_spawn_count"],
        "report_md": report_md,
        "report_html": report_html,
        "summary_json": summary_json,
        "markdown_path": report_md,
        "html_path": report_html,
        "summary_path": summary_json,
        "cache_path": cache_path,
    }


def notify_command_for_project(evals_root: Path, project_root: Path) -> list[str]:
    """Build the project-relative `notify` command array for `.codex/config.toml`."""
    eval_script = evals_root.resolve() / "eval.py"
    try:
        rel = eval_script.relative_to(project_root.resolve())
        script_arg = rel.as_posix()
    except ValueError:
        script_arg = str(eval_script)
    return ["python3", script_arg, "usage", "--quiet"]


def _validate_toml_if_available(content: str) -> dict[str, Any] | None:
    if tomllib is not None:
        return tomllib.loads(content)
    return None


def _parse_existing_top_level_notify(lines: list[str], first_table_idx: int) -> list[str] | None:
    for i in range(first_table_idx):
        m = re.match(r"^\s*notify\s*=\s*(\[.*\])\s*(?:#.*)?$", lines[i])
        if m:
            try:
                val = json.loads(m.group(1))
                if isinstance(val, list):
                    return val
            except json.JSONDecodeError:
                return None
    return None


def configure_codex_notify(
    config_path: Path, notify_command: list[str] | None = None
) -> str:
    """Safely configure `notify = [...]` at the top level of `.codex/config.toml`.

    Preserves existing settings, comments, and `[table]` sections. Validates the
    resulting file with `tomllib.loads` when available. Returns `"created"`,
    `"updated"`, or `"unchanged"`.
    """
    if notify_command is None:
        notify_command = ["python3", ".agents/evals/eval.py", "usage", "--quiet"]
    notify_line = f"notify = {json.dumps(notify_command)}"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "# Auto-updated per-project Codex session usage report\n"
            f"{notify_line}\n"
        )
        _validate_toml_if_available(content)
        config_path.write_text(content, encoding="utf-8")
        return "created"

    existing_text = config_path.read_text(encoding="utf-8")
    parsed = _validate_toml_if_available(existing_text)
    lines = existing_text.splitlines()
    first_table_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"^\s*\[", line)
        ),
        len(lines),
    )
    existing_notify = (
        parsed.get("notify")
        if isinstance(parsed, dict)
        else _parse_existing_top_level_notify(lines, first_table_idx)
    )
    if existing_notify == notify_command:
        return "unchanged"

    # Check if top-level `notify = ...` already exists before the first table header
    replaced = False
    for i in range(first_table_idx):
        if re.match(r"^\s*notify\s*=", lines[i]):
            lines[i] = notify_line
            replaced = True
            break

    if not replaced:
        insert_idx = 0
        while insert_idx < first_table_idx and (
            not lines[insert_idx].strip()
            or lines[insert_idx].lstrip().startswith("#")
        ):
            insert_idx += 1
        lines.insert(insert_idx, notify_line)
        if insert_idx + 1 < len(lines) and lines[insert_idx + 1].strip().startswith("["):
            lines.insert(insert_idx + 1, "")

    new_content = "\n".join(lines) + "\n"
    _validate_toml_if_available(new_content)
    config_path.write_text(new_content, encoding="utf-8")
    return "updated"


def setup_project_usage(
    evals_root: Path,
    project_root: Path | None = None,
    sessions_root: Path | None = None,
    configure_notify: bool = True,
) -> dict[str, Any]:
    """Configure `.codex/config.toml` notify hook and build initial usage reports."""
    resolved_project = (
        project_root.resolve()
        if project_root is not None
        else resolve_project_root(evals_root)
    )
    config_path: Path | None = None
    notify_cmd: list[str] | None = None
    hook_status = "skipped"
    if configure_notify:
        config_path = resolved_project / ".codex" / "config.toml"
        notify_cmd = notify_command_for_project(evals_root, resolved_project)
        hook_status = configure_codex_notify(config_path, notify_cmd)
    report_info = generate_usage_report(
        evals_root=evals_root,
        project_root=resolved_project,
        sessions_root=sessions_root,
    )
    return {
        "project_root": resolved_project,
        "config_path": config_path,
        "notify_command": notify_cmd,
        "hook_status": hook_status,
        "notify_status": hook_status,
        **report_info,
    }
