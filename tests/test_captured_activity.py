"""Recovery contract for captured activity with live session files unavailable."""

import json
from pathlib import Path

import pytest

from _lib.usage import _inject_captured_subagents, build_project_usage, parse_session_rollout


def rollout(path, tid, cwd, seconds=5, tokens=10, parent=None):
    meta = {"id": tid, "cwd": str(cwd)}
    if parent:
        meta["parent_thread_id"] = parent
    events = [
        {"timestamp": "2026-10-26T00:00:00Z", "type": "session_meta", "payload": meta},
        {"timestamp": "2026-10-26T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-10-26T00:00:02Z", "type": "item.completed", "item": {"type": "command_execution", "command": "cat retained-evidence.txt", "status": "completed", "exit_code": 0}},
        {"timestamp": f"2026-10-26T00:00:{seconds:02d}Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": f"Finding from {tid}"}},
        {"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 1}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    return parse_session_rollout(path)


@pytest.fixture
def captured_trial(tmp_path):
    project = tmp_path / "project"
    evals = project / ".agents/evals"
    trial = evals / "example/runs/experiment-001/trials/case/with-skill/trial-01"
    worker = trial / "worker"
    root = rollout(worker / "trace.jsonl", "main", trial / "workspace", seconds=20)
    rollout(worker / "subagents/child.jsonl", "child", trial / "workspace", parent="main")
    manifest = {"subagents": [{"thread_id": "child", "parent_thread_id": "main", "depth": 1,
                               "agent_role": "auditor", "prompt": "Audit retained evidence", "rollout_captured": True}]}
    (worker / "subagents/manifest.json").write_text(json.dumps(manifest))
    return project, evals, worker, root


@pytest.mark.parametrize("parent_in_live_cache", [False, True])
def test_captured_children_survive_with_or_without_live_parent(captured_trial, parent_in_live_cache):
    project, evals, worker, root = captured_trial
    originals = {p: p.read_bytes() for p in worker.rglob("*") if p.is_file()}
    records = {"live-parent.jsonl": root} if parent_in_live_cache else {}
    summary = build_project_usage(rollouts_by_path=records, project_root=project, evals_root=evals)
    trial, = summary["harness_evals"]["sessions"]
    child, = trial["activity_tree"]["children"]
    assert child["thread_id"] == "child"
    assert child["agent_role"] == "auditor"
    assert child["parent_thread_id"] == "main"
    assert child["final_message"] == "Finding from child"
    assert child["events"][0]["command"] == "cat retained-evidence.txt"
    assert trial["subagent_token_usage"]["input_tokens"] == 10
    assert trial["worker_tokens"]["input_tokens"] == 20
    assert all(p.read_bytes() == data for p, data in originals.items())


def test_nested_capture_lineage_and_duplicate_import_are_stable(captured_trial):
    project, evals, worker, root = captured_trial
    rollout(worker / "subagents/grandchild.jsonl", "grandchild", worker.parent / "workspace", parent="child", tokens=7)
    by_tid, links = {}, {}
    _inject_captured_subagents(worker, "main", by_tid, links)
    _inject_captured_subagents(worker, "main", by_tid, links)
    assert links == {"main": ["child"], "child": ["grandchild"]}
    assert by_tid["child"]["spawn_prompt"] == "Audit retained evidence"
    summary = build_project_usage(rollouts_by_path={}, project_root=project, evals_root=evals)
    trial, = summary["harness_evals"]["sessions"]
    assert {a["thread_id"] for a in trial["subagents"]} == {"child", "grandchild"}
    assert trial["subagent_token_usage"]["input_tokens"] == 17
    nested = next(a for a in trial["subagents"] if a["thread_id"] == "grandchild")
    assert nested["depth"] == 2 and nested["parent_thread_id"] == "child"


def test_manifest_return_survives_missing_rollout(captured_trial):
    project, evals, worker, root = captured_trial
    (worker / "subagents/child.jsonl").unlink()
    (worker / "subagents/manifest.json").write_text(json.dumps({"subagents": [{
        "thread_id": "child", "parent_thread_id": "main", "depth": 1,
        "agent_role": "auditor", "rollout_captured": False,
        "final_status": "completed", "final_message": "Recorded returned finding",
    }]}))
    summary = build_project_usage(rollouts_by_path={}, project_root=project, evals_root=evals)
    trial, = summary["harness_evals"]["sessions"]
    child, = trial["subagents"]
    assert child["rollout_captured"] is False
    assert child["final_message"] == "Recorded returned finding"
    assert child["est_cost"] is None
    assert not child["events"]


def test_missing_child_stays_missing_on_repeat_and_recovers_when_file_arrives(captured_trial):
    project, evals, worker, root = captured_trial
    path = worker / "subagents/child.jsonl"
    content = path.read_bytes()
    path.unlink()
    by_tid, links = {}, {}
    for _ in range(2):
        _inject_captured_subagents(worker, "main", by_tid, links)
        assert by_tid["child"]["rollout_captured"] is False
    path.write_bytes(content)
    _inject_captured_subagents(worker, "main", by_tid, links)
    assert by_tid["child"]["rollout_captured"] is True
    assert by_tid["child"]["events"]
    assert links == {"main": ["child"]}


def test_live_child_is_enriched_without_replacing_its_usage(captured_trial):
    project, evals, worker, root = captured_trial
    live = parse_session_rollout(worker / "subagents/child.jsonl")
    live["token_usage"]["input_tokens"] = 42
    live["token_usage"]["uncached_input_tokens"] = 42
    by_tid, links = {"child": live}, {}
    _inject_captured_subagents(worker, "main", by_tid, links)
    assert by_tid["child"]["token_usage"]["input_tokens"] == 42
    assert by_tid["child"]["agent_role"] == "auditor"
    assert by_tid["child"]["spawn_prompt"] == "Audit retained evidence"
    assert links == {"main": ["child"]}


def test_parent_return_is_retained_when_no_manifest_or_child_file_exists(captured_trial):
    project, evals, worker, root = captured_trial
    (worker / "subagents/child.jsonl").unlink()
    (worker / "subagents/manifest.json").unlink()
    root["subagents"] = [{"thread_id": "child", "agent_role": "auditor", "prompt": "Review", "final_message": "Parent recorded finding"}]
    summary = build_project_usage(rollouts_by_path={"live-parent": root}, project_root=project, evals_root=evals)
    trial, = summary["harness_evals"]["sessions"]
    child, = trial["subagents"]
    assert child["rollout_captured"] is False
    assert child["final_message"] == "Parent recorded finding"
    assert child["parent_thread_id"] == "main"


@pytest.mark.parametrize("content", ["not JSON", '{"subagents": null}', '{"subagents": 3}'])
def test_bad_manifest_does_not_hide_available_rollouts(captured_trial, content):
    project, evals, worker, root = captured_trial
    (worker / "subagents/manifest.json").write_text(content)
    by_tid, links = {}, {}
    _inject_captured_subagents(worker, "main", by_tid, links)
    assert "child" in by_tid
    assert links == {"main": ["child"]}
