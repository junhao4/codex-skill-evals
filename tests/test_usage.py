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
    assert "color-scheme: light;" in html
    assert "#f6f8fb" in html
    assert "Real Project Work" in html
    assert "Harness Eval Calls" in html
    assert "unused-skill" in html
    assert "security-reviewer" in html


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
        ):
            with self.subTest(test=fn.__name__), tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))


if __name__ == "__main__":
    unittest.main()


