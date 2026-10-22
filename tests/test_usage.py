"""Tests for project-scoped zero-token Codex usage tracking and setup."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.usage import (  # noqa: E402
    build_project_usage,
    configure_codex_notify,
    discover_project_inventory,
    generate_usage_report,
    is_eval_run_cwd,
    is_project_work_cwd,
    parse_session_rollout,
    render_usage_html,
    render_usage_markdown,
    resolve_project_root,
    setup_project_usage,
    update_rollout_cache,
)


def _write_rollout(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "my-project"
    evals_root = project_root / ".agents" / "evals"
    evals_root.mkdir(parents=True, exist_ok=True)
    # Create two project skills (one that will be triggered, one unused)
    skill_a = project_root / ".agents" / "skills" / "code-review" / "SKILL.md"
    skill_a.parent.mkdir(parents=True, exist_ok=True)
    skill_a.write_text("---\nname: code-review\n---\n# Code Review\n", encoding="utf-8")

    skill_b = project_root / ".agents" / "skills" / "unused-skill" / "SKILL.md"
    skill_b.parent.mkdir(parents=True, exist_ok=True)
    skill_b.write_text("---\nname: unused-skill\n---\n# Unused\n", encoding="utf-8")

    # Create two subagent profiles (one spawned, one unused)
    agent_a = project_root / ".codex" / "agents" / "security-reviewer.toml"
    agent_a.parent.mkdir(parents=True, exist_ok=True)
    agent_a.write_text('name = "security-reviewer"\n', encoding="utf-8")

    agent_b = project_root / ".codex" / "agents" / "unused-agent.toml"
    agent_b.write_text('name = "unused-agent"\n', encoding="utf-8")

    return project_root, evals_root


def test_resolve_project_root_and_cwd_scoping(tmp_path):
    project_root, evals_root = _make_project(tmp_path)
    assert resolve_project_root(evals_root) == project_root.resolve()

    # Ordinary work in project root or subdir -> included
    assert is_project_work_cwd(project_root, project_root, evals_root)
    assert is_project_work_cwd(project_root / "src" / "api", project_root, evals_root)

    # Synthetic eval trial workspace inside .agents/evals/<skill>/runs/... -> excluded
    trial_ws = (
        evals_root
        / "code-review"
        / "runs"
        / "experiment-001"
        / "trials"
        / "case-1"
        / "with-skill"
        / "trial-01"
        / "workspace"
    )
    assert is_eval_run_cwd(trial_ws, evals_root)
    assert not is_project_work_cwd(trial_ws, project_root, evals_root)

    # Unrelated project -> excluded
    other_project = tmp_path / "other-project"
    assert not is_project_work_cwd(other_project, project_root, evals_root)


def test_configure_codex_notify_creates_and_preserves_valid_toml(tmp_path):
    project_root, _ = _make_project(tmp_path)

    # 1. Existing config with [agents] table (top-level notify must be inserted before [agents])
    config_path = project_root / ".codex" / "config.toml"
    config_path.write_text(
        "[agents]\nmax_threads = 4\n",
        encoding="utf-8",
    )

    notify_cmd = ["python3", ".agents/evals/eval.py", "usage", "--quiet"]
    status = configure_codex_notify(config_path, notify_cmd)
    assert status == "updated"

    content = config_path.read_text(encoding="utf-8")
    assert 'notify = ["python3", ".agents/evals/eval.py", "usage", "--quiet"]' in content
    assert content.index("notify =") < content.index("[agents]")
    if tomllib is not None:
        parsed = tomllib.loads(content)
        assert parsed["notify"] == notify_cmd
        assert parsed["agents"]["max_threads"] == 4

    # 2. Idempotent on second call
    status2 = configure_codex_notify(config_path, notify_cmd)
    assert status2 == "unchanged"

    # 3. Creates new file when missing
    fresh_cfg = tmp_path / "fresh" / ".codex" / "config.toml"
    status3 = configure_codex_notify(fresh_cfg, notify_cmd)
    assert status3 == "created"
    assert fresh_cfg.is_file()


def test_parse_session_rollout_extracts_skills_tokens_and_subagents(tmp_path):
    project_root, _ = _make_project(tmp_path)
    rollout = _write_rollout(
        tmp_path / "sessions" / "2026" / "04" / "20" / "rollout-parent.jsonl",
        [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-parent-1",
                    "timestamp": "2026-04-20T10:00:00Z",
                    "cwd": str(project_root),
                    "cli_version": "0.120.0",
                    "source": "cli",
                    "git": {"branch": "main"},
                },
            },
            {
                "timestamp": "2026-04-20T10:00:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "cwd": str(project_root),
                    "model": "gpt-5.4",
                    "effort": "high",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Please run $code-review on the auth module",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "<skill>\n"
                                "<name>code-review</name>\n"
                                f"<path>{project_root}/.agents/skills/code-review/SKILL.md</path>\n"
                                "</skill>"
                            ),
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-04-20T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "command": ["/bin/zsh", "-lc", "pytest -q"],
                    "exit_code": 0,
                },
            },
            {
                "timestamp": "2026-04-20T10:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "collab_agent_spawn_end",
                    "call_id": "call-sub-1",
                    "sender_thread_id": "thread-parent-1",
                    "new_thread_id": "thread-child-1",
                    "new_agent_nickname": "Locke",
                    "new_agent_role": "security-reviewer",
                    "prompt": "Audit auth.py",
                    "status": {"completed": "No issues found"},
                },
            },
            {
                "timestamp": "2026-04-20T10:00:10Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10000,
                            "cached_input_tokens": 6000,
                            "output_tokens": 800,
                            "reasoning_output_tokens": 300,
                            "total_tokens": 10800,
                        }
                    },
                },
            },
        ],
    )

    parsed = parse_session_rollout(rollout)
    assert parsed is not None
    assert parsed["thread_id"] == "thread-parent-1"
    assert parsed["parent_thread_id"] is None
    assert parsed["model"] == "gpt-5.4"
    assert parsed["reasoning_effort"] == "high"
    assert parsed["turn_count"] == 1
    assert parsed["command_count"] == 1
    assert parsed["failed_command_count"] == 0
    assert parsed["first_prompt"] == "Please run $code-review on the auth module"
    assert parsed["skills_used"] == ["code-review"]
    assert parsed["skill_invocations"]["code-review"] == "explicit"
    assert parsed["token_usage"]["uncached_input_tokens"] == 4000
    assert parsed["token_usage"]["cached_input_tokens"] == 6000
    assert parsed["token_usage"]["output_tokens"] == 800
    assert parsed["token_usage"]["reasoning_output_tokens"] == 300
    assert parsed["token_usage"]["billed_output_tokens"] == 1100
    assert len(parsed["subagents"]) == 1
    assert parsed["subagents"][0]["agent_role"] == "security-reviewer"
    assert parsed["subagents"][0]["thread_id"] == "thread-child-1"


def test_build_project_usage_scopes_sessions_and_rolls_up_subagents(tmp_path):
    project_root, evals_root = _make_project(tmp_path)
    sessions_root = tmp_path / "sessions"

    # 1. Real project parent session
    _write_rollout(
        sessions_root / "2026" / "04" / "20" / "rollout-parent.jsonl",
        [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-parent-1",
                    "timestamp": "2026-04-20T10:00:00Z",
                    "cwd": str(project_root),
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:01Z",
                "type": "turn_context",
                "payload": {
                    "cwd": str(project_root),
                    "model": "gpt-5.4",
                    "effort": "medium",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Review the payment handler",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "command": [
                        "/bin/zsh",
                        "-lc",
                        f"cat {project_root}/.agents/skills/code-review/SKILL.md",
                    ],
                    "exit_code": 0,
                },
            },
            {
                "timestamp": "2026-04-20T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "collab_agent_spawn_end",
                    "call_id": "call-sub-1",
                    "sender_thread_id": "thread-parent-1",
                    "new_thread_id": "thread-child-1",
                    "new_agent_role": "security-reviewer",
                    "status": {"completed": "Done"},
                },
            },
            {
                "timestamp": "2026-04-20T10:00:10Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 5000,
                            "cached_input_tokens": 1000,
                            "output_tokens": 500,
                            "reasoning_output_tokens": 100,
                        }
                    },
                },
            },
        ],
    )

    # 2. Spawned subagent session (linked to thread-parent-1)
    _write_rollout(
        sessions_root / "2026" / "04" / "20" / "rollout-child.jsonl",
        [
            {
                "timestamp": "2026-04-20T10:00:05Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-child-1",
                    "timestamp": "2026-04-20T10:00:05Z",
                    "cwd": str(project_root),
                    "agent_role": "security-reviewer",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "thread-parent-1",
                                "depth": 1,
                                "agent_role": "security-reviewer",
                            }
                        }
                    },
                },
            },
            {
                "timestamp": "2026-04-20T10:00:06Z",
                "type": "turn_context",
                "payload": {
                    "cwd": str(project_root),
                    "model": "gpt-5.4-mini",
                    "effort": "low",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:08Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 2000,
                            "cached_input_tokens": 500,
                            "output_tokens": 200,
                            "reasoning_output_tokens": 50,
                        }
                    },
                },
            },
        ],
    )

    # 3. Synthetic eval.py run trial workspace session -> SPLIT INTO HARNESS EVALS (NOT real project work)
    trial_dir = (
        evals_root
        / "code-review"
        / "runs"
        / "experiment-001"
        / "trials"
        / "case-1"
        / "with-skill"
        / "trial-01"
    )
    trial_ws = trial_dir / "workspace"
    _write_rollout(
        sessions_root / "2026" / "04" / "20" / "rollout-eval-trial.jsonl",
        [
            {
                "timestamp": "2026-04-20T11:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-eval-trial",
                    "timestamp": "2026-04-20T11:00:00Z",
                    "cwd": str(trial_ws),
                    "source": "exec",
                },
            },
            {
                "timestamp": "2026-04-20T11:00:01Z",
                "type": "turn_context",
                "payload": {
                    "cwd": str(trial_ws),
                    "model": "gpt-5.4",
                    "effort": "high",
                },
            },
            {
                "timestamp": "2026-04-20T11:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 9000,
                            "cached_input_tokens": 1000,
                            "output_tokens": 900,
                            "reasoning_output_tokens": 100,
                        }
                    },
                },
            },
        ],
    )
    # Also write a semantic judge trace in the trial's grades directory
    judge_trace = trial_dir / "grades" / "semantic" / "judge-01" / "trace.jsonl"
    _write_rollout(
        judge_trace,
        [
            {"type": "thread.started", "thread_id": "judge-thread-01"},
            {"type": "turn.started"},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 3000,
                    "cached_input_tokens": 1000,
                    "output_tokens": 400,
                },
            },
        ],
    )

    # 4. Unrelated repository session -> MUST BE EXCLUDED
    _write_rollout(
        sessions_root / "2026" / "04" / "20" / "rollout-other-repo.jsonl",
        [
            {
                "timestamp": "2026-04-20T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-other-repo",
                    "timestamp": "2026-04-20T12:00:00Z",
                    "cwd": str(tmp_path / "unrelated-repo"),
                    "source": "cli",
                },
            },
        ],
    )

    cache_path = evals_root / "_usage" / "cache.json"
    entries = update_rollout_cache(sessions_root, cache_path)
    assert len(entries) == 4

    inventory = discover_project_inventory(project_root)
    assert set(inventory["skills"]) == {"code-review", "unused-skill"}
    assert set(inventory["agent_profiles"]) == {"security-reviewer", "unused-agent"}

    summary = build_project_usage(
        rollouts_by_path=entries,
        project_root=project_root,
        evals_root=evals_root,
    )

    # Only 1 top-level real project session should be included in `totals` (with 1 linked subagent)
    assert summary["totals"]["session_count"] == 1
    assert summary["totals"]["subagent_spawn_count"] == 1
    # Token usage rolls up parent (4000 uncached + 1000 cached + 600 billed output)
    # + child (1500 uncached + 500 cached + 250 billed output)
    tok = summary["totals"]["tokens"]
    assert tok["uncached_input_tokens"] == 5500
    assert tok["cached_input_tokens"] == 1500
    assert tok["output_tokens"] == 700
    assert tok["reasoning_output_tokens"] == 150
    assert tok["billed_output_tokens"] == 850
    assert summary["totals"]["est_cost"] > 0

    # Harness eval calls are tracked separately in `harness_evals`
    hevals = summary["harness_evals"]
    assert hevals["totals"]["worker_session_count"] == 1
    assert hevals["totals"]["judge_call_count"] == 1
    assert hevals["totals"]["total_call_count"] == 2
    assert hevals["totals"]["worker_tokens"]["uncached_input_tokens"] == 8000
    assert hevals["totals"]["worker_tokens"]["billed_output_tokens"] == 1000
    assert hevals["totals"]["judge_tokens"]["uncached_input_tokens"] == 2000
    assert hevals["totals"]["judge_tokens"]["billed_output_tokens"] == 400
    assert hevals["totals"]["tokens"]["billed_output_tokens"] == 1400
    assert len(hevals["by_skill"]) == 1
    assert hevals["by_skill"][0]["skill"] == "code-review"
    assert hevals["by_skill"][0]["worker_trials"] == 1
    assert hevals["by_skill"][0]["judge_calls"] == 1

    # Installed skills include both used (code-review) and unused (unused-skill)
    skills_by_name = {s["skill"]: s for s in summary["skills"]}
    assert skills_by_name["code-review"]["sessions_triggered"] == 1
    assert skills_by_name["code-review"]["implicit_count"] == 1
    assert skills_by_name["unused-skill"]["sessions_triggered"] == 0
    assert skills_by_name["unused-skill"]["installed"] is True

    # Configured subagents include both spawned (security-reviewer) and unused (unused-agent)
    subagents_by_role = {a["role"]: a for a in summary["subagents"]}
    assert subagents_by_role["security-reviewer"]["spawns"] == 1
    assert subagents_by_role["unused-agent"]["spawns"] == 0
    assert subagents_by_role["unused-agent"]["configured"] is True

    # Render Markdown and HTML and verify key sections exist
    md = render_usage_markdown(summary)
    assert "# Project usage report:" in md
    assert "Real Project Work" in md
    assert "Harness Eval Calls" in md
    assert "`unused-skill`" in md
    assert "`security-reviewer`" in md

    html = render_usage_html(summary)
    assert "<!doctype html>" in html.lower()
    # Prototype-E interactive two-mode explorer structure
    assert 'id="mode-real"' in html
    assert 'id="mode-eval"' in html
    assert 'id="eval-overview"' in html
    assert "function switchMode(" in html
    assert "function openEvalExperiment(" in html
    assert "function openEvalTrial(" in html
    assert "Benchmarked Skills" in html
    assert "Experiment Run History" in html
    # Content still flows through
    assert "Real Project Work" in html
    assert "Harness Eval Calls" in html
    assert "unused-skill" in html
    assert "security-reviewer" in html
    # The one evaluated skill (code-review) produces a clickable experiment
    # drill-down view, wired by a matching onclick target + element id.
    import re as _re

    called = set(
        _re.findall(r"openEval(?:Experiment|Trial)\('([^']+)'\)", html)
    )
    present = set(
        _re.findall(r"id='([^']+)' class='detail-view", html)
    )
    assert called, "expected at least one eval drill-down onclick target"
    # Every navigable target must resolve to a generated detail view.
    assert called <= present, f"unresolved onclick targets: {called - present}"


def test_setup_and_usage_cli_end_to_end(tmp_path):
    project_root, evals_root = _make_project(tmp_path)
    sessions_root = tmp_path / "sessions"
    _write_rollout(
        sessions_root / "2026" / "04" / "20" / "rollout-1.jsonl",
        [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-1",
                    "timestamp": "2026-04-20T10:00:00Z",
                    "cwd": str(project_root),
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:01Z",
                "type": "turn_context",
                "payload": {
                    "cwd": str(project_root),
                    "model": "gpt-5.4",
                    "effort": "high",
                },
            },
            {
                "timestamp": "2026-04-20T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Use $code-review on this repo",
                },
            },
        ],
    )

    result = setup_project_usage(
        evals_root=evals_root,
        project_root=project_root,
        sessions_root=sessions_root,
    )
    assert result["notify_status"] == "created"
    assert result["session_count"] == 1
    assert Path(result["html_path"]).is_file()
    assert Path(result["markdown_path"]).is_file()
    assert Path(result["summary_path"]).is_file()

    # Verify notify hook payload from synthetic eval trial is skipped fast
    trial_ws = (
        evals_root
        / "code-review"
        / "runs"
        / "experiment-001"
        / "trials"
        / "case-1"
        / "with-skill"
        / "trial-01"
        / "workspace"
    )
    skipped = generate_usage_report(
        evals_root=evals_root,
        project_root=project_root,
        sessions_root=sessions_root,
        notify_payload=json.dumps(
            {"type": "agent-turn-complete", "cwd": str(trial_ws)}
        ),
    )
    assert skipped["skipped"] is True

    # Test CLI invocation with --quiet and positional notify JSON payload
    eval_py = REPO_ROOT / "eval.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(eval_py),
            "usage",
            "--quiet",
            "--project-root",
            str(project_root),
            "--sessions-root",
            str(sessions_root),
            json.dumps({"type": "agent-turn-complete", "cwd": str(project_root)}),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == ""


def test_read_trial_grades_parses_deterministic_and_semantic(tmp_path):
    """TDD: a pure reader surfaces per-trial deterministic + semantic grades."""
    from _lib.usage import read_trial_grades

    trial_dir = tmp_path / "skill" / "runs" / "experiment-001" / "trials" / "case-1" / "with-skill" / "trial-01"
    det = trial_dir / "grades" / "deterministic" / "result.json"
    det.parent.mkdir(parents=True, exist_ok=True)
    det.write_text(json.dumps({
        "status": "completed",
        "exit_code": 0,
        "result": {
            "overall_pass": True,
            "score": 100,
            "verdict": "pass",
            "checks": [
                {"id": "summary-written", "pass": True, "points": 40, "notes": "SUMMARY.md created"},
                {"id": "source-unchanged", "pass": True, "points": 20, "notes": "no source changes"},
            ],
            "limitations": [],
        },
        "errors": [],
    }), encoding="utf-8")
    sem = trial_dir / "grades" / "semantic" / "result.json"
    sem.parent.mkdir(parents=True, exist_ok=True)
    sem.write_text(json.dumps({
        "status": "completed",
        "result": {
            "overall_pass": True,
            "verdict": "pass",
            "score": 95,
            "checks": [{"id": "semantic-jury", "pass": True, "points": 95, "notes": "consensus 95"}],
            "limitations": [],
        },
        "errors": [],
        "quorum": 1,
        "deciding_judges": 1,
        "abstaining_judges": 0,
        "judges": [
            {
                "judge": 1,
                "status": "completed",
                "result": {
                    "score": 95,
                    "verdict": "pass",
                    "checks": [
                        {"id": "accurate-summary", "pass": True, "points": 65, "notes": "accurate"},
                        {"id": "scope-respected", "pass": True, "points": 30, "notes": "in scope"},
                    ],
                },
            }
        ],
    }), encoding="utf-8")

    grades = read_trial_grades(trial_dir)

    # Deterministic grade surfaced with its checks
    assert grades["deterministic"] is not None
    assert grades["deterministic"]["score"] == 100
    assert grades["deterministic"]["verdict"] == "pass"
    det_ids = [c["id"] for c in grades["deterministic"]["checks"]]
    assert det_ids == ["summary-written", "source-unchanged"]

    # Semantic consensus + per-judge detail surfaced
    assert grades["semantic"] is not None
    assert grades["semantic"]["score"] == 95
    assert grades["semantic"]["deciding_judges"] == 1
    assert grades["semantic"]["abstaining_judges"] == 0
    assert len(grades["semantic"]["judges"]) == 1
    judge_checks = grades["semantic"]["judges"][0]["checks"]
    assert [c["id"] for c in judge_checks] == ["accurate-summary", "scope-respected"]

    # Missing grades degrade gracefully (no crash, None values)
    empty = read_trial_grades(tmp_path / "does" / "not" / "exist")
    assert empty["deterministic"] is None
    assert empty["semantic"] is None


def test_trial_grades_surface_in_summary_and_html(tmp_path):
    """TDD: per-trial grades flow into the eval session entry and render in HTML."""
    from _lib.usage import (
        build_project_usage,
        render_usage_html,
        update_rollout_cache,
    )

    project_root, evals_root = _make_project(tmp_path)
    sessions_root = tmp_path / "sessions"

    trial_dir = (
        evals_root
        / "code-review"
        / "runs"
        / "experiment-001"
        / "trials"
        / "case-1"
        / "with-skill"
        / "trial-01"
    )
    trial_ws = trial_dir / "workspace"
    _write_rollout(
        sessions_root / "2026" / "04" / "20" / "rollout-eval-trial.jsonl",
        [
            {
                "timestamp": "2026-04-20T11:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "thread-eval-trial",
                    "timestamp": "2026-04-20T11:00:00Z",
                    "cwd": str(trial_ws),
                    "source": "exec",
                },
            },
            {
                "timestamp": "2026-04-20T11:00:01Z",
                "type": "turn_context",
                "payload": {"cwd": str(trial_ws), "model": "gpt-5.4", "effort": "high"},
            },
            {
                "timestamp": "2026-04-20T11:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 9000,
                            "cached_input_tokens": 1000,
                            "output_tokens": 900,
                        }
                    },
                },
            },
        ],
    )
    det = trial_dir / "grades" / "deterministic" / "result.json"
    det.parent.mkdir(parents=True, exist_ok=True)
    det.write_text(json.dumps({
        "status": "completed",
        "result": {
            "overall_pass": True, "score": 100, "verdict": "pass",
            "checks": [{"id": "DET-CHECK-ALPHA", "pass": True, "points": 100, "notes": "ok"}],
            "limitations": [],
        },
        "errors": [],
    }), encoding="utf-8")
    sem = trial_dir / "grades" / "semantic" / "result.json"
    sem.parent.mkdir(parents=True, exist_ok=True)
    sem.write_text(json.dumps({
        "status": "completed",
        "result": {
            "overall_pass": True, "score": 88, "verdict": "pass",
            "checks": [{"id": "semantic-jury", "pass": True, "points": 88, "notes": "consensus 88"}],
            "limitations": [],
        },
        "errors": [], "quorum": 1, "deciding_judges": 1, "abstaining_judges": 0,
        "judges": [{
            "judge": 1, "status": "completed",
            "result": {"score": 88, "verdict": "pass",
                       "checks": [{"id": "SEM-CHECK-BETA", "pass": True, "points": 88, "notes": "good"}]},
        }],
    }), encoding="utf-8")

    entries = update_rollout_cache(sessions_root, evals_root / "_usage" / "cache.json")
    summary = build_project_usage(
        rollouts_by_path=entries, project_root=project_root, evals_root=evals_root
    )

    sessions = summary["harness_evals"]["sessions"]
    assert len(sessions) == 1
    grades = sessions[0]["grades"]
    assert grades["deterministic"]["score"] == 100
    assert grades["semantic"]["score"] == 88
    assert grades["semantic"]["judges"][0]["checks"][0]["id"] == "SEM-CHECK-BETA"

    # The trial-trace detail view must render the grade check ids + scores
    html = render_usage_html(summary)
    assert "Deterministic" in html
    assert "Semantic" in html
    assert "DET-CHECK-ALPHA" in html
    assert "SEM-CHECK-BETA" in html


def test_derive_experiment_comparison_baseline_vs_with_skill(tmp_path=None):
    """TDD: per-experiment baseline vs with-skill comparison from trial data."""
    from _lib.usage import derive_experiment_comparison

    def _trial(case, cfg, det_score, sem_score, billed, cost):
        return {
            "test_case": case,
            "configuration": cfg,
            "token_usage": {"billed_output_tokens": billed},
            "est_cost": cost,
            "grades": {
                "deterministic": {"score": det_score} if det_score is not None else None,
                "semantic": {"score": sem_score} if sem_score is not None else None,
            },
        }

    trials = [
        # case-1: baseline mean score 60, with-skill mean score 90; fewer tokens too
        _trial("case-1", "baseline", 60, None, 1000, 0.010),
        _trial("case-1", "baseline", 60, None, 1000, 0.010),
        _trial("case-1", "with-skill", 90, None, 800, 0.008),
        _trial("case-1", "with-skill", 90, None, 800, 0.008),
        # case-2: only with-skill (no baseline) — headline = min(det, sem) = 70
        _trial("case-2", "with-skill", 100, 70, 500, 0.005),
    ]

    comp = derive_experiment_comparison(trials)
    by_case = {c["test_case"]: c for c in comp}
    assert set(by_case) == {"case-1", "case-2"}

    c1 = by_case["case-1"]
    assert c1["baseline"]["mean_score"] == 60
    assert c1["with_skill"]["mean_score"] == 90
    assert c1["baseline"]["n"] == 2
    assert c1["with_skill"]["n"] == 2
    assert c1["baseline"]["mean_billed_output"] == 1000
    assert c1["with_skill"]["mean_billed_output"] == 800
    # Deltas present and signed (score up, tokens down = the win)
    assert "+30" in c1["score_delta"]
    assert "-200" in c1["billed_output_delta"]
    assert c1["score_verdict"] == "improved"
    assert c1["billed_output_verdict"] == "improved"  # fewer tokens is better

    c2 = by_case["case-2"]
    assert c2["baseline"] is None  # no baseline trials
    assert c2["with_skill"]["mean_score"] == 70  # min(det=100, sem=70)
    assert c2["score_delta"] == "n/a"


def test_experiment_view_renders_baseline_vs_with_skill_table(tmp_path=None):
    """TDD: experiment detail view embeds the baseline vs with-skill comparison."""
    from _lib.usage_html import render_usage_html

    def _sess(case, cfg, det_score, billed, cost):
        return {
            "thread_id": f"t-{case}-{cfg}",
            "started_at": "2026-04-20T11:00:00Z",
            "date": "2026-04-20",
            "cwd": "/x",
            "eval_skill": "demo-skill",
            "experiment": "experiment-001",
            "test_case": case,
            "configuration": cfg,
            "trial": "trial-01",
            "model": "gpt-5.4",
            "reasoning_effort": "high",
            "turn_count": 1,
            "command_count": 0,
            "failed_command_count": 0,
            "first_prompt": "do it",
            "subagent_spawn_count": 0,
            "subagents": [],
            "judge_call_count": 0,
            "judge_tokens": {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": 0, "input_tokens": 0},
            "judge_est_cost": None,
            "parent_token_usage": {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": billed, "input_tokens": billed},
            "subagent_token_usage": {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": 0, "input_tokens": 0},
            "worker_tokens": {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": billed, "input_tokens": billed},
            "worker_est_cost": cost,
            "token_usage": {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": billed, "input_tokens": billed},
            "est_cost": cost,
            "grades": {"deterministic": {"score": det_score, "checks": []}, "semantic": None},
        }

    sessions = [
        _sess("case-1", "baseline", 60, 1000, 0.010),
        _sess("case-1", "with-skill", 92, 800, 0.008),
    ]
    empty_tok = {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": 0, "input_tokens": 0}
    summary = {
        "project_name": "demo",
        "project_root": "/x",
        "generated_at": "2026-04-20T12:00:00Z",
        "totals": {
            "session_count": 0, "turn_count": 0, "command_count": 0,
            "failed_command_count": 0, "subagent_spawn_count": 0,
            "cache_hit_pct": 0.0, "parent_tokens": empty_tok, "subagent_tokens": empty_tok,
            "tokens": empty_tok, "parent_est_cost": None, "subagent_est_cost": None,
            "est_cost": None, "priced_sessions": 0,
        },
        "skills": [], "subagents": [], "models": [], "daily": [], "sessions": [],
        "harness_evals": {
            "totals": {
                "worker_session_count": 2, "subagent_spawn_count": 0,
                "judge_call_count": 0, "total_call_count": 2, "turn_count": 2,
                "worker_turn_count": 2, "judge_turn_count": 0, "command_count": 0,
                "failed_command_count": 0, "cache_hit_pct": 0.0,
                "worker_tokens": empty_tok, "judge_tokens": empty_tok, "tokens": empty_tok,
                "worker_est_cost": 0.018, "judge_est_cost": None, "est_cost": 0.018,
                "priced_calls": 2,
            },
            "by_skill": [], "subagents": [], "models": [], "sessions": sessions,
        },
        "combined_totals": {"est_cost": 0.018, "tokens": empty_tok, "cache_hit_pct": 0.0},
    }

    html = render_usage_html(summary)
    assert "Baseline vs With-Skill" in html
    assert "case-1" in html
    # score improved 60 -> 92 (+32), billed output dropped 1000 -> 800 (-200)
    assert "+32" in html
    assert "-200" in html
    # Outcome KPI card: both trials graded (det 60 baseline, 92 with-skill);
    # the baseline trial (60) is below threshold 70 -> experiment FAILs, and the
    # det mean (76) is surfaced.
    assert "Outcome" in html
    assert "FAIL" in html
    # Setup KPI card surfaces the model + reasoning effort from the trials
    assert "Setup" in html
    assert "gpt-5.4" in html
    assert "high" in html
    # "Open raw report.md" link points at the experiment's report on disk
    assert "report.md" in html
    assert "../demo-skill/runs/experiment-001/report.md" in html


def test_summarize_experiment_outcome(tmp_path=None):
    """TDD: experiment outcome/score rollup from per-trial grades."""
    from _lib.usage import summarize_experiment_outcome

    def _trial(det, sem):
        return {
            "grades": {
                "deterministic": {"score": det} if det is not None else None,
                "semantic": {"score": sem} if sem is not None else None,
            }
        }

    # Two trials, headline = min(det, sem): (100,90)->90, (80,100)->80; mean 85 -> PASS
    trials = [_trial(100, 90), _trial(80, 100)]
    out = summarize_experiment_outcome(trials)
    assert out["mean_score"] == 85.0
    assert out["mean_det"] == 90.0  # (100 + 80) / 2
    assert out["mean_sem"] == 95.0  # (90 + 100) / 2
    assert out["trials_total"] == 2
    assert out["trials_passed"] == 2  # both headline scores >= 70
    assert out["outcome"] == "PASS"

    # A failing trial drags the outcome
    fail_trials = [_trial(100, 100), _trial(40, 100)]  # headlines 100, 40
    out2 = summarize_experiment_outcome(fail_trials)
    assert out2["mean_score"] == 70.0
    assert out2["trials_passed"] == 1  # only the 100 trial clears 70
    assert out2["outcome"] == "FAIL"  # one trial below threshold

    # No grades at all -> unknown, no crash
    out3 = summarize_experiment_outcome([{"grades": {"deterministic": None, "semantic": None}}])
    assert out3["mean_score"] is None
    assert out3["outcome"] == "UNKNOWN"


def test_benchmarked_skills_table_shows_latest_outcome(tmp_path=None):
    """TDD: the Benchmarked Skills overview table shows a latest outcome/score."""
    from _lib.usage_html import render_usage_html

    empty_tok = {"uncached_input_tokens": 0, "cached_input_tokens": 0, "billed_output_tokens": 0, "input_tokens": 0}

    def _sess(exp, cfg, det):
        return {
            "thread_id": f"t-{exp}-{cfg}", "started_at": f"2026-04-20T11:00:0{exp[-1]}Z",
            "date": "2026-04-20", "cwd": "/x", "eval_skill": "demo-skill",
            "experiment": exp, "test_case": "case-1", "configuration": cfg, "trial": "trial-01",
            "model": "gpt-5.4", "reasoning_effort": "high", "turn_count": 1,
            "command_count": 0, "failed_command_count": 0, "first_prompt": "p",
            "subagent_spawn_count": 0, "subagents": [], "judge_call_count": 0,
            "judge_tokens": empty_tok, "judge_est_cost": None,
            "parent_token_usage": empty_tok, "subagent_token_usage": empty_tok,
            "worker_tokens": empty_tok, "worker_est_cost": 0.001,
            "token_usage": empty_tok, "est_cost": 0.001,
            "grades": {"deterministic": {"score": det, "checks": []}, "semantic": None},
        }

    # Two experiments; the LATEST (experiment-002) failed (score 40)
    sessions = [_sess("experiment-001", "with-skill", 100), _sess("experiment-002", "with-skill", 40)]
    by_skill = [{
        "skill": "demo-skill", "experiments": ["experiment-001", "experiment-002"],
        "experiment_count": 2, "worker_trials": 2, "baseline_trials": 0, "with_skill_trials": 2,
        "subagent_spawns": 0, "judge_calls": 0,
        "worker_tokens": empty_tok, "judge_tokens": empty_tok, "tokens": empty_tok,
        "worker_est_cost": 0.002, "judge_est_cost": None, "est_cost": 0.002, "priced_count": 2,
    }]
    summary = {
        "project_name": "demo", "project_root": "/x", "generated_at": "2026-04-20T12:00:00Z",
        "totals": {
            "session_count": 0, "turn_count": 0, "command_count": 0, "failed_command_count": 0,
            "subagent_spawn_count": 0, "cache_hit_pct": 0.0, "parent_tokens": empty_tok,
            "subagent_tokens": empty_tok, "tokens": empty_tok, "parent_est_cost": None,
            "subagent_est_cost": None, "est_cost": None, "priced_sessions": 0,
        },
        "skills": [], "subagents": [], "models": [], "daily": [], "sessions": [],
        "harness_evals": {
            "totals": {
                "worker_session_count": 2, "subagent_spawn_count": 0, "judge_call_count": 0,
                "total_call_count": 2, "turn_count": 2, "worker_turn_count": 2, "judge_turn_count": 0,
                "command_count": 0, "failed_command_count": 0, "cache_hit_pct": 0.0,
                "worker_tokens": empty_tok, "judge_tokens": empty_tok, "tokens": empty_tok,
                "worker_est_cost": 0.002, "judge_est_cost": None, "est_cost": 0.002, "priced_calls": 2,
            },
            "by_skill": by_skill, "subagents": [], "models": [], "sessions": sessions,
        },
        "combined_totals": {"est_cost": 0.002, "tokens": empty_tok, "cache_hit_pct": 0.0},
    }

    html = render_usage_html(summary)
    assert "Latest Outcome" in html
    # The latest experiment (002) failed -> FAIL badge somewhere in the skills table
    assert "FAIL" in html
    # The latest-eval spotlight banner shows the pointed-at experiment's outcome
    assert "LATEST EVAL" in html
    # banner outcome badge: experiment-001 (sessions[0]) scored 100 -> PASS
    assert "PASS" in html


import tempfile  # noqa: E402
import unittest  # noqa: E402


class UsageTests(unittest.TestCase):
    def test_all_usage_scenarios(self):
        for fn in (
            test_resolve_project_root_and_cwd_scoping,
            test_configure_codex_notify_creates_and_preserves_valid_toml,
            test_parse_session_rollout_extracts_skills_tokens_and_subagents,
            test_build_project_usage_scopes_sessions_and_rolls_up_subagents,
            test_setup_and_usage_cli_end_to_end,
            test_read_trial_grades_parses_deterministic_and_semantic,
            test_trial_grades_surface_in_summary_and_html,
            test_derive_experiment_comparison_baseline_vs_with_skill,
            test_experiment_view_renders_baseline_vs_with_skill_table,
            test_summarize_experiment_outcome,
            test_benchmarked_skills_table_shows_latest_outcome,
        ):
            with self.subTest(test=fn.__name__), tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))


if __name__ == "__main__":
    unittest.main()


