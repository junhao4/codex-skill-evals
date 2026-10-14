"""Tests for subagent trace normalization, read-only rollout capture, and token roll-up.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.harness import (  # noqa: E402
    capture_main_rollout,
    capture_subagent_rollouts,
    merge_subagent_metrics,
    read_rollout_metrics,
    sanitize_trace_jsonl,
    trace_metrics,
)


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def test_trace_metrics_no_subagents(tmp_path):
    trace = _write_jsonl(
        tmp_path / "trace.jsonl",
        [
            {"type": "thread.started", "thread_id": "parent-01"},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 200,
                    "output_tokens": 150,
                    "reasoning_output_tokens": 50,
                },
            },
        ],
    )
    m = trace_metrics(trace)
    assert m["subagent_spawn_count"] == 0
    assert m["subagent_wait_count"] == 0
    assert m["subagent_thread_ids"] == []
    assert m["subagents"] == []


def test_trace_metrics_live_collab_tool_call_and_skips_item_started(tmp_path):
    trace = _write_jsonl(
        tmp_path / "trace.jsonl",
        [
            {"type": "thread.started", "thread_id": "parent-01"},
            {
                "type": "item.started",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "parent-01",
                    "receiver_thread_ids": ["child-01"],
                    "prompt": "Review security",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": "parent-01",
                    "receiver_thread_ids": ["child-01"],
                    "prompt": "Review security",
                    "agents_states": {
                        "child-01": {"status": "pending_init", "message": None}
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "status": "completed",
                    "sender_thread_id": "parent-01",
                    "receiver_thread_ids": ["child-01"],
                    "agents_states": {
                        "child-01": {
                            "status": "completed",
                            "message": "Found 1 SQL injection",
                        }
                    },
                },
            },
        ],
    )
    m = trace_metrics(trace)
    assert m["subagent_spawn_count"] == 1
    assert m["subagent_wait_count"] == 1
    assert m["subagent_thread_ids"] == ["child-01"]
    assert m["subagents"] == [
        {
            "thread_id": "child-01",
            "parent_thread_id": "parent-01",
            "prompt": "Review security",
            "waited": True,
            "final_status": "completed",
            "final_message": "Found 1 SQL injection",
        }
    ]


def test_trace_metrics_tolerates_pascal_case_rollout_collab_events(tmp_path):
    trace = _write_jsonl(
        tmp_path / "trace.jsonl",
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "CollabAgentToolCall",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": "parent-02",
                    "target_thread_id": "child-02",
                    "prompt": "Audit auth",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "collab_tool_call",
                    "tool": "wait_agent",
                    "status": "completed",
                    "sender_thread_id": "parent-02",
                    "targets": ["child-02"],
                    "agents_states": {
                        "child-02": {"status": "completed", "message": "Auth OK"}
                    },
                },
            },
        ],
    )
    m = trace_metrics(trace)
    assert m["subagent_spawn_count"] == 1
    assert m["subagent_wait_count"] == 1
    assert m["subagent_thread_ids"] == ["child-02"]
    assert m["subagents"][0]["waited"] is True
    assert m["subagents"][0]["final_message"] == "Auth OK"


def test_read_rollout_metrics_from_persisted_child_rollout(tmp_path):
    rollout = _write_jsonl(
        tmp_path / "rollout-child.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child-01",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent-01",
                                "depth": 1,
                                "agent_nickname": "Locke",
                                "agent_role": "security-reviewer",
                            }
                        }
                    },
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.4-mini", "effort": "medium"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "exec_command_end", "exit_code": 0},
            },
            {
                "type": "event_msg",
                "payload": {"type": "exec_command_end", "exit_code": 2},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 5000,
                            "cached_input_tokens": 4000,
                            "output_tokens": 300,
                            "reasoning_output_tokens": 100,
                        }
                    },
                },
            },
        ],
    )
    info = read_rollout_metrics(rollout)
    assert info["thread_id"] == "child-01"
    assert info["parent_thread_id"] == "parent-01"
    assert info["depth"] == 1
    assert info["agent_nickname"] == "Locke"
    assert info["agent_role"] == "security-reviewer"
    assert info["model"] == "gpt-5.4-mini"
    assert info["reasoning_effort"] == "medium"
    assert info["command_count"] == 2
    assert info["failed_command_count"] == 1
    assert info["token_usage"]["input_tokens"] == 5000
    assert info["token_usage"]["cached_input_tokens"] == 4000
    assert info["token_usage"]["uncached_input_tokens"] == 1000
    assert info["token_usage"]["output_tokens"] == 300
    assert info["token_usage"]["reasoning_output_tokens"] == 100


def test_capture_subagent_rollouts_skips_dir_when_zero_spawns(tmp_path):
    trace = _write_jsonl(
        tmp_path / "worker" / "trace.jsonl",
        [{"type": "thread.started", "thread_id": "parent-01"}],
    )
    subagents_dir = tmp_path / "worker" / "subagents"
    sessions_root = tmp_path / "sessions"
    manifest = capture_subagent_rollouts(trace, subagents_dir, sessions_root)
    assert not subagents_dir.exists()
    assert manifest["subagent_spawn_count"] == 0
    assert manifest["subagent_thread_ids"] == []


def test_capture_subagent_rollouts_recursive_and_graceful_missing(tmp_path):
    sessions_day = tmp_path / "sessions" / "2026" / "10" / "14"
    parent_trace = _write_jsonl(
        tmp_path / "worker" / "trace.jsonl",
        [
            {"type": "thread.started", "thread_id": "parent-01"},
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "parent-01",
                    "receiver_thread_ids": ["child-01"],
                    "prompt": "Task 1",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "parent-01",
                    "receiver_thread_ids": ["child-missing"],
                    "prompt": "Task 2",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "sender_thread_id": "parent-01",
                    "receiver_thread_ids": ["child-01", "child-missing"],
                    "agents_states": {
                        "child-01": {"status": "completed", "message": "Done 1"},
                        "child-missing": {"status": "completed", "message": "Done 2"},
                    },
                },
            },
        ],
    )

    # Child-01 spawns grandchild-01 inside its persisted rollout
    _write_jsonl(
        sessions_day / "rollout-2026-10-14T12-00-00-child-01.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child-01",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent-01",
                                "depth": 1,
                                "agent_nickname": "Gauss",
                            }
                        }
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "child-01",
                    "receiver_thread_ids": ["grandchild-01"],
                    "prompt": "Nested check",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "sender_thread_id": "child-01",
                    "receiver_thread_ids": ["grandchild-01"],
                    "agents_states": {
                        "grandchild-01": {
                            "status": "completed",
                            "message": "Nested done",
                        }
                    },
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 2000,
                    "cached_input_tokens": 1000,
                    "output_tokens": 200,
                    "reasoning_output_tokens": 50,
                },
            },
        ],
    )
    _write_jsonl(
        sessions_day / "rollout-2026-10-14T12-01-00-grandchild-01.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "grandchild-01",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "child-01",
                                "depth": 2,
                                "agent_nickname": "Euler",
                            }
                        }
                    },
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 500,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 25,
                },
            },
        ],
    )

    subagents_dir = tmp_path / "worker" / "subagents"
    manifest = capture_subagent_rollouts(
        parent_trace, subagents_dir, tmp_path / "sessions"
    )

    # 2 parent spawns + 1 nested spawn = 3 total spawns; 1 parent wait + 1 nested wait = 2 waits
    assert manifest["subagent_spawn_count"] == 3
    assert manifest["subagent_wait_count"] == 2
    assert manifest["subagent_thread_ids"] == [
        "child-01",
        "child-missing",
        "grandchild-01",
    ]
    assert (subagents_dir / "child-01.jsonl").is_file()
    assert (subagents_dir / "grandchild-01.jsonl").is_file()
    assert not (subagents_dir / "child-missing.jsonl").exists()
    assert (subagents_dir / "manifest.json").is_file()

    by_tid = {s["thread_id"]: s for s in manifest["subagents"]}
    assert by_tid["child-01"]["rollout_captured"] is True
    assert by_tid["child-01"]["depth"] == 1
    assert by_tid["child-01"]["agent_nickname"] == "Gauss"
    assert by_tid["child-missing"]["rollout_captured"] is False
    assert by_tid["child-missing"]["rollout_file"] is None
    assert by_tid["grandchild-01"]["rollout_captured"] is True
    assert by_tid["grandchild-01"]["depth"] == 2
    assert by_tid["grandchild-01"]["parent_thread_id"] == "child-01"

    assert manifest["subagent_token_usage"]["input_tokens"] == 3000
    assert manifest["subagent_token_usage"]["cached_input_tokens"] == 1500
    assert manifest["subagent_token_usage"]["uncached_input_tokens"] == 1500
    assert manifest["subagent_token_usage"]["output_tokens"] == 300
    assert manifest["subagent_token_usage"]["reasoning_output_tokens"] == 75


def test_merge_subagent_metrics_rolls_up_tokens_and_preserves_breakdown():
    parent_metrics = {
        "event_count": 10,
        "input_tokens": 4000,
        "cached_input_tokens": 3000,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": 1000,
        "output_tokens": 400,
        "reasoning_output_tokens": 100,
        "billed_output_tokens": 400,
        "subagent_spawn_count": 1,
        "subagent_wait_count": 1,
        "subagent_thread_ids": ["child-01"],
        "subagents": [],
    }
    sub_capture = {
        "subagent_spawn_count": 2,
        "subagent_wait_count": 2,
        "subagent_thread_ids": ["child-01", "grandchild-01"],
        "subagent_token_usage": {
            "input_tokens": 2500,
            "cached_input_tokens": 1500,
            "cache_write_input_tokens": 0,
            "uncached_input_tokens": 1000,
            "output_tokens": 250,
            "reasoning_output_tokens": 50,
            "billed_output_tokens": 250,
        },
        "subagents": [{"thread_id": "child-01"}, {"thread_id": "grandchild-01"}],
    }
    merged = merge_subagent_metrics(parent_metrics, sub_capture)
    assert merged["input_tokens"] == 6500
    assert merged["cached_input_tokens"] == 4500
    assert merged["uncached_input_tokens"] == 2000
    assert merged["output_tokens"] == 650
    assert merged["reasoning_output_tokens"] == 150
    assert merged["billed_output_tokens"] == 800
    assert merged["parent_token_usage"]["input_tokens"] == 4000
    assert merged["parent_token_usage"]["billed_output_tokens"] == 500
    assert merged["subagent_token_usage"]["input_tokens"] == 2500
    assert merged["subagent_token_usage"]["billed_output_tokens"] == 300
    assert merged["subagent_spawn_count"] == 2
    assert merged["subagent_thread_ids"] == ["child-01", "grandchild-01"]


def test_sanitize_trace_jsonl_strips_only_encrypted_content(tmp_path):
    source = _write_jsonl(
        tmp_path / "raw-rollout.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"id": "main-01", "base_instructions": {"text": "sys"}},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<permissions instructions>workspace-write</permissions instructions>",
                        }
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Checking files"}],
                    "content": None,
                    "encrypted_content": "gAAAAABp3huge_opaque_ciphertext_blob==",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "compaction",
                    "encrypted_content": "gAAAAABp3compaction_blob==",
                },
            },
        ],
    )
    dest = tmp_path / "sanitized.jsonl"
    sanitize_trace_jsonl(source, dest)
    lines = [
        json.loads(line)
        for line in dest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 4
    assert lines[0]["payload"]["id"] == "main-01"
    assert "<permissions instructions>" in lines[1]["payload"]["content"][0]["text"]
    assert lines[2]["payload"]["summary"] == [
        {"type": "summary_text", "text": "Checking files"}
    ]
    assert "encrypted_content" not in lines[2]["payload"]
    assert "encrypted_content" not in lines[3]["payload"]


def test_capture_main_rollout_prefers_persisted_rollout_and_falls_back(tmp_path):
    sessions_day = tmp_path / "sessions" / "2026" / "10" / "14"
    turn1 = _write_jsonl(
        tmp_path / "turns" / "turn-01" / "trace.jsonl",
        [{"type": "thread.started", "thread_id": "main-01"}],
    )
    _write_jsonl(
        sessions_day / "rollout-2026-10-14T12-00-00-main-01.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "main-01"}},
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.4", "effort": "high"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Plan"}],
                    "encrypted_content": "secret_blob",
                },
            },
        ],
    )
    dest = tmp_path / "worker" / "trace.jsonl"
    used_rollout = capture_main_rollout(
        [turn1], dest, "main-01", tmp_path / "sessions"
    )
    assert used_rollout is not None
    records = [
        json.loads(line)
        for line in dest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["type"] for r in records] == [
        "session_meta",
        "turn_context",
        "response_item",
    ]
    assert "encrypted_content" not in records[2]["payload"]

    # Fallback when rollout file is absent
    dest_fallback = tmp_path / "worker-fallback" / "trace.jsonl"
    used_rollout_missing = capture_main_rollout(
        [turn1], dest_fallback, "missing-thread", tmp_path / "sessions"
    )
    assert used_rollout_missing is None
    fallback_records = [
        json.loads(line)
        for line in dest_fallback.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert fallback_records == [{"type": "thread.started", "thread_id": "main-01"}]


def test_trace_metrics_parses_persisted_rollout_format(tmp_path):
    rollout_trace = _write_jsonl(
        tmp_path / "trace.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "main-01"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "main-01",
                    "item": {
                        "type": "CommandExecution",
                        "command": [
                            "/bin/zsh",
                            "-lc",
                            "cat .agents/skills/demo-skill/SKILL.md",
                        ],
                        "exit_code": 0,
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "main-01",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["/bin/zsh", "-lc", "git reset --hard"],
                        "exit_code": 1,
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "main-01",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "tool": "spawn_agent",
                        "status": "completed",
                        "sender_thread_id": "main-01",
                        "receiver_thread_ids": ["child-01"],
                        "prompt": "Audit code",
                        "agents_states": {"child-01": "pending_init"},
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "main-01",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "tool": "wait",
                        "status": "completed",
                        "sender_thread_id": "main-01",
                        "receiver_thread_ids": ["child-01"],
                        "agents_states": {
                            "child-01": {"completed": "Audit finished cleanly."}
                        },
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 8000,
                            "cached_input_tokens": 6000,
                            "output_tokens": 500,
                            "reasoning_output_tokens": 200,
                        }
                    },
                },
            },
        ],
    )
    m = trace_metrics(rollout_trace, "demo-skill")
    assert m["command_count"] == 2
    assert m["failed_command_count"] == 1
    assert m["skill_read_observed"] is True
    assert m["prohibited_commands"] == ["/bin/zsh -lc git reset --hard"]
    assert m["input_tokens"] == 8000
    assert m["cached_input_tokens"] == 6000
    assert m["uncached_input_tokens"] == 2000
    assert m["output_tokens"] == 500
    assert m["reasoning_output_tokens"] == 200
    assert m["billed_output_tokens"] == 700
    assert m["subagent_spawn_count"] == 1
    assert m["subagent_wait_count"] == 1
    assert m["subagent_thread_ids"] == ["child-01"]
    assert m["subagents"][0]["parent_thread_id"] == "main-01"
    assert m["subagents"][0]["waited"] is True
    assert m["subagents"][0]["final_status"] == "completed"
    assert m["subagents"][0]["final_message"] == "Audit finished cleanly."

