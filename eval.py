#!/usr/bin/env python3
"""List, check, and run project-local Codex skill evaluations."""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


EVALS_ROOT = Path(__file__).resolve().parent
AGENTS_ROOT = EVALS_ROOT.parent
SKILLS_ROOT = AGENTS_ROOT / "skills"
LIB_ROOT = EVALS_ROOT / "_lib"
SCHEMA = LIB_ROOT / "grade.schema.json"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_SEMANTIC_JUDGES = 3
if str(EVALS_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALS_ROOT))
if str(LIB_ROOT) not in sys.path:
    sys.path.insert(0, str(LIB_ROOT))

from _lib.common import (  # noqa: E402, F401
    HarnessError,
    _fmt,
    markdown_cell,
    now,
    read_json,
    write_json,
)
from _lib.compare import (  # noqa: E402, F401
    _axis_verdict,
    _case_representative,
    _cell,
    _confounds,
    _run_field,
    _skill_diff,
    compare_cell,
    compare_experiments as _compare_experiments,
    partition_cases,
    render_comparison_markdown,
)
from _lib.grading import (  # noqa: E402, F401
    score_band,
    validate_grade_schema,
)
from _lib.harness import (  # noqa: E402, F401
    discover_cases,
    discover_skills,
    next_experiment_id,
    regrade_with_semantic,
    run_trial,
)
from _lib.paths import SKILLS_ROOT  # noqa: E402, F401
from _lib.pricing import (  # noqa: E402, F401
    LONG_CONTEXT_THRESHOLD,
    PRICING,
    PRICING_AS_OF,
    PRICING_SOURCE,
    _select_tier,
    estimate_api_cost,
    format_cost,
    format_cost_delta,
    format_delta,
)
from _lib.report import (  # noqa: E402, F401
    TOKEN_FIELDS,
    aggregate,
    finalize_experiment,
    write_report,
)
from _lib.trace import (  # noqa: E402, F401
    capture_subagent_rollouts,
    merge_subagent_metrics,
    read_rollout_metrics,
    read_skill_snapshot_text,
)
from _lib.usage import (  # noqa: E402, F401
    build_project_usage,
    configure_codex_notify,
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


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser(
        "list", help="list skills or one skill's test cases"
    )
    list_parser.add_argument("skill", nargs="?", help="skill directory name")

    check_parser = commands.add_parser(
        "check", help="validate an evaluation without model calls"
    )
    check_parser.add_argument("skill", help="skill directory name")

    run_parser = commands.add_parser("run", help="run an evaluation")
    run_parser.add_argument("skill", help="skill directory name")
    run_parser.add_argument(
        "test_case", nargs="?", help="one test case; omit to run all"
    )
    run_parser.add_argument(
        "--compare",
        action="store_true",
        help="also run a no-skill baseline",
    )
    run_parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="worker trials per test-case/configuration cell (default: 1)",
    )
    run_parser.add_argument(
        "--judges",
        type=int,
        default=DEFAULT_SEMANTIC_JUDGES,
        help="parallel semantic judges per worker trial (default: 3)",
    )
    run_parser.add_argument(
        "--no-semantic-judges",
        action="store_true",
        help="run workers and check.py without semantic-judge calls",
    )
    run_parser.add_argument(
        "--parallel",
        action="store_true",
        help="run all workers first, then batch semantic judges in parallel",
    )

    compare_parser = commands.add_parser(
        "compare",
        help="compare two experiments of a skill (v1 vs v2); read-only",
    )
    compare_parser.add_argument("skill", help="skill directory name")
    compare_parser.add_argument("old", help="older experiment id, e.g. experiment-001")
    compare_parser.add_argument("new", help="newer experiment id, e.g. experiment-002")
    compare_parser.add_argument(
        "--config",
        choices=("with-skill", "baseline"),
        default="with-skill",
        help="configuration cell to compare across experiments (default: with-skill)",
    )

    setup_parser = commands.add_parser(
        "setup",
        help="configure .codex/config.toml notify hook and backfill project usage report",
    )
    setup_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="override detected project root",
    )
    setup_parser.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help="override ~/.codex/sessions directory",
    )
    setup_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="generate initial usage report without editing .codex/config.toml",
    )

    usage_parser = commands.add_parser(
        "usage",
        help="update project-scoped Codex session usage reports (zero model calls)",
    )
    usage_parser.add_argument(
        "notify_payload",
        nargs="?",
        default=None,
        help="optional JSON event payload appended by Codex notify hook",
    )
    usage_parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress normal stdout output (used by Codex notify hook)",
    )
    usage_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="override detected project root",
    )
    usage_parser.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help="override ~/.codex/sessions directory",
    )
    return parser.parse_args()


def load_cases(skill: str, skills: dict[str, Path]):
    if skill not in skills:
        available = ", ".join(skills) or "none"
        fail(f"no evaluation for {skill}; available: {available}")
    try:
        return discover_cases(skills[skill], SKILLS_ROOT / skill)
    except HarnessError as error:
        fail(str(error))


def list_evaluations(args: argparse.Namespace, skills: dict[str, Path]) -> int:
    if args.skill:
        cases = load_cases(args.skill, skills)
        print(f"Skill: {args.skill}\n\nTest cases:")
        for case in cases.values():
            graders = []
            if case.checker:
                graders.append("deterministic")
            if case.expected:
                graders.append("semantic")
            supporting = (
                ", ".join(skill.name for skill in case.supporting_skills) or "none"
            )
            profiles = ", ".join(p.stem for p in case.agent_profiles) or "none"
            print(
                f"  {case.name}\n"
                f"    Turns: {1 + len(case.interactions)}\n"
                f"    Supporting skills: {supporting}\n"
                f"    Agent profiles: {profiles}\n"
                f"    Graders: {' + '.join(graders)}"
            )
        return 0

    print("Available skill evaluations:")
    for name, eval_root in skills.items():
        cases = discover_cases(eval_root, SKILLS_ROOT / name)
        print(f"\n  {name}\n    Test cases: {len(cases)}")
    if not skills:
        print("\n  (none)")
    return 0


def check_evaluation(args: argparse.Namespace, skills: dict[str, Path]) -> int:
    cases = load_cases(args.skill, skills)
    schema_errors = validate_grade_schema(SCHEMA)
    if schema_errors:
        fail("; ".join(schema_errors))

    print(f"Checking: {args.skill}\n")
    for case in cases.values():
        graders = []
        if case.checker:
            graders.append("deterministic")
        if case.expected:
            graders.append("semantic")
        print(
            f"  ✓ {case.name} ({1 + len(case.interactions)} turn(s); "
            f"{len(case.agent_profiles)} agent profile(s); {' + '.join(graders)})"
        )
    print(f"\n{len(cases)} test case definition(s) structurally valid.")
    print("Graders were not executed and no model calls were made.")
    return 0


def run_evaluation(args: argparse.Namespace, skills: dict[str, Path]) -> int:
    if args.trials < 1:
        fail("--trials must be at least 1")
    if args.judges < 1:
        fail("--judges must be at least 1")

    cases = load_cases(args.skill, skills)
    if args.test_case:
        if args.test_case not in cases:
            fail(f"unknown test case: {args.test_case}")
        selected = [cases[args.test_case]]
    else:
        selected = list(cases.values())

    if args.no_semantic_judges:
        semantic_only = [case.name for case in selected if case.checker is None]
        if semantic_only:
            fail(
                "--no-semantic-judges cannot run test cases without check.py: "
                + ", ".join(semantic_only)
            )

    schema_errors = validate_grade_schema(SCHEMA)
    if schema_errors:
        fail("; ".join(schema_errors))

    configurations = ["baseline", "with-skill"] if args.compare else ["with-skill"]
    semantic_enabled = not args.no_semantic_judges
    worker_trials = len(selected) * len(configurations) * args.trials
    worker_calls = (
        sum(1 + len(case.interactions) for case in selected)
        * len(configurations)
        * args.trials
    )
    semantic_trials = sum(case.expected is not None for case in selected)
    semantic_calls = (
        semantic_trials * len(configurations) * args.trials * args.judges
        if semantic_enabled
        else 0
    )

    print(f"Skill: {args.skill}")
    print(f"Test cases: {', '.join(case.name for case in selected)}")
    print(f"Configurations: {', '.join(configurations)}")
    print(f"Worker trials per cell: {args.trials}")
    print(f"Semantic judges per worker trial: {args.judges if semantic_enabled else 0}")
    print(f"Worker trials: {worker_trials}")
    print(f"Planned calls: {worker_calls} worker turn, {semantic_calls} semantic judge")

    runs_root = skills[args.skill] / "runs"
    experiment_id = next_experiment_id(runs_root)
    experiment_root = runs_root / experiment_id
    experiment = {
        "id": experiment_id,
        "skill": args.skill,
        "test_cases": [case.name for case in selected],
        "configurations": configurations,
        "trials": args.trials,
        "worker_trials": worker_trials,
        "worker_calls": worker_calls,
        "model": "codex-configured-default",
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "semantic_grading": semantic_enabled,
        "semantic_judges": args.judges if semantic_enabled else 0,
        "started_at": now(),
        "status": "running",
    }
    write_json(experiment_root / "experiment.json", experiment)

    current = 0
    results = []
    pending_semantic = []
    try:
        if args.parallel:
            print(
                f"\n########## Phase 1: {worker_trials} worker(s) + deterministic "
                f"grading (sequential) ##########",
                flush=True,
            )
        for case in selected:
            for configuration in configurations:
                for trial in range(1, args.trials + 1):
                    current += 1
                    print(
                        f"\n=== Worker trial {current}/{worker_trials}: {case.name} / "
                        f"{configuration} / trial-{trial:02d} ===",
                        flush=True,
                    )
                    run_dir = (
                        experiment_root
                        / "trials"
                        / case.name
                        / configuration
                        / f"trial-{trial:02d}"
                    )
                    result = run_trial(
                        case=case,
                        skill_root=SKILLS_ROOT / args.skill,
                        configuration=configuration,
                        trial=trial,
                        run_dir=run_dir,
                        schema=SCHEMA,
                        semantic_enabled=semantic_enabled and not args.parallel,
                        semantic_judges=args.judges,
                        timeout=DEFAULT_TIMEOUT_SECONDS,
                        model=None,
                        semantic_deferred=(
                            args.parallel
                            and semantic_enabled
                            and case.expected is not None
                        ),
                    )
                    results.append(result)
                    if args.parallel and semantic_enabled and case.expected is not None:
                        pending_semantic.append((case, run_dir, len(results) - 1))
                    print(
                        f"      result: score={result['score']} "
                        f"pass={result['overall_pass']}",
                        flush=True,
                    )
        if pending_semantic:
            print(
                f"\n########## Phase 2: {len(pending_semantic)} semantic judge(s) "
                f"(parallel) ##########",
                flush=True,
            )
            max_parallel = min(len(pending_semantic), 8)
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                futures = {
                    executor.submit(
                        regrade_with_semantic,
                        case=case,
                        run_dir=run_dir,
                        schema=SCHEMA,
                        timeout=DEFAULT_TIMEOUT_SECONDS,
                        model=None,
                        judge_count=args.judges,
                    ): index
                    for case, run_dir, index in pending_semantic
                }
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
        if args.parallel:
            print(
                "\n########## Phase 3: writing report ##########",
                flush=True,
            )
    except KeyboardInterrupt:
        experiment["status"] = "interrupted"
        experiment["completed_at"] = now()
        _, report = finalize_experiment(experiment_root, experiment, results)
        print(f"\nInterrupted. Partial report: {report}")
        return 130
    except HarnessError as error:
        experiment["status"] = "failed"
        experiment["error"] = str(error)
        experiment["completed_at"] = now()
        _, report = finalize_experiment(experiment_root, experiment, results)
        print(f"Partial report: {report}", file=sys.stderr)
        fail(str(error))

    experiment["status"] = "completed"
    experiment["completed_at"] = now()
    aggregates, report = finalize_experiment(experiment_root, experiment, results)
    if aggregates["comparisons"]:
        print("\nBaseline comparison:")
        for test_case_name, comparison in aggregates["comparisons"].items():
            print(
                f"  {test_case_name}: {comparison['baseline_mean']} -> "
                f"{comparison['with_skill_mean']} "
                f"(delta {comparison['delta']:+})"
            )
    outcome = "PASS" if experiment["overall_pass"] else "FAIL"
    score = experiment["mean_score"]
    score_text = f"{score}/100" if isinstance(score, (int, float)) else "not available"
    print("\nEvaluation complete.")
    print(f"Result: {outcome}")
    print(f"Score: {score_text}")
    print(f"Report: {report}")
    return 0


def compare_experiments(args: argparse.Namespace, skills: dict[str, Path]) -> int:
    return _compare_experiments(args, skills, fail_fn=fail)


def setup_evaluation(args: argparse.Namespace, skills: dict[str, Path]) -> int:
    try:
        result = setup_project_usage(
            evals_root=EVALS_ROOT,
            project_root=args.project_root,
            sessions_root=args.sessions_root,
            configure_notify=not args.no_notify,
        )
    except Exception as error:
        fail(str(error))

    print(f"Project root: {result['project_root']}")
    if result["config_path"] is not None:
        print(f"Codex notify hook: {result['config_path']} ({result['notify_status']})")
    else:
        print("Codex notify hook: skipped (--no-notify)")
    print(
        f"Project sessions tracked: {result['session_count']} "
        f"({result['subagent_count']} subagent spawn(s))"
    )
    print(f"HTML report: {result['html_path']}")
    print(f"Markdown report: {result['markdown_path']}")
    print(f"Summary JSON: {result['summary_path']}\n")
    return list_evaluations(argparse.Namespace(skill=None), skills)


def run_usage(args: argparse.Namespace) -> int:
    try:
        result = generate_usage_report(
            evals_root=EVALS_ROOT,
            project_root=args.project_root,
            sessions_root=args.sessions_root,
            notify_payload=args.notify_payload,
        )
    except Exception as error:
        if args.quiet:
            return 0
        fail(str(error))

    if args.quiet:
        return 0
    if result.get("skipped"):
        print(f"Usage report skipped ({result.get('reason', 'not applicable')}).")
        return 0
    print(
        f"Project usage updated: {result['session_count']} session(s), "
        f"{result['subagent_count']} subagent spawn(s)"
    )
    print(f"HTML report: {result['html_path']}")
    print(f"Markdown report: {result['markdown_path']}")
    print(f"Summary JSON: {result['summary_path']}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "usage":
        return run_usage(args)
    skills = discover_skills(EVALS_ROOT, SKILLS_ROOT)
    if args.command == "setup":
        return setup_evaluation(args, skills)
    if args.command == "list":
        return list_evaluations(args, skills)
    if args.command == "check":
        return check_evaluation(args, skills)
    if args.command == "compare":
        return compare_experiments(args, skills)
    return run_evaluation(args, skills)


if __name__ == "__main__":
    raise SystemExit(main())
