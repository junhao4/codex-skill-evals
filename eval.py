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
sys.path.insert(0, str(LIB_ROOT))

from harness import (  # noqa: E402
    HarnessError,
    discover_cases,
    discover_skills,
    next_experiment_id,
    now,
    regrade_with_semantic,
    run_trial,
    validate_grade_schema,
    write_json,
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
            print(
                f"  {case.name}\n"
                f"    Turns: {1 + len(case.interactions)}\n"
                f"    Supporting skills: {supporting}\n"
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
            f"{' + '.join(graders)})"
        )
    print(f"\n{len(cases)} test case definition(s) structurally valid.")
    print("Graders were not executed and no model calls were made.")
    return 0


def aggregate(results: list[dict]) -> dict:
    """Summarize repeated trials and baseline-versus-skill differences."""
    TOKEN_FIELDS = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "billed_output_tokens",
    )
    cells: dict[str, dict[str, dict]] = {}
    for result in results:
        test_case = cells.setdefault(result["test_case"], {})
        cell = test_case.setdefault(
            result["configuration"],
            {
                "scores": [],
                "token_samples": [],
                "passed_trials": 0,
                "needs_review_trials": 0,
                "total_trials": 0,
            },
        )
        cell["total_trials"] += 1
        if result.get("needs_review") or result.get("status") == "needs_review":
            # Neither a pass nor a fail: an honest "not enough evidence to
            # decide". Excluded from passed_trials and from the score mean.
            cell["needs_review_trials"] += 1
            continue
        cell["passed_trials"] += int(result["overall_pass"])
        if isinstance(result["score"], (int, float)):
            cell["scores"].append(result["score"])
        tokens = result.get("tokens")
        if isinstance(tokens, dict):
            cell["token_samples"].append(tokens)

    comparisons: dict[str, dict] = {}
    for test_case_name, configurations in cells.items():
        for cell in configurations.values():
            scores = cell.pop("scores")
            cell["mean_score"] = round(sum(scores) / len(scores), 2) if scores else None
            samples = cell.pop("token_samples")
            mean_tokens: dict[str, float] = {}
            for field in TOKEN_FIELDS:
                values = [
                    s.get(field, 0) for s in samples if isinstance(s.get(field, 0), (int, float))
                ]
                mean_tokens[field] = round(sum(values) / len(values), 2) if values else 0
            cell["mean_tokens"] = mean_tokens
        baseline = configurations.get("baseline", {})
        with_skill = configurations.get("with-skill", {})
        baseline_score = baseline.get("mean_score")
        with_skill_score = with_skill.get("mean_score")
        if isinstance(baseline_score, (int, float)) and isinstance(
            with_skill_score, (int, float)
        ):
            token_delta = {
                field: round(
                    with_skill["mean_tokens"].get(field, 0)
                    - baseline["mean_tokens"].get(field, 0),
                    2,
                )
                for field in TOKEN_FIELDS
            }
            comparisons[test_case_name] = {
                "baseline_mean": baseline_score,
                "with_skill_mean": with_skill_score,
                "delta": round(with_skill_score - baseline_score, 2),
                "token_delta": token_delta,
            }
    return {"cells": cells, "comparisons": comparisons}


def markdown_cell(value: object) -> str:
    """Make a value safe for a compact Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_report(
    experiment_root: Path,
    experiment: dict,
    aggregates: dict,
    results: list[dict],
) -> Path:
    """Write the single human-facing experiment report without a model call."""
    if experiment["status"] == "completed":
        outcome = "PASS" if experiment["overall_pass"] else "FAIL"
    else:
        outcome = "INCOMPLETE"
    score = experiment.get("mean_score")
    score_text = f"{score}/100" if isinstance(score, (int, float)) else "Not available"

    # Provenance: the model + reasoning effort that actually produced these
    # numbers, read back from the rollout. Distinct resolved values across trials
    # are joined; absence is marked explicitly rather than omitted.
    def _distinct(key: str) -> str:
        vals = sorted({
            str(r.get(key)) for r in results
            if r.get(key) not in (None, "")
        })
        return ", ".join(vals) if vals else "not captured"

    model_text = _distinct("resolved_model")
    reasoning_text = _distinct("resolved_reasoning_effort")
    trials_text = str(experiment.get("trials", len(results)))
    date_text = experiment.get("started_at", "unknown")

    lines = [
        f"# Evaluation report: {experiment['skill']}",
        "",
        "## Result",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Outcome | **{outcome}** |",
        f"| Mean score | {score_text} |",
        f"| Passed trials | {experiment.get('passed_trials', 0)}/{len(results)} |",
        f"| Needs review | {experiment.get('needs_review_trials', 0)}/{len(results)} |",
        f"| Completed trials | {len(results)}/{experiment['worker_trials']} |",
        f"| Experiment status | {experiment['status']} |",
        f"| Semantic judges | {experiment.get('semantic_judges', 0)} per eligible trial |",
        f"| Model | {markdown_cell(model_text)} |",
        f"| Reasoning effort | {markdown_cell(reasoning_text)} |",
        f"| Trials per cell | {markdown_cell(trials_text)} |",
        f"| Started | {markdown_cell(date_text)} |",
        "",
        "Token counts below are only comparable across runs on the same model "
        "and reasoning effort.",
        "",
        "## Trial results",
        "",
    ]
    if results:
        lines.extend(
            [
                "| Test case | Configuration | Trial | Score | Pass | Details |",
                "|---|---|---:|---:|---:|---|",
            ]
        )
        for result in results:
            run_dir = Path(result["run_directory"])
            try:
                details = (run_dir / "result.json").relative_to(experiment_root)
            except ValueError:
                details = run_dir / "result.json"
            result_score = result.get("score")
            shown_score = (
                result_score if isinstance(result_score, (int, float)) else "—"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(result["test_case"]),
                        markdown_cell(result["configuration"]),
                        str(result["trial"]),
                        str(shown_score),
                        (
                            "Review"
                            if (
                                result.get("needs_review")
                                or result.get("status") == "needs_review"
                            )
                            else ("Yes" if result["overall_pass"] else "No")
                        ),
                        f"[result.json]({details.as_posix()})",
                    ]
                )
                + " |"
            )
    else:
        lines.append("No trial completed successfully enough to produce a result.")

    comparisons = aggregates.get("comparisons", {})
    if comparisons:
        lines.extend(
            [
                "",
                "## Baseline comparison",
                "",
                "| Test case | Baseline | With skill | Difference |",
                "|---|---:|---:|---:|",
            ]
        )
        for name, comparison in comparisons.items():
            lines.append(
                f"| {markdown_cell(name)} | {comparison['baseline_mean']} | "
                f"{comparison['with_skill_mean']} | {comparison['delta']:+} |"
            )

    cells = aggregates.get("cells", {})
    has_token_data = any(
        isinstance(cell.get("mean_tokens"), dict)
        for configs in cells.values()
        for cell in configs.values()
    )
    if has_token_data:
        lines.extend(
            [
                "",
                "## Token usage",
                "",
                "Mean tokens per trial. `billed output` = output + reasoning "
                "(reasoning bills as output). `uncached input` = input - cached. "
                "Where a baseline is compared, a negative delta means with-skill "
                "used fewer tokens. Token counts are only comparable across runs "
                "on the same model and reasoning effort.",
                "",
                "| Test case | Config | Uncached input | Cached input | "
                "Output | Reasoning | Billed output |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for name in sorted(cells):
            for config in ("baseline", "with-skill"):
                mt = cells.get(name, {}).get(config, {}).get("mean_tokens")
                if not isinstance(mt, dict):
                    continue
                lines.append(
                    f"| {markdown_cell(name)} | {config} | "
                    f"{mt.get('uncached_input_tokens', 0)} | "
                    f"{mt.get('cached_input_tokens', 0)} | "
                    f"{mt.get('output_tokens', 0)} | "
                    f"{mt.get('reasoning_output_tokens', 0)} | "
                    f"{mt.get('billed_output_tokens', 0)} |"
                )
            td = comparisons.get(name, {}).get("token_delta")
            if isinstance(td, dict):
                lines.append(
                    f"| {markdown_cell(name)} | **delta** | "
                    f"{td.get('uncached_input_tokens', 0):+} | "
                    f"{td.get('cached_input_tokens', 0):+} | "
                    f"{td.get('output_tokens', 0):+} | "
                    f"{td.get('reasoning_output_tokens', 0):+} | "
                    f"{td.get('billed_output_tokens', 0):+} |"
                )

    failed = [
        result
        for result in results
        if not result["overall_pass"]
        and not (
            result.get("needs_review") or result.get("status") == "needs_review"
        )
    ]
    if failed or experiment.get("error"):
        lines.extend(["", "## Main failure reasons", ""])
        if experiment.get("error"):
            lines.append(f"- Harness: {experiment['error']}")
        for result in failed:
            label = (
                f"{result['test_case']} / {result['configuration']} / "
                f"trial-{result['trial']:02d}"
            )
            reasons = result.get("failure_reasons") or [
                "The trial did not satisfy every configured pass condition."
            ]
            for reason in reasons:
                lines.append(f"- **{markdown_cell(label)}:** {markdown_cell(reason)}")

    lines.extend(
        [
            "",
            "## Detailed evidence",
            "",
            "Open `trials/` for each disposable workspace, worker trace, Git evidence, "
            "and configured grader outputs. `experiment.json` contains the complete "
            "machine-readable experiment data.",
            "",
        ]
    )
    report = experiment_root / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def finalize_experiment(
    experiment_root: Path,
    experiment: dict,
    results: list[dict],
) -> tuple[dict, Path]:
    """Merge final summaries into experiment.json and generate report.md."""

    def is_needs_review(result: dict) -> bool:
        return bool(result.get("needs_review")) or result.get("status") == "needs_review"

    scores = [
        result["score"]
        for result in results
        if isinstance(result.get("score"), (int, float)) and not is_needs_review(result)
    ]
    aggregates = aggregate(results)
    experiment["graded_trials"] = sum(
        result["status"] == "graded" for result in results
    )
    experiment["needs_review_trials"] = sum(
        is_needs_review(result) for result in results
    )
    # passed/overall_pass count only DECIDED trials. A needs_review trial is
    # neither a pass nor a fail: it is excluded from the pass tally and does not
    # by itself fail the experiment (it is surfaced separately for a human).
    decided = [result for result in results if not is_needs_review(result)]
    experiment["passed_trials"] = sum(result["overall_pass"] for result in decided)
    experiment["completed_trials"] = len(results)
    experiment["mean_score"] = round(sum(scores) / len(scores), 2) if scores else None
    experiment["overall_pass"] = (
        experiment["status"] == "completed"
        and len(results) == experiment["worker_trials"]
        and bool(decided)
        and all(result["overall_pass"] for result in decided)
    )
    experiment["aggregates"] = aggregates
    experiment["results"] = results
    write_json(experiment_root / "experiment.json", experiment)
    return aggregates, write_report(experiment_root, experiment, aggregates, results)


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


def main() -> int:
    args = parse_args()
    skills = discover_skills(EVALS_ROOT, SKILLS_ROOT)
    if args.command == "list":
        return list_evaluations(args, skills)
    if args.command == "check":
        return check_evaluation(args, skills)
    return run_evaluation(args, skills)


if __name__ == "__main__":
    raise SystemExit(main())
