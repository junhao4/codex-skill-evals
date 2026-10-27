#!/usr/bin/env python3
"""Internal harness for evaluating project-local Codex skills."""

from __future__ import annotations

import hashlib
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# Ensure `_lib` is importable as a package even when `_lib` itself is on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from _lib.common import (  # noqa: E402, F401
    EXPERIMENT_NAME,
    INTERACTION_NAME,
    SKILL_NAME,
    SPINNER_FRAMES,
    TERMINAL_PROGRESS,
    Case,
    HarnessError,
    TerminalProgress,
    _fmt,
    concatenate_files,
    elapsed,
    markdown_cell,
    now,
    read_json,
    run_command,
    write_json,
)
from _lib.agent_profiles import discover_profiles, inject_profiles, profile_hashes, profile_overrides
from _lib.git_workspace import (  # noqa: E402, F401
    capture_git_evidence,
    commit_workspace_base,
    directory_digest,
    git,
    git_state,
    initialize_fixture_repository,
    remove_workspace_git_metadata,
    require_clean_repository,
    untracked_paths,
    validate_fixture,
)
from _lib.grading import (  # noqa: E402, F401
    ABSTAIN_VERDICT,
    PASSING_VERDICTS,
    PASS_THRESHOLD,
    VERDICTS,
    _grade_passed,
    combine_semantic_judges,
    grade_failure_reasons,
    grade_outcome,
    run_checker,
    run_semantic_grader,
    score_band,
    score_pass,
    validate_grade,
    validate_grade_schema,
    verdict_implies_pass,
)
from _lib.trace import (  # noqa: E402, F401
    capture_main_rollout,
    capture_subagent_rollouts,
    classify_invocation,
    find_rollout,
    merge_subagent_metrics,
    read_rollout_metrics,
    read_skill_snapshot_text,
    read_turn_context,
    sanitize_trace_jsonl,
    trace_metrics,
    trace_thread_id,
)


def discover_skills(evals_root: Path, skills_root: Path) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    if not evals_root.is_dir():
        return discovered
    for candidate in sorted(evals_root.iterdir()):
        if (
            not candidate.is_dir()
            or candidate.name.startswith("_")
            or not SKILL_NAME.fullmatch(candidate.name)
        ):
            continue
        skill_file = skills_root / candidate.name / "SKILL.md"
        test_cases = candidate / "test-cases"
        if skill_file.is_file() and test_cases.is_dir():
            discovered[candidate.name] = candidate
    return discovered


def discover_cases(eval_root: Path, skill_root: Path) -> dict[str, Case]:
    cases_root = eval_root / "test-cases"
    if not cases_root.is_dir():
        raise HarnessError(f"missing test-cases directory: {cases_root}")

    cases: dict[str, Case] = {}
    for root in sorted(cases_root.iterdir()):
        if not root.is_dir() or root.name.startswith("_"):
            continue
        prompt = root / "prompt.md"
        fixture = root / "fixture"
        expected = root / "expected.md"
        checker = root / "check.py"
        supporting_root = root / "supporting-skills"
        interactions_root = root / "interactions"
        if not prompt.is_file():
            raise HarnessError(f"test case {root.name} is missing prompt.md")
        if not fixture.is_dir():
            raise HarnessError(f"test case {root.name} is missing fixture/")
        validate_fixture(fixture)
        if not expected.is_file() and not checker.is_file():
            raise HarnessError(
                f"test case {root.name} needs expected.md, check.py, or both"
            )

        supporting_skills: list[Path] = []
        if supporting_root.exists() and not supporting_root.is_dir():
            raise HarnessError(
                f"test case {root.name} supporting-skills must be a directory"
            )
        if supporting_root.is_dir():
            for supporting in sorted(supporting_root.iterdir()):
                if supporting.name.startswith("."):
                    continue
                if not supporting.is_dir() or not SKILL_NAME.fullmatch(supporting.name):
                    raise HarnessError(
                        f"test case {root.name} has invalid supporting skill: {supporting.name}"
                    )
                if supporting.name == skill_root.name:
                    raise HarnessError(
                        f"test case {root.name} repeats the target skill under supporting-skills"
                    )
                if not (supporting / "SKILL.md").is_file():
                    raise HarnessError(
                        f"supporting skill {supporting.name} is missing SKILL.md"
                    )
                supporting_skills.append(supporting)

        interactions: list[Path] = []
        if interactions_root.exists() and not interactions_root.is_dir():
            raise HarnessError(
                f"test case {root.name} interactions must be a directory"
            )
        if interactions_root.is_dir():
            for interaction in sorted(interactions_root.iterdir()):
                if interaction.name.startswith("."):
                    continue
                if not interaction.is_file() or not INTERACTION_NAME.fullmatch(
                    interaction.name
                ):
                    raise HarnessError(
                        f"interaction {interaction.name} must match NN-description.md"
                    )
                interactions.append(interaction)

        cases[root.name] = Case(
            name=root.name,
            root=root,
            fixture=fixture,
            prompt=prompt,
            expected=expected if expected.is_file() else None,
            checker=checker if checker.is_file() else None,
            supporting_skills=tuple(supporting_skills),
            interactions=tuple(interactions),
            agent_profiles=discover_profiles(root, skill_root.parents[2]),
        )
    if not cases:
        raise HarnessError(f"no test cases found in {cases_root}")
    return cases


def next_experiment_id(runs_root: Path) -> str:
    numbers: list[int] = []
    if runs_root.is_dir():
        for candidate in runs_root.iterdir():
            match = EXPERIMENT_NAME.fullmatch(candidate.name)
            if match:
                numbers.append(int(match.group(1)))
    return f"experiment-{max(numbers, default=0) + 1:03d}"


def run_trial(
    *,
    case: Case,
    skill_root: Path,
    configuration: str,
    trial: int,
    run_dir: Path,
    schema: Path,
    semantic_enabled: bool,
    semantic_judges: int,
    timeout: int,
    model: str | None,
    semantic_deferred: bool = False,
) -> dict[str, Any]:
    # Validate before creating output, including for direct API callers.
    if run_dir.exists():
        raise HarnessError(f"refusing to overwrite run: {run_dir}")
    if configuration not in {"baseline", "with-skill"}:
        raise HarnessError(f"unknown trial configuration: {configuration}")
    validate_fixture(case.fixture)
    options = dict(case=case, skill_root=skill_root, configuration=configuration,
                   trial=trial, run_dir=run_dir, schema=schema,
                   semantic_enabled=semantic_enabled, semantic_judges=semantic_judges,
                   timeout=timeout, model=model, semantic_deferred=semantic_deferred)
    # Creation stays outside recovery: never touch an existing run after a race.
    run_dir.mkdir(parents=True, exist_ok=False)
    started = now()
    try:
        return _execute_trial(**options)
    except (Exception, KeyboardInterrupt) as error:
        try:
            _record_aborted_trial(case, skill_root.name, configuration, trial, run_dir, started, error)
        except Exception as recovery_error:
            # Recovery must not replace the original exception or interruption.
            print(f"Could not persist partial trial result at {run_dir}: {recovery_error}", file=sys.stderr)
        raise


def _record_aborted_trial(case: Case, skill: str, configuration: str, trial: int,
                          run_dir: Path, started: str, error: BaseException) -> None:
    """Persist explicit failure without grading or deleting partial evidence."""
    status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
    reason = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
    metadata = read_json(run_dir / "run.json") or {
        "skill": skill, "test_case": case.name,
        "configuration": configuration, "trial": trial, "started_at": started,
    }
    metadata.update(status=status, completed_at=now(), error=reason)
    worker = read_json(run_dir / "worker/result.json") or {}
    result = {
        "status": status, "skill": metadata.get("skill"), "test_case": case.name,
        "configuration": configuration, "trial": trial, "overall_pass": False,
        "needs_review": False, "score": None,
        "worker_exit_code": worker.get("exit_code"),
        "protected_inputs_intact": worker.get("protected_inputs_intact"),
        "worker_turns_planned": 1 + len(case.interactions),
        "worker_turns_completed": worker.get("turns_completed", len(list((run_dir / "worker/turns").glob("*/state.json")))),
        "failure_reasons": [reason], "error_type": type(error).__name__,
        "run_directory": str(run_dir), "completed_at": metadata["completed_at"],
    }
    # Keep measured worker usage even when grading or later capture failed.
    metrics = worker.get("metrics")
    if isinstance(metrics, dict):
        result["tokens"] = {key: metrics[key] for key in (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "uncached_input_tokens", "output_tokens", "reasoning_output_tokens",
            "billed_output_tokens",
        ) if key in metrics}
    if worker.get("elapsed_seconds") is not None:
        result["worker_elapsed_seconds"] = worker["elapsed_seconds"]
    for key in ("resolved_model", "resolved_reasoning_effort"):
        if metadata.get(key) is not None:
            result[key] = metadata[key]
    # Write the failure first, so later metadata/trace errors cannot leave a pass.
    write_json(run_dir / "result.json", result)
    write_json(run_dir / "run.json", metadata)
    trace = run_dir / "worker/trace.jsonl"
    if not trace.exists():
        trace.parent.mkdir(parents=True, exist_ok=True)
        concatenate_files(sorted((run_dir / "worker/turns").glob("*/trace.jsonl")), trace)
        sanitize_trace_jsonl(trace, trace)


def _execute_trial(
    *, case: Case, skill_root: Path, configuration: str, trial: int,
    run_dir: Path, schema: Path, semantic_enabled: bool, semantic_judges: int,
    timeout: int, model: str | None, semantic_deferred: bool = False,
) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    worker_dir = run_dir / "worker"
    turns_root = worker_dir / "turns"

    print("  [1/7] Copying the case-local fixture", flush=True)
    shutil.copytree(case.fixture, workspace, symlinks=True)
    validate_fixture(workspace)
    fixture_sha256 = directory_digest(case.fixture)
    fixture_revision = initialize_fixture_repository(workspace)

    print("  [2/7] Injecting controlled skills and prompts", flush=True)
    valid_configurations = {"baseline", "with-skill"}
    if configuration not in valid_configurations:
        raise HarnessError(f"unknown trial configuration: {configuration}")
    has_skill = configuration == "with-skill"
    target_skill = workspace / ".agents" / "skills" / skill_root.name
    if has_skill:
        target_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_root, target_skill)
    injected_supporting_skills: dict[str, Path] = {}
    for supporting_skill in case.supporting_skills:
        injected = workspace / ".agents" / "skills" / supporting_skill.name
        injected.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(supporting_skill, injected)
        injected_supporting_skills[supporting_skill.name] = injected
    shutil.copy2(case.prompt, workspace / "TASK.md")
    injected_profiles = inject_profiles(case.agent_profiles, workspace, run_dir)
    initial_profile_hashes = profile_hashes(injected_profiles)

    # Immutable skill snapshot (Layer 2a). Capture a readable copy of exactly the
    # skill bytes this run injected into a dedicated run-dir location, SEPARATE
    # from workspace/ so the worker can never mutate it. skill_sha256 proves two
    # runs differ; this snapshot lets a later `compare` show WHAT changed. We
    # capture supporting skills too, since those may not exist in any repo.
    snapshot_root = run_dir / "skill-snapshot"
    skill_snapshot_rel: str | None = None
    supporting_skill_snapshot_rel: dict[str, str] = {}
    if has_skill:
        skill_snapshot = snapshot_root / skill_root.name
        skill_snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_root, skill_snapshot)
        skill_snapshot_rel = str(skill_snapshot.relative_to(run_dir))
    for supporting_skill in case.supporting_skills:
        snapshot = snapshot_root / supporting_skill.name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(supporting_skill, snapshot)
        supporting_skill_snapshot_rel[supporting_skill.name] = str(
            snapshot.relative_to(run_dir)
        )

    workspace_base_revision = commit_workspace_base(workspace)
    prompt_sha256 = hashlib.sha256(case.prompt.read_bytes()).hexdigest()
    skill_sha256 = directory_digest(skill_root)
    supporting_skill_sha256 = {
        supporting.name: directory_digest(supporting)
        for supporting in case.supporting_skills
    }
    metadata = {
        "skill": skill_root.name,
        "skill_sha256": skill_sha256,
        "skill_snapshot": skill_snapshot_rel,
        "supporting_skill_snapshot": supporting_skill_snapshot_rel,
        "test_case": case.name,
        "configuration": configuration,
        "trial": trial,
        "fixture_sha256": fixture_sha256,
        "fixture_revision": fixture_revision,
        "workspace_base_revision": workspace_base_revision,
        "fixture_path": str(case.fixture),
        "prompt_sha256": prompt_sha256,
        "supporting_skills": [skill.name for skill in case.supporting_skills],
        "supporting_skill_sha256": supporting_skill_sha256,
        "agent_profiles": [p.stem for p in case.agent_profiles],
        "agent_profile_sha256": initial_profile_hashes,
        "agent_profile_snapshot": {p.stem: f"agent-profile-snapshot/{p.name}" for p in case.agent_profiles},
        "interactions": [interaction.name for interaction in case.interactions],
        "worker_turns_planned": 1 + len(case.interactions),
        "model": model or "codex-configured-default",
        "sandbox": "workspace-write",
        "semantic_grading": semantic_enabled and case.expected is not None,
        "semantic_judges": semantic_judges if semantic_enabled else 0,
        "workspace_git_metadata_removed": False,
        "initial_git_state": git_state(workspace),
        "started_at": now(),
        "status": "running",
    }
    write_json(run_dir / "run.json", metadata)

    print(
        f"  [3/7] Running the worker agent ({1 + len(case.interactions)} turn(s))",
        flush=True,
    )
    prompts = [case.prompt, *case.interactions]
    prompt_texts = [prompt.read_text(encoding="utf-8") for prompt in prompts]
    invocation = classify_invocation(has_skill, prompt_texts, skill_root.name)
    metadata["skill_invocation"] = invocation
    metadata["worker_prompt_sha256"] = hashlib.sha256(
        prompt_texts[0].encode("utf-8")
    ).hexdigest()
    write_json(run_dir / "run.json", metadata)

    turns_root.mkdir(parents=True)
    trace_parts: list[Path] = []
    worker_exit_code = 0
    completed_turns = 0
    thread_id: str | None = None
    worker_started = time.monotonic()
    for index, (prompt_file, turn_prompt) in enumerate(
        zip(prompts, prompt_texts), start=1
    ):
        turn_dir = turns_root / f"turn-{index:02d}"
        turn_dir.mkdir()
        turn_trace = turn_dir / "trace.jsonl"
        turn_stderr = turn_dir / "stderr.log"
        turn_response = turn_dir / "response.md"
        (turn_dir / "prompt.md").write_text(turn_prompt, encoding="utf-8")

        if index == 1:
            worker_command = ["codex", "exec"]
            # NOTE: previously single-turn cases used --ephemeral. We now always
            # persist the rollout so provenance (model + reasoning effort) can be
            # read back from turn_context. The harness only READS the rollout; it
            # never deletes from ~/.codex/sessions/.
            worker_command.extend(
                [
                    "--json",
                    "--sandbox",
                    "workspace-write",
                    "--skip-git-repo-check",
                    "-o",
                    str(turn_response),
                ]
            )
            if model:
                worker_command.extend(["--model", model])
            worker_command.append(turn_prompt)
        else:
            if thread_id is None:
                raise HarnessError(
                    f"worker turn 1 did not record a resumable thread ID for {case.name}"
                )
            worker_command = [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-o",
                str(turn_response),
            ]
            if model:
                worker_command.extend(["--model", model])
            worker_command.extend([thread_id, turn_prompt])

        worker_command[1:1] = profile_overrides(injected_profiles)
        worker = run_command(
            worker_command,
            cwd=workspace,
            stdout_path=turn_trace,
            stderr_path=turn_stderr,
            timeout=timeout,
            label=f"worker turn {index}/{len(prompts)} ({prompt_file.name})",
        )
        trace_parts.append(turn_trace)
        worker_exit_code = worker.returncode
        completed_turns += 1
        write_json(
            turn_dir / "state.json",
            {
                **git_state(workspace),
                "worker_exit_code": worker.returncode,
                "completed_at": now(),
            },
        )
        if index == 1:
            thread_id = trace_thread_id(turn_trace)
        if worker.returncode != 0:
            break

    worker_seconds = round(time.monotonic() - worker_started, 3)
    capture_main_rollout(
        trace_parts,
        worker_dir / "trace.jsonl",
        thread_id,
        Path.home() / ".codex" / "sessions",
    )
    metadata["worker_turns_completed"] = completed_turns
    metadata["thread_id"] = thread_id
    # Read-only provenance: read the model + reasoning effort that ACTUALLY ran
    # from the captured rollout trace. Never writes/deletes ~/.codex/sessions/.
    provenance = read_turn_context(worker_dir / "trace.jsonl")
    metadata["resolved_model"] = provenance["model"]
    metadata["resolved_reasoning_effort"] = provenance["reasoning_effort"]
    write_json(run_dir / "run.json", metadata)

    print("  [4/7] Capturing trace, metrics, Git state, and diff", flush=True)
    untracked_files = capture_git_evidence(
        workspace,
        run_dir / "git",
        workspace_base_revision,
    )
    metrics = trace_metrics(
        worker_dir / "trace.jsonl",
        skill_root.name if has_skill else None,
    )
    subagent_capture = capture_subagent_rollouts(
        worker_dir / "trace.jsonl",
        worker_dir / "subagents",
        Path.home() / ".codex" / "sessions",
    )
    metrics = merge_subagent_metrics(metrics, subagent_capture)
    metrics["skill_invocation_mode"] = invocation
    metrics["worker_elapsed_seconds"] = worker_seconds
    write_json(worker_dir / "metrics.json", metrics)
    final_state = git_state(workspace)
    final_state["worker_exit_code"] = worker_exit_code
    final_state["completed_at"] = now()
    task_intact = (workspace / "TASK.md").is_file() and hashlib.sha256(
        (workspace / "TASK.md").read_bytes()
    ).hexdigest() == prompt_sha256
    skill_intact = not has_skill or (
        target_skill.is_dir() and directory_digest(target_skill) == skill_sha256
    )
    supporting_skills_intact = {
        name: injected.is_dir()
        and directory_digest(injected) == supporting_skill_sha256[name]
        for name, injected in injected_supporting_skills.items()
    }
    all_supporting_skills_intact = all(supporting_skills_intact.values())
    profiles_intact = profile_hashes(injected_profiles) == initial_profile_hashes
    final_state["protected_inputs_intact"] = (
        task_intact and skill_intact and all_supporting_skills_intact and profiles_intact
    )
    final_state["task_prompt_intact"] = task_intact
    final_state["injected_skill_intact"] = skill_intact
    final_state["supporting_skills_intact"] = supporting_skills_intact
    final_state["agent_profiles_intact"] = profiles_intact
    worker_result = {
        "exit_code": worker_exit_code,
        "elapsed_seconds": worker_seconds,
        "turns_planned": len(prompts),
        "turns_completed": completed_turns,
        "metrics": metrics,
        "final_git_state": final_state,
        "protected_inputs_intact": final_state["protected_inputs_intact"],
        "task_prompt_intact": task_intact,
        "injected_skill_intact": skill_intact,
        "supporting_skills_intact": supporting_skills_intact,
        "agent_profiles_intact": profiles_intact,
        "untracked_files": untracked_files,
    }
    write_json(worker_dir / "result.json", worker_result)

    print("  [5/7] Running the deterministic grader", flush=True)
    deterministic = run_checker(case, workspace, run_dir, timeout)
    if deterministic["status"] == "not_requested":
        print("      skipped: this case has no check.py", flush=True)

    if semantic_enabled:
        print(
            f"  [6/7] Running {semantic_judges} semantic judge(s) in parallel",
            flush=True,
        )
        semantic = run_semantic_grader(
            case,
            run_dir,
            schema,
            timeout,
            model,
            semantic_judges,
        )
        if semantic["status"] == "not_requested":
            print("      skipped: this case has no expected.md", flush=True)
    else:
        print("  [6/7] Semantic grading", flush=True)
        semantic = {
            "status": "not_requested",
            "result": None,
            "errors": [],
            "judges": [],
        }
        if semantic_deferred:
            print(
                "      deferred: batched and parallelised after all workers",
                flush=True,
            )
        else:
            print("      skipped: disabled by --no-semantic-judges", flush=True)

    print("  [7/7] Finalizing artifacts and writing the result", flush=True)
    deterministic_result = deterministic.get("result")
    semantic_result = semantic.get("result")
    outcome = grade_outcome(deterministic, semantic)
    graders_valid = outcome["graders_valid"]
    graders_pass = outcome["graders_pass"]
    headline = outcome["headline"]
    # A trial needs review only when it did not otherwise fail for a concrete
    # reason (worker crash or protected-input tampering) and a grader abstained.
    trial_needs_review = (
        outcome["needs_review"]
        and worker_exit_code == 0
        and final_state["protected_inputs_intact"]
    )
    failure_reasons: list[str] = []
    if worker_exit_code != 0:
        failure_reasons.append(f"Worker exited with code {worker_exit_code}.")
    if not final_state["protected_inputs_intact"]:
        failure_reasons.append("The worker changed a protected harness input.")
    failure_reasons.extend(grade_failure_reasons("deterministic", deterministic))
    failure_reasons.extend(grade_failure_reasons("semantic", semantic))

    # Git is required while the worker and graders inspect the trial. Once they
    # finish, the exported git/ evidence is sufficient and workspace/ becomes a
    # normal final-state snapshot that the parent project can traverse safely.
    remove_workspace_git_metadata(workspace)
    metadata["workspace_git_metadata_removed"] = True
    metadata["status"] = "completed"
    metadata["completed_at"] = now()
    write_json(run_dir / "run.json", metadata)

    result = {
        "status": (
            "needs_review"
            if trial_needs_review
            else ("graded" if graders_valid else "ungraded")
        ),
        "skill": skill_root.name,
        "test_case": case.name,
        "configuration": configuration,
        "trial": trial,
        "overall_pass": (
            worker_exit_code == 0
            and final_state["protected_inputs_intact"]
            and graders_pass
        ),
        "needs_review": trial_needs_review,
        "score": headline.get("score") if isinstance(headline, dict) else None,
        "deterministic_score": deterministic_result.get("score")
        if isinstance(deterministic_result, dict)
        else None,
        "semantic_score": semantic_result.get("score")
        if isinstance(semantic_result, dict)
        else None,
        "deterministic_pass": deterministic_result.get("overall_pass")
        if isinstance(deterministic_result, dict)
        else None,
        "semantic_pass": semantic_result.get("overall_pass")
        if isinstance(semantic_result, dict)
        else None,
        "semantic_judges": len(semantic.get("judges", [])),
        "worker_exit_code": worker_exit_code,
        "worker_elapsed_seconds": worker_seconds,
        "tokens": {
            "input_tokens": metrics.get("input_tokens", 0),
            "cached_input_tokens": metrics.get("cached_input_tokens", 0),
            "cache_write_input_tokens": metrics.get("cache_write_input_tokens", 0),
            "uncached_input_tokens": metrics.get("uncached_input_tokens", 0),
            "output_tokens": metrics.get("output_tokens", 0),
            "reasoning_output_tokens": metrics.get("reasoning_output_tokens", 0),
            "billed_output_tokens": metrics.get("billed_output_tokens", 0),
        },
        "parent_tokens": metrics.get("parent_token_usage"),
        "subagent_tokens": metrics.get("subagent_token_usage"),
        "subagent_spawn_count": metrics.get("subagent_spawn_count", 0),
        "subagent_wait_count": metrics.get("subagent_wait_count", 0),
        "subagent_thread_ids": metrics.get("subagent_thread_ids", []),
        "model": model,
        "resolved_model": metadata.get("resolved_model"),
        "resolved_reasoning_effort": metadata.get("resolved_reasoning_effort"),
        "worker_turns_planned": len(prompts),
        "worker_turns_completed": completed_turns,
        "protected_inputs_intact": final_state["protected_inputs_intact"],
        "deterministic_status": deterministic["status"],
        "semantic_status": semantic["status"],
        "workspace_git_metadata_removed": True,
        "failure_reasons": list(dict.fromkeys(failure_reasons)),
        "run_directory": str(run_dir),
    }
    write_json(run_dir / "result.json", result)
    return result


def regrade_with_semantic(
    *,
    case: Case,
    run_dir: Path,
    schema: Path,
    timeout: int,
    model: str | None,
    judge_count: int,
) -> dict[str, Any]:
    """Add semantic grading to a trial already run without judges, and rewrite
    its result.json. Used by the parallel path so judges can be batched after
    all workers finish. Reuses run_semantic_grader and the persisted grades."""
    result = read_json(run_dir / "result.json")
    if (
        not isinstance(result, dict)
        or type(result.get("worker_exit_code")) is not int
        or not isinstance(result.get("protected_inputs_intact"), bool)
    ):
        raise HarnessError(
            f"cannot regrade without a valid trial result and worker integrity evidence: {run_dir}"
        )
    deterministic = read_json(run_dir / "grades" / "deterministic" / "result.json")
    deterministic_required = (
        (case is not None and case.checker is not None)
        or result.get("deterministic_status") not in (None, "not_requested")
    )
    if deterministic_required:
        if not deterministic or deterministic.get("status") not in ("completed", "invalid"):
            raise HarnessError(f"cannot regrade without retained deterministic evidence: {run_dir}")
        if deterministic["status"] == "completed":
            retained_grade = deterministic.get("result")
            valid, errors = validate_grade(retained_grade if isinstance(retained_grade, dict) else None)
            if not valid:
                raise HarnessError(f"cannot regrade with malformed deterministic evidence: {run_dir}: {errors}")
    if deterministic is None:
        deterministic = {"status": "not_requested", "result": None, "errors": []}
    semantic = run_semantic_grader(case, run_dir, schema, timeout, model, judge_count)

    outcome = grade_outcome(deterministic, semantic)
    graders_valid = outcome["graders_valid"]
    graders_pass = outcome["graders_pass"]
    det_result = deterministic.get("result")
    sem_result = semantic.get("result")
    headline = outcome["headline"]
    protected = result["protected_inputs_intact"]
    worker_exit = result["worker_exit_code"]
    trial_needs_review = (
        outcome["needs_review"] and worker_exit == 0 and protected
    )

    failure_reasons = [r for r in result.get("failure_reasons", []) if "semantic" not in r]
    failure_reasons.extend(grade_failure_reasons("semantic", semantic))

    result.update(
        {
            "status": (
                "needs_review"
                if trial_needs_review
                else ("graded" if graders_valid else "ungraded")
            ),
            "overall_pass": worker_exit == 0 and protected and graders_pass,
            "needs_review": trial_needs_review,
            "score": headline.get("score") if isinstance(headline, dict) else None,
            "semantic_score": sem_result.get("score") if isinstance(sem_result, dict) else None,
            "semantic_pass": sem_result.get("overall_pass") if isinstance(sem_result, dict) else None,
            "semantic_judges": len(semantic.get("judges", [])),
            "semantic_status": semantic["status"],
            "failure_reasons": list(dict.fromkeys(failure_reasons)),
        }
    )
    write_json(run_dir / "result.json", result)
    return result
