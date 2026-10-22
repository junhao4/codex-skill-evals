"""Per-project Codex session usage tracking, incremental rollout cache, and HTML/Markdown reports."""

from __future__ import annotations

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

from _lib.common import markdown_cell, now, read_json, write_json
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


def parse_eval_run_cwd(
    cwd: str | Path | None, evals_root: Path
) -> dict[str, Any] | None:
    """Extract skill, experiment, test_case, configuration, and trial from an eval trial `cwd`."""
    if not cwd:
        return None
    cwd_path = Path(cwd)
    try:
        rel = cwd_path.resolve().relative_to(evals_root.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if "runs" not in parts:
        return None
    runs_idx = parts.index("runs")
    eval_skill = parts[0] if runs_idx >= 1 else "unknown"
    experiment = parts[runs_idx + 1] if len(parts) > runs_idx + 1 else "unknown"
    test_case = "unknown"
    configuration = "unknown"
    trial = "unknown"
    trial_dir: Path | None = None
    if len(parts) > runs_idx + 2 and parts[runs_idx + 2] == "trials":
        if len(parts) > runs_idx + 3:
            test_case = parts[runs_idx + 3]
        if len(parts) > runs_idx + 4:
            configuration = parts[runs_idx + 4]
        if len(parts) > runs_idx + 5:
            trial = parts[runs_idx + 5]
            trial_dir = evals_root.resolve().joinpath(*parts[: runs_idx + 6])
    return {
        "eval_skill": eval_skill,
        "experiment": experiment,
        "test_case": test_case,
        "configuration": configuration,
        "trial": trial,
        "trial_dir": str(trial_dir) if trial_dir is not None else None,
    }


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
        "commands": commands[:200],
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
    If `sessions_root` is not accessible (e.g. inside a restricted sandbox),
    preserves existing cached rollout entries rather than wiping `cache_path`.
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
    elif cached_files:
        updated_files = dict(cached_files)

    write_json(
        cache_path,
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "updated_at": now(),
            "files": updated_files,
        },
    )
    return {
        key: entry["data"]
        for key, entry in updated_files.items()
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict)
    }


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


def _traverse_subagent_tree(
    root: dict[str, Any],
    root_tid: str,
    by_tid: dict[str, dict[str, Any]],
    children_by_parent: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], dict[str, int], float, bool]:
    """Traverse all descendant subagent sessions spawned by `root_tid`."""
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
    return subagent_records, subagent_tokens, subagent_cost_sum, subagent_cost_known


def read_trial_grades(trial_dir: Path) -> dict[str, Any]:
    """Read a trial's deterministic + semantic grades off disk (READ-ONLY, pure).

    Returns ``{"deterministic": <grade|None>, "semantic": <grade|None>}`` where
    each grade is a plain dict suitable for rendering. Missing or malformed
    grade files degrade gracefully to ``None`` (never raises).

    - deterministic: from ``grades/deterministic/result.json`` -> its inner
      ``result`` ({overall_pass, score, verdict, checks[], limitations}).
    - semantic: from ``grades/semantic/result.json`` -> consensus ``result``
      flattened with quorum/deciding/abstaining counts and a trimmed per-judge
      list (each judge's score, verdict, and checks).
    """
    trial_dir = Path(trial_dir)
    deterministic: dict[str, Any] | None = None
    semantic: dict[str, Any] | None = None

    det_raw = read_json(trial_dir / "grades" / "deterministic" / "result.json")
    if isinstance(det_raw, dict):
        inner = det_raw.get("result")
        if isinstance(inner, dict):
            deterministic = {
                "overall_pass": inner.get("overall_pass"),
                "score": inner.get("score"),
                "verdict": inner.get("verdict"),
                "checks": [c for c in inner.get("checks", []) if isinstance(c, dict)],
                "limitations": inner.get("limitations", []),
                "status": det_raw.get("status"),
            }

    sem_raw = read_json(trial_dir / "grades" / "semantic" / "result.json")
    if isinstance(sem_raw, dict):
        inner = sem_raw.get("result")
        if isinstance(inner, dict):
            judges: list[dict[str, Any]] = []
            for j in sem_raw.get("judges", []):
                if not isinstance(j, dict):
                    continue
                j_result = j.get("result") or {}
                if not isinstance(j_result, dict):
                    j_result = {}
                judges.append(
                    {
                        "judge": j.get("judge"),
                        "status": j.get("status"),
                        "score": j_result.get("score"),
                        "verdict": j_result.get("verdict"),
                        "checks": [
                            c for c in j_result.get("checks", []) if isinstance(c, dict)
                        ],
                    }
                )
            semantic = {
                "overall_pass": inner.get("overall_pass"),
                "score": inner.get("score"),
                "verdict": inner.get("verdict"),
                "checks": [c for c in inner.get("checks", []) if isinstance(c, dict)],
                "limitations": inner.get("limitations", []),
                "status": sem_raw.get("status"),
                "quorum": sem_raw.get("quorum"),
                "deciding_judges": sem_raw.get("deciding_judges"),
                "abstaining_judges": sem_raw.get("abstaining_judges"),
                "judges": judges,
            }

    return {"deterministic": deterministic, "semantic": semantic}


def _trial_headline_score(trial: dict[str, Any]) -> float | None:
    """A trial's headline score = min(det, sem) when both present (harness rule)."""
    grades = trial.get("grades") or {}
    det = grades.get("deterministic") or {}
    sem = grades.get("semantic") or {}
    scores = [
        s for s in (det.get("score"), sem.get("score")) if isinstance(s, (int, float))
    ]
    return float(min(scores)) if scores else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def derive_experiment_comparison(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive a per-test-case baseline vs with-skill comparison from trial data.

    Pure (no I/O). For each test case, computes the mean headline score, mean
    billed output tokens, and mean est. cost over its baseline trials and over
    its with-skill trials, plus signed deltas (with-skill relative to baseline).
    A config with no trials yields ``None`` for that side and ``"n/a"`` deltas.
    Score/pass are higher-is-better; tokens and cost are lower-is-better.
    """
    from _lib.pricing import format_delta

    by_case: dict[str, dict[str, list[dict[str, Any]]]] = {}
    order: list[str] = []
    for t in trials:
        case = str(t.get("test_case"))
        cfg = t.get("configuration")
        if case not in by_case:
            by_case[case] = {"baseline": [], "with-skill": []}
            order.append(case)
        if cfg in by_case[case]:
            by_case[case][cfg].append(t)

    def _cell(group: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not group:
            return None
        scores = [s for s in (_trial_headline_score(t) for t in group) if s is not None]
        billed = [
            t["token_usage"]["billed_output_tokens"]
            for t in group
            if t.get("token_usage")
        ]
        costs = [t["est_cost"] for t in group if t.get("est_cost") is not None]
        return {
            "n": len(group),
            "mean_score": _mean(scores),
            "mean_billed_output": _mean([float(b) for b in billed]),
            "mean_cost": round(sum(costs) / len(costs), 6) if costs else None,
        }

    comparison: list[dict[str, Any]] = []
    for case in order:
        base = _cell(by_case[case]["baseline"])
        skill = _cell(by_case[case]["with-skill"])

        def _delta(key: str, higher_is_better: bool) -> tuple[str, str]:
            o = base.get(key) if base else None
            n = skill.get(key) if skill else None
            if o is None or n is None:
                return "n/a", "unknown"
            return format_delta(o, n), _axis_verdict_local(o, n, higher_is_better)

        score_delta, score_verdict = _delta("mean_score", True)
        billed_delta, billed_verdict = _delta("mean_billed_output", False)
        cost_delta, cost_verdict = _delta("mean_cost", False)
        comparison.append(
            {
                "test_case": case,
                "baseline": base,
                "with_skill": skill,
                "score_delta": score_delta,
                "score_verdict": score_verdict,
                "billed_output_delta": billed_delta,
                "billed_output_verdict": billed_verdict,
                "cost_delta": cost_delta,
                "cost_verdict": cost_verdict,
            }
        )
    return comparison


def _axis_verdict_local(old, new, higher_is_better: bool) -> str:
    """improved / regressed / unchanged for a numeric change. Pure."""
    if old is None or new is None:
        return "unknown"
    if new == old:
        return "unchanged"
    better = new > old if higher_is_better else new < old
    return "improved" if better else "regressed"


def summarize_experiment_outcome(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up an experiment's outcome + scores from per-trial grades. Pure.

    - mean_score: mean of per-trial headline scores (min(det, sem) when both ran).
    - mean_det / mean_sem: mean of the respective grader scores where present.
    - trials_passed / trials_total: headline score >= PASS_THRESHOLD counts.
    - outcome: "PASS" when every graded trial passed, "FAIL" when at least one
      graded trial scored below threshold, "UNKNOWN" when no trial was graded.
    """
    from _lib.grading import PASS_THRESHOLD

    headlines = [s for s in (_trial_headline_score(t) for t in trials) if s is not None]
    det_scores: list[float] = []
    sem_scores: list[float] = []
    for t in trials:
        grades = t.get("grades") or {}
        det = grades.get("deterministic") or {}
        sem = grades.get("semantic") or {}
        if isinstance(det.get("score"), (int, float)):
            det_scores.append(float(det["score"]))
        if isinstance(sem.get("score"), (int, float)):
            sem_scores.append(float(sem["score"]))

    if not headlines:
        outcome = "UNKNOWN"
    elif all(s >= PASS_THRESHOLD for s in headlines):
        outcome = "PASS"
    else:
        outcome = "FAIL"

    return {
        "outcome": outcome,
        "mean_score": _mean(headlines),
        "mean_det": _mean(det_scores),
        "mean_sem": _mean(sem_scores),
        "trials_total": len(headlines),
        "trials_passed": sum(1 for s in headlines if s >= PASS_THRESHOLD),
    }


def _collect_harness_eval_usage(
    by_tid: dict[str, dict[str, Any]],
    children_by_parent: dict[str, list[str]],
    evals_root: Path,
    installed_agents: set[str],
) -> dict[str, Any]:
    """Collect and aggregate Harness Eval Calls (worker trials, eval subagents, and semantic judges)."""
    eval_root_sessions: list[dict[str, Any]] = []
    seen_tids: set[str] = set()
    seen_trial_dirs: set[str] = set()

    for tid, entry in by_tid.items():
        if entry.get("parent_thread_id"):
            continue
        if is_eval_run_cwd(entry.get("cwd"), evals_root):
            eval_root_sessions.append(entry)
            seen_tids.add(tid)
            parsed_cwd = parse_eval_run_cwd(entry.get("cwd"), evals_root)
            if parsed_cwd and parsed_cwd.get("trial_dir"):
                seen_trial_dirs.add(str(parsed_cwd["trial_dir"]))

    # Also discover any local worker/trace.jsonl on disk not already in ~/.codex/sessions
    if evals_root.is_dir():
        for worker_trace in sorted(
            evals_root.glob("*/runs/*/trials/*/*/*/worker/trace.jsonl")
        ):
            trial_dir = worker_trace.parent.parent.resolve()
            trial_dir_str = str(trial_dir)
            if trial_dir_str in seen_trial_dirs:
                continue
            parsed_worker = parse_session_rollout(worker_trace)
            tid = parsed_worker.get("thread_id") or str(worker_trace)
            if tid in seen_tids:
                continue
            run_meta = read_json(trial_dir / "run.json")
            if isinstance(run_meta, dict):
                if not parsed_worker.get("model") and isinstance(
                    run_meta.get("resolved_model"), str
                ):
                    parsed_worker["model"] = run_meta["resolved_model"]
                if not parsed_worker.get("reasoning_effort") and isinstance(
                    run_meta.get("resolved_reasoning_effort"), str
                ):
                    parsed_worker["reasoning_effort"] = run_meta[
                        "resolved_reasoning_effort"
                    ]
                if isinstance(run_meta.get("started_at"), str):
                    parsed_worker["started_at"] = run_meta["started_at"]
            parsed_worker["cwd"] = str(trial_dir / "workspace")
            parsed_worker["rollout_path"] = str(worker_trace)
            eval_root_sessions.append(parsed_worker)
            seen_tids.add(tid)
            seen_trial_dirs.add(trial_dir_str)

    # Scan semantic judge traces on disk (`grades/semantic/judge-*/trace.jsonl`)
    judges_by_trial_dir: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if evals_root.is_dir():
        for judge_trace in sorted(
            evals_root.glob("*/runs/*/trials/*/*/*/grades/semantic/judge-*/trace.jsonl")
        ):
            trial_dir = judge_trace.parents[3].resolve()
            trial_dir_str = str(trial_dir)
            run_meta = read_json(trial_dir / "run.json")
            parsed_judge = parse_session_rollout(judge_trace)
            j_model = parsed_judge.get("model")
            j_effort = parsed_judge.get("reasoning_effort")
            j_started = parsed_judge.get("started_at")
            if isinstance(run_meta, dict):
                if not j_model and isinstance(run_meta.get("resolved_model"), str):
                    j_model = run_meta["resolved_model"]
                if not j_effort and isinstance(
                    run_meta.get("resolved_reasoning_effort"), str
                ):
                    j_effort = run_meta["resolved_reasoning_effort"]
                if isinstance(run_meta.get("started_at"), str):
                    j_started = run_meta["started_at"]
            j_tokens = dict(parsed_judge.get("token_usage") or _empty_token_usage())
            j_cost = estimate_api_cost(j_tokens, j_model)
            judges_by_trial_dir[trial_dir_str].append(
                {
                    "judge_id": judge_trace.parent.name,
                    "trace_path": str(judge_trace),
                    "started_at": j_started,
                    "model": j_model,
                    "reasoning_effort": j_effort,
                    "turn_count": int(parsed_judge.get("turn_count", 1) or 1),
                    "token_usage": j_tokens,
                    "est_cost": j_cost,
                }
            )

    eval_root_sessions.sort(
        key=lambda s: str(s.get("started_at") or ""),
        reverse=True,
    )

    eval_sessions_summary: list[dict[str, Any]] = []
    worker_tokens_total = _empty_token_usage()
    judge_tokens_total = _empty_token_usage()
    worker_cost_total = 0.0
    judge_cost_total = 0.0
    priced_worker_sessions = 0
    priced_judge_calls = 0
    total_worker_turns = 0
    total_judge_turns = 0
    total_eval_commands = 0
    total_eval_failed_commands = 0
    total_eval_subagent_spawns = 0
    total_judge_calls = 0

    eval_skill_stats: dict[str, dict[str, Any]] = {}
    eval_agent_stats: dict[str, dict[str, Any]] = {}
    eval_model_stats: dict[str, dict[str, Any]] = {}

    def _record_eval_model(
        model_name: str | None,
        effort_name: str | None,
        call_kind: str,
        calls: int,
        turns: int,
        tokens: dict[str, int],
        cost: float | None,
    ) -> None:
        m_key = model_name or "unknown"
        e_key = effort_name or "default"
        me_key = f"{m_key} ({e_key})"
        if me_key not in eval_model_stats:
            eval_model_stats[me_key] = {
                "model": m_key,
                "reasoning_effort": e_key,
                "worker_calls": 0,
                "subagent_calls": 0,
                "judge_calls": 0,
                "total_calls": 0,
                "turns": 0,
                "tokens": _empty_token_usage(),
                "est_cost": 0.0,
                "priced_count": 0,
            }
        ms = eval_model_stats[me_key]
        if call_kind == "worker":
            ms["worker_calls"] += calls
        elif call_kind == "subagent":
            ms["subagent_calls"] += calls
        elif call_kind == "judge":
            ms["judge_calls"] += calls
        ms["total_calls"] += calls
        ms["turns"] += turns
        ms["tokens"] = _add_token_dicts(ms["tokens"], tokens)
        if cost is not None:
            ms["est_cost"] = round(ms["est_cost"] + cost, 6)
            ms["priced_count"] += calls

    def _ensure_eval_skill(sk_name: str) -> dict[str, Any]:
        if sk_name not in eval_skill_stats:
            eval_skill_stats[sk_name] = {
                "skill": sk_name,
                "experiments": set(),
                "worker_trials": 0,
                "baseline_trials": 0,
                "with_skill_trials": 0,
                "subagent_spawns": 0,
                "judge_calls": 0,
                "worker_tokens": _empty_token_usage(),
                "judge_tokens": _empty_token_usage(),
                "tokens": _empty_token_usage(),
                "worker_est_cost": 0.0,
                "judge_est_cost": 0.0,
                "est_cost": 0.0,
                "priced_count": 0,
            }
        return eval_skill_stats[sk_name]

    consumed_judge_trial_dirs: set[str] = set()

    for root in eval_root_sessions:
        root_tid = root.get("thread_id") or root["rollout_path"]
        parsed_meta = parse_eval_run_cwd(root.get("cwd"), evals_root) or {
            "eval_skill": "unknown",
            "experiment": "unknown",
            "test_case": "unknown",
            "configuration": "unknown",
            "trial": "unknown",
            "trial_dir": None,
        }
        trial_dir_str = parsed_meta.get("trial_dir")
        r_model = root.get("model")
        r_effort = root.get("reasoning_effort")
        if trial_dir_str:
            run_meta = read_json(Path(trial_dir_str) / "run.json")
            if isinstance(run_meta, dict):
                if not r_model and isinstance(run_meta.get("resolved_model"), str):
                    r_model = run_meta["resolved_model"]
                if not r_effort and isinstance(
                    run_meta.get("resolved_reasoning_effort"), str
                ):
                    r_effort = run_meta["resolved_reasoning_effort"]

        parent_tokens = dict(root.get("token_usage") or _empty_token_usage())
        parent_cost = estimate_api_cost(parent_tokens, r_model)
        (
            subagent_records,
            subagent_tokens,
            subagent_cost_sum,
            subagent_cost_known,
        ) = _traverse_subagent_tree(root, root_tid, by_tid, children_by_parent)

        roles_in_trial: set[str] = set()
        for sub in subagent_records:
            c_role = sub["agent_role"]
            c_tokens = sub["token_usage"]
            c_cost = sub["est_cost"]
            c_cmd = sub["command_count"]
            c_fail = sub["failed_command_count"]
            if c_role not in eval_agent_stats:
                eval_agent_stats[c_role] = {
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
            astat = eval_agent_stats[c_role]
            astat["spawns"] += 1
            astat["command_count"] += c_cmd
            astat["failed_command_count"] += c_fail
            astat["tokens"] = _add_token_dicts(astat["tokens"], c_tokens)
            if c_cost is not None:
                astat["est_cost"] = round(astat["est_cost"] + c_cost, 6)
                astat["priced_count"] += 1
            roles_in_trial.add(c_role)
            _record_eval_model(
                sub.get("model") or r_model,
                sub.get("reasoning_effort") or r_effort,
                "subagent",
                1,
                1,
                c_tokens,
                c_cost,
            )
        for r_name in roles_in_trial:
            eval_agent_stats[r_name]["sessions_used"] += 1

        trial_worker_tokens = _add_token_dicts(parent_tokens, subagent_tokens)
        if parent_cost is not None or subagent_cost_known:
            trial_worker_cost: float | None = round(
                (parent_cost or 0.0) + subagent_cost_sum, 6
            )
        else:
            trial_worker_cost = None

        turns = int(root.get("turn_count", 1) or 1)
        _record_eval_model(
            r_model,
            r_effort,
            "worker",
            1,
            turns,
            parent_tokens,
            parent_cost,
        )

        # Attach semantic judges for this trial_dir if present
        trial_judges: list[dict[str, Any]] = []
        if trial_dir_str and trial_dir_str in judges_by_trial_dir:
            trial_judges = judges_by_trial_dir[trial_dir_str]
            consumed_judge_trial_dirs.add(trial_dir_str)

        trial_judge_tokens = _empty_token_usage()
        trial_judge_cost_sum = 0.0
        trial_judge_cost_known = False
        for j in trial_judges:
            trial_judge_tokens = _add_token_dicts(
                trial_judge_tokens, j["token_usage"]
            )
            if j["est_cost"] is not None:
                trial_judge_cost_sum += j["est_cost"]
                trial_judge_cost_known = True
                priced_judge_calls += 1
            total_judge_turns += int(j.get("turn_count", 1) or 1)
            _record_eval_model(
                j.get("model") or r_model,
                j.get("reasoning_effort") or r_effort,
                "judge",
                1,
                int(j.get("turn_count", 1) or 1),
                j["token_usage"],
                j["est_cost"],
            )

        trial_judge_cost: float | None = (
            round(trial_judge_cost_sum, 6) if trial_judge_cost_known else None
        )
        total_judge_calls += len(trial_judges)
        judge_tokens_total = _add_token_dicts(judge_tokens_total, trial_judge_tokens)
        if trial_judge_cost is not None:
            judge_cost_total = round(judge_cost_total + trial_judge_cost, 6)

        trial_total_tokens = _add_token_dicts(
            trial_worker_tokens, trial_judge_tokens
        )
        if trial_worker_cost is not None or trial_judge_cost is not None:
            trial_total_cost: float | None = round(
                (trial_worker_cost or 0.0) + (trial_judge_cost or 0.0), 6
            )
        else:
            trial_total_cost = None

        cmds = int(root.get("command_count", 0) or 0) + sum(
            s["command_count"] for s in subagent_records
        )
        failed_cmds = int(root.get("failed_command_count", 0) or 0) + sum(
            s["failed_command_count"] for s in subagent_records
        )
        spawns = max(
            int(root.get("subagent_spawn_count", 0) or 0), len(subagent_records)
        )

        worker_tokens_total = _add_token_dicts(
            worker_tokens_total, trial_worker_tokens
        )
        if trial_worker_cost is not None:
            worker_cost_total = round(worker_cost_total + trial_worker_cost, 6)
            priced_worker_sessions += 1
        total_worker_turns += turns
        total_eval_commands += cmds
        total_eval_failed_commands += failed_cmds
        total_eval_subagent_spawns += spawns

        sk_name = str(parsed_meta["eval_skill"])
        es = _ensure_eval_skill(sk_name)
        exp_name = str(parsed_meta["experiment"])
        if exp_name and exp_name != "unknown":
            es["experiments"].add(exp_name)
        es["worker_trials"] += 1
        cfg_name = str(parsed_meta["configuration"])
        if cfg_name == "baseline":
            es["baseline_trials"] += 1
        elif cfg_name == "with-skill":
            es["with_skill_trials"] += 1
        es["subagent_spawns"] += spawns
        es["judge_calls"] += len(trial_judges)
        es["worker_tokens"] = _add_token_dicts(
            es["worker_tokens"], trial_worker_tokens
        )
        es["judge_tokens"] = _add_token_dicts(
            es["judge_tokens"], trial_judge_tokens
        )
        es["tokens"] = _add_token_dicts(es["tokens"], trial_total_tokens)
        if trial_worker_cost is not None:
            es["worker_est_cost"] = round(
                es["worker_est_cost"] + trial_worker_cost, 6
            )
        if trial_judge_cost is not None:
            es["judge_est_cost"] = round(
                es["judge_est_cost"] + trial_judge_cost, 6
            )
        if trial_total_cost is not None:
            es["est_cost"] = round(es["est_cost"] + trial_total_cost, 6)
            es["priced_count"] += 1

        started_str = str(root.get("started_at") or "")
        day_key = started_str[:10] if len(started_str) >= 10 else "unknown"
        trial_grades = (
            read_trial_grades(Path(trial_dir_str))
            if trial_dir_str
            else {"deterministic": None, "semantic": None}
        )
        eval_sessions_summary.append(
            {
                "thread_id": root_tid,
                "started_at": started_str,
                "date": day_key,
                "cwd": root.get("cwd"),
                "eval_skill": sk_name,
                "experiment": exp_name,
                "test_case": str(parsed_meta["test_case"]),
                "configuration": cfg_name,
                "trial": str(parsed_meta["trial"]),
                "model": r_model,
                "reasoning_effort": r_effort,
                "turn_count": turns,
                "command_count": cmds,
                "failed_command_count": failed_cmds,
                "commands": root.get("commands") or [],
                "first_prompt": root.get("first_prompt") or "",
                "subagent_spawn_count": spawns,
                "subagents": subagent_records,
                "judge_call_count": len(trial_judges),
                "judge_tokens": trial_judge_tokens,
                "judge_est_cost": trial_judge_cost,
                "parent_token_usage": parent_tokens,
                "subagent_token_usage": subagent_tokens,
                "worker_tokens": trial_worker_tokens,
                "worker_est_cost": trial_worker_cost,
                "token_usage": trial_total_tokens,
                "est_cost": trial_total_cost,
                "grades": trial_grades,
            }
        )

    # Include any orphan judge calls whose trial_dir had no worker rollout
    for trial_dir_str, orphan_judges in sorted(judges_by_trial_dir.items()):
        if trial_dir_str in consumed_judge_trial_dirs:
            continue
        parsed_meta = parse_eval_run_cwd(trial_dir_str, evals_root) or {
            "eval_skill": "unknown",
            "experiment": "unknown",
            "test_case": "unknown",
            "configuration": "unknown",
            "trial": "unknown",
        }
        sk_name = str(parsed_meta["eval_skill"])
        es = _ensure_eval_skill(sk_name)
        exp_name = str(parsed_meta["experiment"])
        if exp_name and exp_name != "unknown":
            es["experiments"].add(exp_name)
        for j in orphan_judges:
            total_judge_calls += 1
            total_judge_turns += int(j.get("turn_count", 1) or 1)
            judge_tokens_total = _add_token_dicts(
                judge_tokens_total, j["token_usage"]
            )
            es["judge_calls"] += 1
            es["judge_tokens"] = _add_token_dicts(
                es["judge_tokens"], j["token_usage"]
            )
            es["tokens"] = _add_token_dicts(es["tokens"], j["token_usage"])
            if j["est_cost"] is not None:
                judge_cost_total = round(judge_cost_total + j["est_cost"], 6)
                priced_judge_calls += 1
                es["judge_est_cost"] = round(
                    es["judge_est_cost"] + j["est_cost"], 6
                )
                es["est_cost"] = round(es["est_cost"] + j["est_cost"], 6)
                es["priced_count"] += 1
            _record_eval_model(
                j.get("model"),
                j.get("reasoning_effort"),
                "judge",
                1,
                int(j.get("turn_count", 1) or 1),
                j["token_usage"],
                j["est_cost"],
            )

    combined_eval_tokens = _add_token_dicts(worker_tokens_total, judge_tokens_total)
    eval_input = combined_eval_tokens["input_tokens"]
    eval_cache_hit_pct = (
        round(combined_eval_tokens["cached_input_tokens"] / eval_input * 100, 1)
        if eval_input > 0
        else 0.0
    )
    priced_calls = priced_worker_sessions + priced_judge_calls
    total_eval_cost = (
        round(worker_cost_total + judge_cost_total, 6)
        if priced_calls > 0
        else None
    )

    by_skill_list: list[dict[str, Any]] = []
    for sk_entry in eval_skill_stats.values():
        exps = sorted(sk_entry["experiments"])
        by_skill_list.append(
            {
                "skill": sk_entry["skill"],
                "experiments": exps,
                "experiment_count": len(exps),
                "worker_trials": sk_entry["worker_trials"],
                "baseline_trials": sk_entry["baseline_trials"],
                "with_skill_trials": sk_entry["with_skill_trials"],
                "subagent_spawns": sk_entry["subagent_spawns"],
                "judge_calls": sk_entry["judge_calls"],
                "worker_tokens": sk_entry["worker_tokens"],
                "judge_tokens": sk_entry["judge_tokens"],
                "tokens": sk_entry["tokens"],
                "worker_est_cost": sk_entry["worker_est_cost"],
                "judge_est_cost": sk_entry["judge_est_cost"],
                "est_cost": sk_entry["est_cost"],
                "priced_count": sk_entry["priced_count"],
            }
        )
    by_skill_list.sort(key=lambda s: (-s["worker_trials"], s["skill"]))

    return {
        "totals": {
            "worker_session_count": len(eval_sessions_summary),
            "subagent_spawn_count": total_eval_subagent_spawns,
            "judge_call_count": total_judge_calls,
            "total_call_count": (
                len(eval_sessions_summary)
                + total_eval_subagent_spawns
                + total_judge_calls
            ),
            "turn_count": total_worker_turns + total_judge_turns,
            "worker_turn_count": total_worker_turns,
            "judge_turn_count": total_judge_turns,
            "command_count": total_eval_commands,
            "failed_command_count": total_eval_failed_commands,
            "cache_hit_pct": eval_cache_hit_pct,
            "worker_tokens": worker_tokens_total,
            "judge_tokens": judge_tokens_total,
            "tokens": combined_eval_tokens,
            "worker_est_cost": (
                round(worker_cost_total, 6) if priced_worker_sessions > 0 else None
            ),
            "judge_est_cost": (
                round(judge_cost_total, 6) if priced_judge_calls > 0 else None
            ),
            "est_cost": total_eval_cost,
            "priced_calls": priced_calls,
        },
        "by_skill": by_skill_list,
        "subagents": sorted(
            eval_agent_stats.values(),
            key=lambda a: (-a["spawns"], a["role"]),
        ),
        "models": sorted(
            eval_model_stats.values(),
            key=lambda m: (-m["total_calls"], m["model"]),
        ),
        "sessions": eval_sessions_summary,
    }


def build_project_usage(
    rollouts_by_path: dict[str, dict[str, Any]],
    project_root: Path,
    evals_root: Path,
) -> dict[str, Any]:
    """Filter, link parent/subagent trees, and aggregate usage metrics for `project_root`.

    Splits telemetry into:
    1. Real Project Work (`totals`, `skills`, `subagents`, `models`, `daily`, `sessions`)
    2. Harness Eval Calls (`harness_evals`: `eval.py run` worker trials, eval subagents, semantic judges)
    3. Combined Totals (`combined_totals`)
    """
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
    total_parent_tokens = _empty_token_usage()
    total_subagent_tokens = _empty_token_usage()
    total_cost = 0.0
    total_parent_cost = 0.0
    total_subagent_cost = 0.0
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

        (
            subagent_records,
            subagent_tokens,
            subagent_cost_sum,
            subagent_cost_known,
        ) = _traverse_subagent_tree(root, root_tid, by_tid, children_by_parent)

        roles_in_session: set[str] = set()
        for sub in subagent_records:
            c_role = sub["agent_role"]
            c_tokens = sub["token_usage"]
            c_cost = sub["est_cost"]
            c_cmd = sub["command_count"]
            c_fail = sub["failed_command_count"]
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
        total_parent_tokens = _add_token_dicts(total_parent_tokens, parent_tokens)
        total_subagent_tokens = _add_token_dicts(
            total_subagent_tokens, subagent_tokens
        )
        if parent_cost is not None:
            total_parent_cost = round(total_parent_cost + parent_cost, 6)
        if subagent_cost_known:
            total_subagent_cost = round(total_subagent_cost + subagent_cost_sum, 6)
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
                "commands": root.get("commands") or [],
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
    real_est_cost = round(total_cost, 6) if priced_sessions > 0 else None

    harness_evals = _collect_harness_eval_usage(
        by_tid=by_tid,
        children_by_parent=children_by_parent,
        evals_root=evals_root,
        installed_agents=installed_agents,
    )
    eval_totals = harness_evals["totals"]
    combined_all_tokens = _add_token_dicts(total_tokens, eval_totals["tokens"])
    combined_input = combined_all_tokens["input_tokens"]
    combined_cache_hit_pct = (
        round(
            combined_all_tokens["cached_input_tokens"] / combined_input * 100, 1
        )
        if combined_input > 0
        else 0.0
    )
    if real_est_cost is not None or eval_totals["est_cost"] is not None:
        combined_est_cost: float | None = round(
            (real_est_cost or 0.0) + (eval_totals["est_cost"] or 0.0), 6
        )
    else:
        combined_est_cost = None

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
            "parent_tokens": total_parent_tokens,
            "subagent_tokens": total_subagent_tokens,
            "tokens": total_tokens,
            "parent_est_cost": (
                round(total_parent_cost, 6) if priced_sessions > 0 else None
            ),
            "subagent_est_cost": (
                round(total_subagent_cost, 6)
                if total_subagent_spawns > 0
                else None
            ),
            "est_cost": real_est_cost,
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
        "harness_evals": harness_evals,
        "combined_totals": {
            "real_session_count": len(sessions_summary),
            "real_subagent_count": total_subagent_spawns,
            "eval_worker_count": eval_totals["worker_session_count"],
            "eval_subagent_count": eval_totals["subagent_spawn_count"],
            "eval_judge_count": eval_totals["judge_call_count"],
            "eval_call_count": eval_totals["total_call_count"],
            "cache_hit_pct": combined_cache_hit_pct,
            "tokens": combined_all_tokens,
            "est_cost": combined_est_cost,
        },
    }


def _default_harness_evals() -> dict[str, Any]:
    empty_tok = _empty_token_usage()
    return {
        "totals": {
            "worker_session_count": 0,
            "subagent_spawn_count": 0,
            "judge_call_count": 0,
            "total_call_count": 0,
            "turn_count": 0,
            "worker_turn_count": 0,
            "judge_turn_count": 0,
            "command_count": 0,
            "failed_command_count": 0,
            "cache_hit_pct": 0.0,
            "worker_tokens": dict(empty_tok),
            "judge_tokens": dict(empty_tok),
            "tokens": dict(empty_tok),
            "worker_est_cost": None,
            "judge_est_cost": None,
            "est_cost": None,
            "priced_calls": 0,
        },
        "by_skill": [],
        "subagents": [],
        "models": [],
        "sessions": [],
    }


def render_usage_markdown(summary: dict[str, Any]) -> str:
    """Render the human-readable `_usage/report.md` for a project, split into Real Project Work and Harness Eval Calls."""
    totals = summary["totals"]
    tok = totals["tokens"]
    total_cost_str = (
        format_cost(totals["est_cost"])
        if totals["session_count"] > 0
        else "$0.0000"
    )
    harness_evals = summary.get("harness_evals") or _default_harness_evals()
    etotals = harness_evals["totals"]
    etok = etotals["tokens"]
    eval_cost_str = (
        format_cost(etotals["est_cost"])
        if etotals["total_call_count"] > 0
        else "$0.0000"
    )
    combined = summary.get("combined_totals") or {
        "tokens": _add_token_dicts(tok, etok),
        "cache_hit_pct": totals.get("cache_hit_pct", 0.0),
        "est_cost": (
            round((totals.get("est_cost") or 0.0) + (etotals.get("est_cost") or 0.0), 6)
            if (totals.get("est_cost") is not None or etotals.get("est_cost") is not None)
            else None
        ),
    }
    ctok = combined["tokens"]
    combined_cost_str = (
        format_cost(combined["est_cost"])
        if (totals["session_count"] + etotals["total_call_count"]) > 0
        else "$0.0000"
    )

    lines = [
        f"# Project usage report: {summary['project_name']}",
        "",
        f"**Project root:** `{summary['project_root']}` · **Updated:** `{summary['generated_at']}`",
        "",
        "## Workload split (Real Project Work vs Harness Eval Calls)",
        "",
        "| Workload | Root sessions / trials | Subagents | Judges | Turns | Uncached input | Cached input (hit %) | Billed output | Est. API cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| **Real Project Work** | {totals['session_count']} | "
            f"{totals['subagent_spawn_count']} | 0 | {totals['turn_count']} | "
            f"{tok['uncached_input_tokens']:,} | "
            f"{tok['cached_input_tokens']:,} ({totals['cache_hit_pct']}%) | "
            f"{tok['billed_output_tokens']:,} | {total_cost_str} |"
        ),
        (
            f"| **Harness Eval Calls** | {etotals['worker_session_count']} | "
            f"{etotals['subagent_spawn_count']} | {etotals['judge_call_count']} | "
            f"{etotals['turn_count']} | "
            f"{etok['uncached_input_tokens']:,} | "
            f"{etok['cached_input_tokens']:,} ({etotals['cache_hit_pct']}%) | "
            f"{etok['billed_output_tokens']:,} | {eval_cost_str} |"
        ),
        (
            f"| **Combined Total** | {totals['session_count'] + etotals['worker_session_count']} | "
            f"{totals['subagent_spawn_count'] + etotals['subagent_spawn_count']} | "
            f"{etotals['judge_call_count']} | "
            f"{totals['turn_count'] + etotals['turn_count']} | "
            f"{ctok['uncached_input_tokens']:,} | "
            f"{ctok['cached_input_tokens']:,} ({combined['cache_hit_pct']}%) | "
            f"{ctok['billed_output_tokens']:,} | {combined_cost_str} |"
        ),
        "",
        f"> **Cost note:** `Est. API cost` is an API-equivalent estimate at public OpenAI list prices (as of {PRICING_AS_OF}, {PRICING_SOURCE}; long-context tier at {LONG_CONTEXT_THRESHOLD:,} input tokens). It is not actual subscription or credit spend.",
        "",
        "## 1. Real project work — Skill usage",
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
            "## 1b. Real project work — Subagent usage",
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
            "## 1c. Real project work — Recent sessions",
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

    lines.extend(
        [
            "",
            "## 2. Harness eval calls (`eval.py run` workers, eval subagents & semantic judges)",
            "",
            "| Evaluated skill | Experiments | Worker trials (`with-skill` / `baseline`) | Eval subagents | Semantic judges | Worker billed out | Judge billed out | Worker cost | Judge cost | Total eval cost |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if harness_evals["by_skill"]:
        for es in harness_evals["by_skill"]:
            wtok = es["worker_tokens"]
            jtok = es["judge_tokens"]
            w_cost = format_cost(es["worker_est_cost"]) if es["worker_trials"] > 0 else "$0.0000"
            j_cost = format_cost(es["judge_est_cost"]) if es["judge_calls"] > 0 else "$0.0000"
            t_cost = format_cost(es["est_cost"]) if es["priced_count"] > 0 else "$0.0000"
            lines.append(
                f"| `{markdown_cell(es['skill'])}` | {es['experiment_count']} | "
                f"{es['worker_trials']} ({es['with_skill_trials']} / {es['baseline_trials']}) | "
                f"{es['subagent_spawns']} | {es['judge_calls']} | "
                f"{wtok['billed_output_tokens']:,} | {jtok['billed_output_tokens']:,} | "
                f"{w_cost} | {j_cost} | {t_cost} |"
            )
    else:
        lines.append("| (none) | 0 | 0 (0 / 0) | 0 | 0 | 0 | 0 | $0.0000 | $0.0000 | $0.0000 |")

    lines.append("")
    return "\n".join(lines)


def render_usage_html(summary: dict[str, Any]) -> str:
    """Render the interactive two-mode usage explorer (see `_lib/usage_html.py`)."""
    from _lib.usage_html import render_usage_html as _render
    return _render(summary)


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
    eval_totals = summary["harness_evals"]["totals"]
    return {
        "skipped": False,
        "summary": summary,
        "session_count": totals["session_count"],
        "subagent_count": totals["subagent_spawn_count"],
        "eval_worker_count": eval_totals["worker_session_count"],
        "eval_subagent_count": eval_totals["subagent_spawn_count"],
        "eval_judge_count": eval_totals["judge_call_count"],
        "eval_call_count": eval_totals["total_call_count"],
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
