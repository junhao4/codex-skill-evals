"""Tests for the pure rollout provenance reader (model + reasoning effort).

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.harness import read_turn_context, find_rollout  # noqa: E402


def _write_rollout(tmp_path: Path, records: list[dict]) -> Path:
    f = tmp_path / "rollout.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return f


def test_reads_model_and_effort_from_turn_context(tmp_path):
    rollout = _write_rollout(tmp_path, [
        {"type": "session_meta", "payload": {"id": "x", "model_provider": "openai"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}},
    ])
    ctx = read_turn_context(rollout)
    assert ctx == {"model": "gpt-5.6-luna", "reasoning_effort": "low"}


def test_uses_last_turn_context_when_multiple(tmp_path):
    # A multi-turn session has several turn_context records; take the last.
    rollout = _write_rollout(tmp_path, [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "high"}},
    ])
    ctx = read_turn_context(rollout)
    assert ctx["reasoning_effort"] == "high"


def test_missing_file_returns_none_values(tmp_path):
    ctx = read_turn_context(tmp_path / "does-not-exist.jsonl")
    assert ctx == {"model": None, "reasoning_effort": None}


def test_no_turn_context_returns_none_values(tmp_path):
    rollout = _write_rollout(tmp_path, [
        {"type": "session_meta", "payload": {"id": "x"}},
    ])
    ctx = read_turn_context(rollout)
    assert ctx == {"model": None, "reasoning_effort": None}


def test_tolerates_malformed_lines(tmp_path):
    f = tmp_path / "rollout.jsonl"
    f.write_text(
        "not json\n"
        + json.dumps({"type": "turn_context", "payload": {"model": "m", "effort": "medium"}})
        + "\n",
        encoding="utf-8",
    )
    ctx = read_turn_context(f)
    assert ctx == {"model": "m", "reasoning_effort": "medium"}


# --- find_rollout ---


def test_find_rollout_locates_file_by_thread_id(tmp_path):
    sessions = tmp_path / "sessions" / "2026" / "10" / "09"
    sessions.mkdir(parents=True)
    tid = "01a119c7-0e0d-7222-b1d5-30cfb8426d20"
    target = sessions / f"rollout-2026-10-09T12-30-38-{tid}.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    # a decoy from a different thread
    (sessions / "rollout-2026-10-09T11-00-00-other.jsonl").write_text("{}\n", encoding="utf-8")

    found = find_rollout(tid, tmp_path / "sessions")
    assert found == target


def test_find_rollout_returns_none_when_absent(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    assert find_rollout("missing-thread", sessions) is None


def test_find_rollout_none_thread_returns_none(tmp_path):
    assert find_rollout(None, tmp_path) is None


def test_find_rollout_missing_sessions_dir_returns_none(tmp_path):
    assert find_rollout("x", tmp_path / "nope") is None
