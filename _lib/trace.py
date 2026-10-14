"""Codex JSONL trace parsing, rollout provenance lookup, and skill snapshot reading."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


def trace_thread_id(trace: Path) -> str | None:
    """Return the persisted Codex thread ID recorded in a JSONL trace."""
    if not trace.is_file():
        return None
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
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
    input_tokens = 0
    cached_input_tokens = 0
    cache_write_input_tokens = 0
    output_tokens = 0
    reasoning_output_tokens = 0
    for event in events:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            commands.append(item)
        if event.get("type") == "turn.completed" and isinstance(
            event.get("usage"), dict
        ):
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
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "uncached_input_tokens": max(input_tokens - cached_input_tokens, 0),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "billed_output_tokens": output_tokens + reasoning_output_tokens,
    }


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
