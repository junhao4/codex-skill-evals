"""Exercise the real trial workflow; replace only the external Codex process."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _lib import grading, harness
from _lib.common import Case, HarnessError, run_command


SCHEMA = Path(__file__).resolve().parents[1] / "_lib" / "grade.schema.json"


def grade(score=100, verdict="pass"):
    return {"overall_pass": score >= 70, "verdict": verdict, "score": score,
            "checks": [{"id": "result", "pass": score >= 70, "points": score, "notes": "Observed fixture result"}],
            "limitations": []}


@pytest.fixture
def trial_case(tmp_path):
    root = tmp_path / "project"
    skill = root / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Demo skill instructions")
    case_root = root / ".agents" / "evals" / "demo" / "test-cases" / "case"
    fixture = case_root / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "source.txt").write_text("original")
    prompt = case_root / "prompt.md"
    prompt.write_text("Use $demo and update source.txt")
    checker = case_root / "check.py"
    checker.write_text("import json\nprint(json.dumps(" + repr(grade()) + "))\n")
    support = case_root / "supporting-skills" / "helper"
    support.mkdir(parents=True)
    (support / "SKILL.md").write_text("Supporting instructions")
    profile = root / ".codex" / "agents" / "reviewer.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text('model = "example-model"\n')
    case = Case("case", case_root, fixture, prompt, None, checker, (support,), (), (profile,))
    return case, skill, tmp_path / "run"


def simulate_worker(monkeypatch, *, exits=(0,), mutation=None, thread=True):
    calls = []

    def run(command, *, cwd, stdout_path, stderr_path, **kwargs):
        index = len(calls)
        calls.append(command)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        events = []
        if thread:
            events.append({"type": "thread.started", "thread_id": "integration-fake-thread"})
        events.append({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}})
        stdout_path.write_text("\n".join(map(json.dumps, events)) + "\n")
        stderr_path.write_text("")
        if mutation:
            mutation(cwd)
        else:
            (cwd / "source.txt").write_text("updated")
            (cwd / "new.txt").write_text("untracked evidence")
        return subprocess.CompletedProcess(command, exits[min(index, len(exits) - 1)])

    monkeypatch.setattr(harness, "run_command", run)
    # Do not consult real user sessions for synthetic test thread IDs.
    monkeypatch.setattr(harness, "capture_main_rollout", lambda sources, dest, *args: harness.concatenate_files(sources, dest))
    return calls


def run_case(data, **overrides):
    case, skill, run_dir = data
    return harness.run_trial(case=case, skill_root=skill, configuration="with-skill", trial=1,
                             run_dir=run_dir, schema=SCHEMA, semantic_enabled=False,
                             semantic_judges=1, timeout=10, model="example-model", **overrides)


def test_discovery_connects_case_skills_and_explicit_agent_profiles(trial_case):
    case, skill, _ = trial_case
    (case.root / "agent-profiles.json").write_text('["reviewer"]')
    skills = harness.discover_skills(case.root.parents[2], skill.parent)
    assert skills == {"demo": case.root.parents[1]}
    discovered = harness.discover_cases(skills["demo"], skill)["case"]
    assert discovered.agent_profiles == case.agent_profiles
    assert discovered.supporting_skills == case.supporting_skills


@pytest.mark.parametrize("path", [".git", ".agents", ".codex"])
def test_discovery_rejects_fixture_control_directories(trial_case, path):
    case, skill, _ = trial_case
    (case.fixture / path).mkdir()
    with pytest.raises(HarnessError):
        harness.discover_cases(case.root.parents[1], skill)


@pytest.mark.parametrize("names", [["../outside"], ["reviewer", "reviewer"], ["missing"]])
def test_discovery_rejects_invalid_profile_dependencies(trial_case, names):
    case, skill, _ = trial_case
    (case.root / "agent-profiles.json").write_text(json.dumps(names))
    with pytest.raises(HarnessError):
        harness.discover_cases(case.root.parents[1], skill)


def test_process_timeout_preserves_partial_diagnostics(tmp_path):
    result = run_command([sys.executable, "-c", 'import time; print("partial evidence", flush=True); time.sleep(5)'],
                         cwd=tmp_path, stdout_path=tmp_path / "output", stderr_path=tmp_path / "errors",
                         timeout=0.2, label="timeout regression test")
    assert result.returncode == 124
    assert "partial evidence" in (tmp_path / "output").read_text()


def test_missing_process_is_explicit_failure(tmp_path):
    result = run_command([str(tmp_path / "absent-program")], cwd=tmp_path,
                         stdout_path=tmp_path / "output", stderr_path=tmp_path / "errors",
                         timeout=1, label="missing process regression test")
    assert result.returncode == 127


@pytest.mark.parametrize("configuration", ["baseline", "with-skill"])
def test_trial_exports_evidence_and_preserves_source_inputs(trial_case, monkeypatch, configuration):
    calls = simulate_worker(monkeypatch)
    case, skill, run_dir = trial_case
    result = harness.run_trial(case=case, skill_root=skill, configuration=configuration, trial=1,
                               run_dir=run_dir, schema=SCHEMA, semantic_enabled=False,
                               semantic_judges=1, timeout=10, model="example-model")
    assert result["overall_pass"] and result["protected_inputs_intact"]
    assert result["tokens"]["input_tokens"] == 100
    assert result["worker_turns_completed"] == 1
    assert (case.fixture / "source.txt").read_text() == "original"
    assert not (run_dir / "workspace" / ".git").exists()
    assert "updated" in (run_dir / "git" / "changes.patch").read_text()
    assert "untracked evidence" in (run_dir / "git" / "untracked.patch").read_text()
    assert (run_dir / "agent-profile-snapshot" / "reviewer.toml").is_file()
    assert (run_dir / "workspace" / ".agents" / "skills" / "demo").exists() == (configuration == "with-skill")
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["TASK.md", ".agents/skills/demo/SKILL.md", ".agents/skills/helper/SKILL.md", ".codex/agents/reviewer.toml"])
def test_protected_input_tampering_fails_even_when_grader_passes(trial_case, monkeypatch, path):
    simulate_worker(monkeypatch, mutation=lambda cwd: (cwd / path).write_text("tampered"))
    result = run_case(trial_case)
    assert result["score"] == 100
    assert not result["overall_pass"] and not result["protected_inputs_intact"]
    assert any("protected" in reason for reason in result["failure_reasons"])


@pytest.mark.parametrize("exit_code", [1, 124, 127])
def test_worker_failure_cannot_be_overridden_by_passing_grade(trial_case, monkeypatch, exit_code):
    calls = simulate_worker(monkeypatch, exits=(exit_code,))
    case = trial_case[0]
    interaction = case.root / "02-follow-up.md"
    interaction.write_text("Continue")
    case = Case(case.name, case.root, case.fixture, case.prompt, case.expected, case.checker,
                case.supporting_skills, (interaction,), case.agent_profiles)
    result = run_case((case, *trial_case[1:]))
    assert not result["overall_pass"]
    assert result["worker_exit_code"] == exit_code
    assert result["worker_turns_completed"] == 1 and len(calls) == 1


def test_multiple_turns_resume_same_thread_and_sum_tokens_once(trial_case, monkeypatch):
    calls = simulate_worker(monkeypatch, exits=(0, 0))
    case = trial_case[0]
    interaction = case.root / "02-follow-up.md"
    interaction.write_text("Continue")
    case = Case(case.name, case.root, case.fixture, case.prompt, case.expected, case.checker,
                case.supporting_skills, (interaction,), case.agent_profiles)
    result = run_case((case, *trial_case[1:]))
    assert result["overall_pass"] and result["worker_turns_completed"] == 2
    assert result["tokens"]["input_tokens"] == 200
    assert result["tokens"]["billed_output_tokens"] == 20
    assert "resume" in calls[1] and "integration-fake-thread" in calls[1]
    assert calls[1].count("--model") == 1


@pytest.mark.parametrize("evidence", [None, {}, {"worker_exit_code": 0}, {"protected_inputs_intact": True}, {"worker_exit_code": False, "protected_inputs_intact": True}])
def test_regrade_refuses_missing_worker_evidence(tmp_path, monkeypatch, evidence):
    if evidence is not None:
        (tmp_path / "result.json").write_text(json.dumps(evidence))
    monkeypatch.setattr(harness, "run_semantic_grader", lambda *args: {"status": "completed", "result": grade(), "judges": [1], "errors": []})
    with pytest.raises(HarnessError, match="worker|trial result"):
        harness.regrade_with_semantic(case=None, run_dir=tmp_path, schema=SCHEMA,
                                     timeout=10, model=None, judge_count=1)


@pytest.mark.parametrize("worker_exit, protected", [(1, True), (0, False)])
def test_regrade_cannot_erase_worker_failure_or_tampering(tmp_path, monkeypatch, worker_exit, protected):
    (tmp_path / "result.json").write_text(json.dumps({"worker_exit_code": worker_exit, "protected_inputs_intact": protected}))
    monkeypatch.setattr(harness, "run_semantic_grader", lambda *args: {"status": "completed", "result": grade(), "judges": [1], "errors": []})
    result = harness.regrade_with_semantic(case=None, run_dir=tmp_path, schema=SCHEMA, timeout=10, model=None, judge_count=1)
    assert result["score"] == 100
    assert not result["overall_pass"]


@pytest.mark.parametrize("output", ["[]", "not json", json.dumps(grade()) + "\\ninvalid trailing data"])
def test_checker_runner_rejects_bad_output(trial_case, output):
    case, _, run_dir = trial_case
    case.checker.write_text("print(" + repr(output) + ")\n")
    result = grading.run_checker(case, case.fixture, run_dir, 10)
    assert result["status"] == "invalid" and result["result"] is None


def test_checker_nonzero_exit_cannot_pass_with_valid_json(trial_case):
    case, _, run_dir = trial_case
    case.checker.write_text("import json\nprint(json.dumps(" + repr(grade()) + "))\nraise SystemExit(2)\n")
    result = grading.run_checker(case, case.fixture, run_dir, 10)
    assert result["status"] == "invalid" and result["exit_code"] == 2


@pytest.mark.parametrize("modes, expected", [
    (["pass", "pass", "crash"], "completed"),
    (["pass", "invalid", "crash"], "invalid"),
    (["abstain", "abstain", "pass"], "needs_review"),
    (["invalid", "invalid", "invalid"], "invalid"),
])
def test_semantic_runner_handles_bad_judges_without_losing_others(trial_case, monkeypatch, modes, expected):
    case, _, run_dir = trial_case
    rubric = case.root / "expected.md"
    rubric.write_text("Evaluate the result")
    case = Case(case.name, case.root, case.fixture, case.prompt, rubric, case.checker,
                case.supporting_skills, case.interactions, case.agent_profiles)
    def run(command, *, stdout_path, **kwargs):
        index = int(stdout_path.parent.name.split("-")[-1]) - 1
        mode = modes[index]
        if mode == "crash":
            raise RuntimeError("simulated judge process failure")
        output = Path(command[command.index("-o") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("not json" if mode == "invalid" else json.dumps(grade(verdict="insufficient_evidence" if mode == "abstain" else "pass")))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(grading, "run_command", run)
    result = grading.run_semantic_grader(case, run_dir, SCHEMA, 10, None, len(modes))
    assert result["status"] == expected
    assert len(result["judges"]) == len(modes)
    assert (run_dir / "grades" / "semantic" / "result.json").is_file()


@pytest.mark.parametrize('artifact', [None, {}, {'status': 'completed', 'result': {'score': 100}}, {'status': 'not_requested', 'result': None}])
def test_regrade_requires_retained_deterministic_evidence(trial_case, monkeypatch, artifact):
    case, _, run_dir = trial_case
    run_dir.mkdir()
    (run_dir / 'result.json').write_text(json.dumps({
        'worker_exit_code': 0, 'protected_inputs_intact': True,
        'deterministic_status': 'completed', 'overall_pass': False,
    }))
    if artifact is not None:
        target = run_dir / 'grades/deterministic/result.json'
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(artifact))
    calls = []
    monkeypatch.setattr(harness, 'run_semantic_grader', lambda *args: calls.append(args))
    with pytest.raises(HarnessError, match='deterministic'):
        harness.regrade_with_semantic(case=case, run_dir=run_dir, schema=SCHEMA,
                                     timeout=10, model=None, judge_count=1)
    assert not calls
    assert not json.loads((run_dir / 'result.json').read_text())['overall_pass']


def test_checker_cannot_turn_abstention_into_passing_score(trial_case):
    case, _, run_dir = trial_case
    case.checker.write_text('import json\nprint(json.dumps(' + repr(grade(verdict='insufficient_evidence')) + '))\n')
    result = grading.run_checker(case, case.fixture, run_dir, 10)
    assert result['status'] == 'invalid'
    assert any('abstain' in error for error in result['errors'])


@pytest.mark.parametrize('det_status, det_score, expected_pass', [('completed', 100, True), ('completed', 0, False), ('invalid', 100, False)])
def test_regrade_preserves_retained_checker_decision(trial_case, monkeypatch, det_status, det_score, expected_pass):
    case, _, run_dir = trial_case
    run_dir.mkdir()
    (run_dir / 'result.json').write_text(json.dumps({
        'worker_exit_code': 0, 'protected_inputs_intact': True,
        'deterministic_status': det_status, 'overall_pass': False,
    }))
    target = run_dir / 'grades/deterministic/result.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'status': det_status, 'result': grade(det_score), 'errors': []}))
    monkeypatch.setattr(harness, 'run_semantic_grader', lambda *args: {'status': 'completed', 'result': grade(), 'judges': [], 'errors': []})
    result = harness.regrade_with_semantic(case=case, run_dir=run_dir, schema=SCHEMA,
                                         timeout=10, model=None, judge_count=1)
    assert result['overall_pass'] is expected_pass


@pytest.mark.parametrize('kind', ['external-file', 'external-dir', 'absolute-internal', 'relative-internal', 'dangling'])
def test_fixture_symlinks_are_rejected_before_copy_or_execution(trial_case, monkeypatch, kind):
    case, skill, run_dir = trial_case
    outside = run_dir.parent / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('source must stay unchanged')
    targets = {'external-file': outside / 'secret.txt', 'external-dir': outside,
               'absolute-internal': case.fixture / 'source.txt',
               'relative-internal': Path('source.txt'), 'dangling': Path('missing.txt')}
    (case.fixture / 'link').symlink_to(targets[kind])
    calls = simulate_worker(monkeypatch)
    with pytest.raises(HarnessError, match='symlink'):
        harness.discover_cases(case.root.parents[1], skill)
    with pytest.raises(HarnessError, match='symlink'):
        run_case(trial_case)
    assert not run_dir.exists()
    assert not calls
    assert (outside / 'secret.txt').read_text() == 'source must stay unchanged'









def test_nested_fixture_directory_link_is_rejected(trial_case, monkeypatch):
    case, _, run_dir = trial_case
    nested = case.fixture / 'nested'
    nested.mkdir()
    (nested / 'cycle').symlink_to('..')
    simulate_worker(monkeypatch)
    with pytest.raises(HarnessError, match='symlink'):
        run_case(trial_case)
    assert not run_dir.exists()


def test_fixture_root_symlink_is_rejected(trial_case, monkeypatch):
    case, skill, run_dir = trial_case
    alias = case.root / 'fixture-alias'
    alias.symlink_to(case.fixture)
    linked_case = Case(case.name, case.root, alias, case.prompt, case.expected,
                       case.checker, case.supporting_skills, case.interactions, case.agent_profiles)
    simulate_worker(monkeypatch)
    with pytest.raises(HarnessError, match='symlink'):
        run_case((linked_case, skill, run_dir))
    assert not run_dir.exists()
