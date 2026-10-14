#!/usr/bin/env python3
"""List, check, and run project-local Codex skill evaluations."""

from __future__ import annotations

import argparse
import difflib
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
    read_json,
    read_skill_snapshot_text,
    regrade_with_semantic,
    run_trial,
    score_band,
    validate_grade_schema,
    write_json,
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


# --- API-equivalent cost estimation (Layer 1c) --------------------------------
#
# IMPORTANT HONESTY NOTE. These rates are PUBLIC OpenAI *API* list prices. Many
# users (including ChatGPT Plus / Edu / Enterprise subscription or credit-pool
# accounts) are NOT billed per token at these rates at all. For those users this
# figure is a HYPOTHETICAL "what an API user would pay" number — useful only as a
# model-agnostic unit for comparing one run against another. It is NEVER the
# user's actual subscription or credit spend. Every surface that prints it MUST
# carry that caveat. This mirrors how Langfuse/Braintrust label cost as an
# *estimate* derived from a price table times token usage.
#
# Keep this table pinned and dated. Rates are USD per 1,000,000 tokens, from the
# STANDARD processing tier of platform.openai.com/pricing. Each model carries
# BOTH context tiers the card lists: "short" and "long". A request is billed at
# the long-context rate once its input context crosses LONG_CONTEXT_THRESHOLD
# tokens, and crossing re-prices the WHOLE request (a cliff, not a marginal
# rate) — per OpenAI's note that standard rates apply "for context lengths under
# 272K". (The card also lists Batch/Flex/Fast processing tiers, not encoded.)
# Update PRICING_AS_OF when you refresh the numbers.
PRICING_AS_OF = "2026-10-12"
PRICING_SOURCE = (
    "platform.openai.com/pricing (API list prices, Standard tier, "
    "short+long context)"
)
# Input context size (uncached + cached) at/above which the long-context rate
# applies to the whole request. OpenAI states standard rates apply under ~272K.
LONG_CONTEXT_THRESHOLD = 272_000
# model id -> {"short"/"long": (input, cached, cache_write, output)} per 1M.
# reasoning tokens bill as output, so billed_output uses the output rate.
# cache_write None means the card lists no separate cache-write rate. A model
# may omit a tier (e.g. its long-context column was not published/legible); the
# estimator falls back to the tier that exists.
PRICING: dict[str, dict[str, tuple[float, float | None, float | None, float]]] = {
    "gpt-6-astra": {
        "short": (10.00, 1.00, 12.50, 50.00),
        "long": (20.00, 2.00, 25.00, 75.00),
    },
    "gpt-6.1-sol": {
        "short": (2.00, 0.10, 2.50, 10.00),
        "long": (4.00, 0.20, 5.00, 15.00),
    },
    "gpt-6-luna": {
        "short": (0.10, 0.01, 0.125, 0.50),
        "long": (0.20, 0.02, 0.25, 0.75),
    },
    "gpt-6-sol": {
        "short": (2.00, 0.20, 2.50, 10.00),
        "long": (4.00, 0.40, 5.00, 15.00),
    },
    "gpt-5.6-sol": {
        "short": (4.00, 0.40, 5.00, 20.00),
        "long": (8.00, 0.80, 10.00, 30.00),
    },
    "gpt-5.6-terra": {
        "short": (2.00, 0.20, 2.50, 12.00),
        "long": (4.00, 0.40, 5.00, 18.00),
    },
    "gpt-5.6-luna": {
        "short": (0.20, 0.02, 0.25, 1.20),
        "long": (0.40, 0.04, 0.50, 1.80),
    },
    "gpt-5.5": {
        "short": (5.00, 0.50, None, 30.00),
        "long": (10.00, 1.00, None, 45.00),
    },
    "gpt-5.5-pro": {
        "short": (30.00, None, None, 180.00),
        "long": (60.00, None, None, 270.00),
    },
    # gpt-5.4 long-context column was not fully legible on the source; short only.
    "gpt-5.4": {
        "short": (2.50, 0.25, None, 15.00),
    },
}


def _select_tier(model_rates: dict, input_context: float) -> tuple:
    """Pick the (input, cached, cache_write, output) tuple for a request's input
    context size. Uses long-context rates at/above the threshold; falls back to
    whichever tier exists when a model omits one."""
    want = "long" if input_context >= LONG_CONTEXT_THRESHOLD else "short"
    if want in model_rates:
        return model_rates[want]
    # Fall back to the only tier published for this model.
    other = "short" if want == "long" else "long"
    return model_rates[other]


def estimate_api_cost(mean_tokens: dict, model: str | None) -> float | None:
    """Pure: estimate API-equivalent USD cost for one trial's mean token usage.

    Returns None when the model is unknown or not supplied (never guess). Picks
    the short- or long-context rate tier from the request's input context size
    (uncached + cached), then prices uncached-input at the input rate,
    cached-input at the cached rate, cache-write input at the cache-write rate
    (when the card lists one), and billed-output (output + reasoning) at the
    output rate.
    """
    if not model:
        return None
    model_rates = PRICING.get(model)
    if model_rates is None:
        return None
    uncached = mean_tokens.get("uncached_input_tokens", 0) or 0
    cached = mean_tokens.get("cached_input_tokens", 0) or 0
    cache_write = mean_tokens.get("cache_write_input_tokens", 0) or 0
    billed_out = mean_tokens.get("billed_output_tokens", 0) or 0
    input_context = uncached + cached
    input_rate, cached_rate, cache_write_rate, output_rate = _select_tier(
        model_rates, input_context
    )
    cost = (
        uncached * input_rate
        + cached * (cached_rate or 0)
        + cache_write * (cache_write_rate or 0)
        + billed_out * output_rate
    ) / 1_000_000
    return round(cost, 6)


def format_cost(cost: float | None) -> str:
    """Render a cost estimate, or a clear 'n/a' when the model is unpriced."""
    if cost is None:
        return "n/a (model not in price table)"
    return f"${cost:.4f}"


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
                "det_scores": [],
                "sem_scores": [],
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
        if isinstance(result.get("score"), (int, float)) and not isinstance(
            result.get("score"), bool
        ):
            cell["scores"].append(result["score"])
        det_s = result.get("deterministic_score")
        if isinstance(det_s, (int, float)) and not isinstance(det_s, bool):
            cell["det_scores"].append(det_s)
        sem_s = result.get("semantic_score")
        if isinstance(sem_s, (int, float)) and not isinstance(sem_s, bool):
            cell["sem_scores"].append(sem_s)
        tokens = result.get("tokens")
        if isinstance(tokens, dict):
            cell["token_samples"].append(tokens)

    comparisons: dict[str, dict] = {}
    for test_case_name, configurations in cells.items():
        for cell in configurations.values():
            scores = cell.pop("scores")
            det_scores = cell.pop("det_scores")
            sem_scores = cell.pop("sem_scores")
            cell["mean_score"] = round(sum(scores) / len(scores), 2) if scores else None
            cell["mean_deterministic_score"] = (
                round(sum(det_scores) / len(det_scores), 2) if det_scores else None
            )
            cell["mean_semantic_score"] = (
                round(sum(sem_scores) / len(sem_scores), 2) if sem_scores else None
            )
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


def format_delta(old, new) -> str:
    """Render a signed absolute + percent change, e.g. '-73 (-6.7%)'. Pure.

    Returns 'n/a' when either value is missing, and omits the percent when the
    old value is zero (percent change is undefined against a zero baseline).
    """
    if old is None or new is None:
        return "n/a"
    diff = new - old
    whole = float(diff).is_integer()
    abs_text = f"{int(diff):+d}" if whole else f"{diff:+.2f}"
    if old == 0:
        return abs_text if diff == 0 else f"{abs_text} (n/a%)"
    percent = diff / old * 100
    return f"{abs_text} ({percent:+.1f}%)"


def format_cost_delta(old_cost: float | None, new_cost: float | None) -> str:
    """Render a signed 4-decimal USD + percent change, e.g. '-$0.0009 (-14.2%)'. Pure."""
    if old_cost is None or new_cost is None:
        return "n/a"
    diff = new_cost - old_cost
    sign = "+" if diff >= 0 else "-"
    abs_text = f"{sign}${abs(diff):.4f}"
    if old_cost == 0:
        return abs_text if diff == 0 else f"{abs_text} (n/a%)"
    percent = diff / old_cost * 100
    return f"{abs_text} ({percent:+.1f}%)"


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
    exp_id = experiment.get("id")
    title = (
        f"# Evaluation report: {experiment['skill']} (`{exp_id}`)"
        if exp_id
        else f"# Evaluation report: {experiment['skill']}"
    )

    # Only price when exactly one priced model produced the run; otherwise the
    # dollar figure would mix rate cards. _distinct joins multiple models.
    cost_model = model_text if model_text in PRICING else None
    total_cost: float | None = None
    if cost_model and results:
        trial_costs = [
            estimate_api_cost(r.get("tokens") or {}, cost_model)
            for r in results
            if isinstance(r.get("tokens"), dict)
        ]
        if trial_costs and all(c is not None for c in trial_costs):
            total_cost = round(sum(c for c in trial_costs if c is not None), 6)

    lines = [
        title,
        "",
        f"**Setup:** Model `{markdown_cell(model_text)}` · "
        f"Reasoning effort `{markdown_cell(reasoning_text)}` · "
        f"Trials per cell `{markdown_cell(trials_text)}` · "
        f"Semantic judges `{experiment.get('semantic_judges', 0)}` · "
        f"Status `{experiment['status']}` · "
        f"Started `{markdown_cell(date_text)}`",
        "",
        "## Result",
        "",
        "| Outcome | Mean score | Passed | Needs review | Completed | Est. total cost |",
        "|:---:|---:|---:|---:|---:|---:|",
        f"| **{outcome}** | {score_text} | "
        f"{experiment.get('passed_trials', 0)}/{len(results)} | "
        f"{experiment.get('needs_review_trials', 0)}/{len(results)} | "
        f"{len(results)}/{experiment['worker_trials']} | "
        f"{format_cost(total_cost)} |",
        "",
        "## Trials",
        "",
    ]
    if results:
        lines.extend(
            [
                "| Test case | Config | Trial | Score | Det | Sem | Band | Pass | Evidence |",
                "|---|---|---:|---:|---:|---:|:--:|:--:|---|",
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
            det_score = result.get("deterministic_score")
            shown_det = (
                det_score
                if isinstance(det_score, (int, float)) and not isinstance(det_score, bool)
                else "—"
            )
            sem_score = result.get("semantic_score")
            shown_sem = (
                sem_score
                if isinstance(sem_score, (int, float)) and not isinstance(sem_score, bool)
                else "—"
            )
            is_review = (
                result.get("needs_review") or result.get("status") == "needs_review"
            )
            # Display band derived from score; "review" when the trial abstained.
            band = (
                "review"
                if is_review
                else (score_band(result_score)
                      if isinstance(result_score, (int, float)) else "—")
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(result["test_case"]),
                        markdown_cell(result["configuration"]),
                        str(result["trial"]),
                        str(shown_score),
                        str(shown_det),
                        str(shown_sem),
                        band,
                        (
                            "Review"
                            if is_review
                            else ("Yes" if result["overall_pass"] else "No")
                        ),
                        f"[result.json]({details.as_posix()})",
                    ]
                )
                + " |"
            )
    else:
        lines.append("No trial completed successfully enough to produce a result.")

    # Findings: surface both failing trials and score deductions on passing trials
    # so any <100 score is explained directly in report.md.
    def _trial_reasons(r: dict) -> list[str]:
        if r.get("failure_reasons"):
            return list(r["failure_reasons"])
        combined: list[str] = []
        for section in ("deterministic", "semantic"):
            sec = r.get(section)
            if isinstance(sec, dict):
                for item in sec.get("reasons") or []:
                    if item and item not in combined:
                        combined.append(str(item))
        r_score = r.get("score")
        run_dir = r.get("run_directory")
        if (
            not combined
            and isinstance(r_score, (int, float))
            and r_score < 100
            and run_dir
        ):
            sem_file = Path(run_dir) / "grades" / "semantic" / "result.json"
            sem_data = read_json(sem_file) if sem_file.is_file() else None
            if isinstance(sem_data, dict):
                for j in sem_data.get("judges") or []:
                    jr = j.get("result") if isinstance(j, dict) else None
                    if not isinstance(jr, dict) or (jr.get("score") or 100) >= 100:
                        continue
                    for chk in jr.get("checks") or []:
                        if not isinstance(chk, dict):
                            continue
                        notes = str(chk.get("notes") or "").strip()
                        if notes and (
                            chk.get("pass") is False
                            or chk.get("verdict") not in (None, "pass")
                            or "deduct" in notes.lower()
                        ):
                            cid = chk.get("id", "check")
                            entry = f"semantic/{cid}: {notes}"
                            if entry not in combined:
                                combined.append(entry)
        return combined

    notable = [
        result
        for result in results
        if _trial_reasons(result)
        or (
            not result["overall_pass"]
            and not (
                result.get("needs_review") or result.get("status") == "needs_review"
            )
        )
    ]
    if notable or experiment.get("error"):
        lines.extend(["", "## Findings", ""])
        if experiment.get("error"):
            lines.append(f"- **Harness:** {experiment['error']}")
        for result in notable:
            label = (
                f"{result['test_case']} / {result['configuration']} / "
                f"trial-{result['trial']:02d}"
            )
            r_score = result.get("score")
            score_tag = f", score {r_score}" if isinstance(r_score, (int, float)) else ""
            if result.get("needs_review") or result.get("status") == "needs_review":
                tag = f"REVIEW{score_tag}"
            elif result.get("overall_pass"):
                tag = f"PASS{score_tag}"
            else:
                tag = f"FAIL{score_tag}"
            reasons = _trial_reasons(result) or [
                "The trial did not satisfy every configured pass condition."
            ]
            for reason in reasons:
                lines.append(
                    f"- **{markdown_cell(label)}** (`{tag}`): {markdown_cell(reason)}"
                )

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
                f"{comparison['with_skill_mean']} | "
                f"{format_delta(comparison['baseline_mean'], comparison['with_skill_mean'])} |"
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
                "| Test case | Config | Uncached input | Cached input | "
                "Output | Reasoning | Billed output | Est. cost |",
                "|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for name in sorted(cells):
            for config in ("baseline", "with-skill"):
                mt = cells.get(name, {}).get(config, {}).get("mean_tokens")
                if not isinstance(mt, dict):
                    continue
                cost = estimate_api_cost(mt, cost_model)
                lines.append(
                    f"| {markdown_cell(name)} | {config} | "
                    f"{_fmt(mt.get('uncached_input_tokens', 0))} | "
                    f"{_fmt(mt.get('cached_input_tokens', 0))} | "
                    f"{_fmt(mt.get('output_tokens', 0))} | "
                    f"{_fmt(mt.get('reasoning_output_tokens', 0))} | "
                    f"{_fmt(mt.get('billed_output_tokens', 0))} | "
                    f"{format_cost(cost)} |"
                )
            td = comparisons.get(name, {}).get("token_delta")
            if isinstance(td, dict):
                base_mt = cells.get(name, {}).get("baseline", {}).get("mean_tokens", {})
                skill_mt = cells.get(name, {}).get("with-skill", {}).get("mean_tokens", {})
                fields = (
                    "uncached_input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "billed_output_tokens",
                )
                cell_text = " | ".join(
                    format_delta(base_mt.get(f, 0), skill_mt.get(f, 0)) for f in fields
                )
                cost_delta = format_cost_delta(
                    estimate_api_cost(base_mt, cost_model),
                    estimate_api_cost(skill_mt, cost_model),
                )
                lines.append(
                    f"| {markdown_cell(name)} | **delta** | {cell_text} | {cost_delta} |"
                )
        token_trials = [r.get("tokens") for r in results if isinstance(r.get("tokens"), dict)]
        if len(token_trials) > 1:
            sum_uncached = sum(t.get("uncached_input_tokens", 0) or 0 for t in token_trials)
            sum_cached = sum(t.get("cached_input_tokens", 0) or 0 for t in token_trials)
            sum_output = sum(t.get("output_tokens", 0) or 0 for t in token_trials)
            sum_reasoning = sum(t.get("reasoning_output_tokens", 0) or 0 for t in token_trials)
            sum_billed = sum(t.get("billed_output_tokens", 0) or 0 for t in token_trials)
            lines.append(
                f"| **Total ({len(token_trials)} trials)** | — | "
                f"**{sum_uncached}** | **{sum_cached}** | **{sum_output}** | "
                f"**{sum_reasoning}** | **{sum_billed}** | **{format_cost(total_cost)}** |"
            )
        lines.extend(
            [
                "",
                f"_Per-case rows are trial means (comparable only on the same model and "
                f"reasoning effort); `Billed output` = output + reasoning. `Est. cost` is "
                f"an **API-equivalent estimate only** (public API list prices as of "
                f"{PRICING_AS_OF}) — NOT your actual subscription or credit spend._",
            ]
        )

    report = experiment_root / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
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


# --- Layer 2b: cross-experiment version/regression comparator (read-only) ---


def _axis_verdict(old, new, higher_is_better: bool) -> str:
    """Classify a numeric change as improved / regressed / unchanged. Pure."""
    if old is None or new is None:
        return "unknown"
    if new == old:
        return "unchanged"
    better = new > old if higher_is_better else new < old
    return "improved" if better else "regressed"


def _case_representative(experiment: dict, test_case: str, configuration: str) -> dict:
    """Return one per-trial result for a case/config, for reading run.json. Pure."""
    for result in experiment.get("results", []):
        if (
            result.get("test_case") == test_case
            and result.get("configuration") == configuration
        ):
            return result
    return {}


def compare_cell(old_cell: dict, new_cell: dict) -> dict:
    """Diff two cells across score, pass rate, and token axes. Pure.

    Verdict direction: score and pass rate are higher-is-better; every token
    axis is lower-is-better (fewer tokens for equal-or-better score is the win).
    """
    old_score = old_cell.get("mean_score")
    new_score = new_cell.get("mean_score")

    def _pass_rate(cell: dict):
        total = cell.get("total_trials") or 0
        return (cell.get("passed_trials", 0) / total) if total else None

    old_pass = _pass_rate(old_cell)
    new_pass = _pass_rate(new_cell)
    old_tokens = old_cell.get("mean_tokens") or {}
    new_tokens = new_cell.get("mean_tokens") or {}

    result = {
        "n_old": old_cell.get("total_trials", 0),
        "n_new": new_cell.get("total_trials", 0),
        "passed_old": old_cell.get("passed_trials", 0),
        "passed_new": new_cell.get("passed_trials", 0),
        "score": {
            "old": old_score,
            "new": new_score,
            "verdict": _axis_verdict(old_score, new_score, higher_is_better=True),
        },
        "pass_rate": {
            "old": old_pass,
            "new": new_pass,
            "verdict": _axis_verdict(old_pass, new_pass, higher_is_better=True),
        },
    }
    for key, cell_field in (
        ("deterministic_score", "mean_deterministic_score"),
        ("semantic_score", "mean_semantic_score"),
    ):
        old_val = old_cell.get(cell_field)
        new_val = new_cell.get(cell_field)
        if old_val is not None or new_val is not None:
            result[key] = {
                "old": old_val,
                "new": new_val,
                "verdict": _axis_verdict(old_val, new_val, higher_is_better=True),
            }
    # Token axes that reflect a skill-wording change: uncached input (a shorter
    # skill means less context every turn), output, reasoning, and billed total.
    for axis in (
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "billed_output_tokens",
    ):
        old_value = old_tokens.get(axis)
        new_value = new_tokens.get(axis)
        result[axis] = {
            "old": old_value,
            "new": new_value,
            "verdict": _axis_verdict(old_value, new_value, higher_is_better=False),
        }
    # Carry the raw mean-token dicts so a caller that knows the model can derive
    # an API-equivalent cost estimate without recomputing per-axis values.
    result["_mean_tokens_old"] = old_tokens
    result["_mean_tokens_new"] = new_tokens
    return result


def partition_cases(old_cases: list[str], new_cases: list[str]) -> dict:
    """Split case names into both / only-old / only-new. Pure."""
    old_set, new_set = set(old_cases), set(new_cases)
    return {
        "both": sorted(old_set & new_set),
        "only_old": sorted(old_set - new_set),
        "only_new": sorted(new_set - old_set),
    }


def _cell(experiment: dict, test_case: str, configuration: str) -> dict:
    raw = (
        experiment.get("aggregates", {})
        .get("cells", {})
        .get(test_case, {})
        .get(configuration, {})
    )
    if not raw:
        return {}
    cell = dict(raw)
    if "mean_deterministic_score" not in cell and experiment.get("results"):
        trials = [
            r
            for r in experiment.get("results", [])
            if r.get("test_case") == test_case
            and r.get("configuration") == configuration
        ]
        det_scores = [
            r["deterministic_score"]
            for r in trials
            if isinstance(r.get("deterministic_score"), (int, float))
        ]
        sem_scores = [
            r["semantic_score"]
            for r in trials
            if isinstance(r.get("semantic_score"), (int, float))
        ]
        cell["mean_deterministic_score"] = (
            round(sum(det_scores) / len(det_scores), 2) if det_scores else None
        )
        cell["mean_semantic_score"] = (
            round(sum(sem_scores) / len(sem_scores), 2) if sem_scores else None
        )
    return cell


def _confounds(old_rep: dict, new_rep: dict) -> list[str]:
    """Detect model/fixture confounds for one case by reading its run.json."""
    warnings: list[str] = []
    old_model = old_rep.get("resolved_model")
    new_model = new_rep.get("resolved_model")
    if old_model and new_model and old_model != new_model:
        warnings.append(f"model changed ({old_model} -> {new_model})")
    old_fixture = _run_field(old_rep, "fixture_sha256")
    new_fixture = _run_field(new_rep, "fixture_sha256")
    if old_fixture and new_fixture and old_fixture != new_fixture:
        warnings.append("fixture changed")
    return warnings


def _run_field(result: dict, field: str):
    """Read a field from a trial's run.json via its run_directory. READ-ONLY."""
    run_directory = result.get("run_directory")
    if not run_directory:
        return None
    metadata = read_json(Path(run_directory) / "run.json") or {}
    return metadata.get(field)


def _skill_diff(old_rep: dict, new_rep: dict) -> list[str]:
    """Unified diff of the two experiments' captured SKILL.md snapshots."""
    old_dir = old_rep.get("run_directory")
    new_dir = new_rep.get("run_directory")
    if not old_dir or not new_dir:
        return []
    old_meta = read_json(Path(old_dir) / "run.json") or {}
    new_meta = read_json(Path(new_dir) / "run.json") or {}
    old_text = read_skill_snapshot_text(Path(old_dir), old_meta)
    new_text = read_skill_snapshot_text(Path(new_dir), new_meta)
    if old_text is None or new_text is None or old_text == new_text:
        return []
    return list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="v1/SKILL.md",
            tofile="v2/SKILL.md",
            lineterm="",
        )
    )


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def render_comparison_markdown(
    skill: str, old_id: str, new_id: str, partition: dict, rows: list[dict],
    skill_diff: list[str], summary: dict, provenance: dict,
) -> str:
    """Build the markdown comparison report. Pure (no I/O)."""
    trials = provenance.get("trials") or {}
    old_trials = trials.get("old")
    new_trials = trials.get("new")
    old_model = provenance.get("old_model") or "not captured"
    new_model = provenance.get("new_model") or "not captured"
    old_effort = provenance.get("old_effort") or "not captured"
    new_effort = provenance.get("new_effort") or "not captured"
    config = provenance.get("config") or "with-skill"
    models_match = (
        provenance.get("old_model")
        and provenance.get("new_model")
        and provenance["old_model"] == provenance["new_model"]
        and provenance.get("old_effort") == provenance.get("new_effort")
    )
    cost_model = (
        provenance.get("old_model")
        if provenance.get("old_model") == provenance.get("new_model")
        and provenance.get("old_model") in PRICING
        else None
    )

    title_suffix = f" ({config})" if config != "with-skill" else ""
    lines = [
        f"# Comparison: {skill}{title_suffix} — {old_id} (v1) vs {new_id} (v2)",
        "",
        "| Setup | v1 (" + old_id + ") | v2 (" + new_id + ") |",
        "|---|---|---|",
        f"| Model | {old_model} | {new_model} |",
        f"| Reasoning effort | {old_effort} | {new_effort} |",
        f"| Trials per cell | {old_trials if old_trials is not None else '?'} "
        f"| {new_trials if new_trials is not None else '?'} |",
    ]
    if config != "with-skill":
        lines.append(f"| Configuration | {config} | {config} |")
    lines.append("")
    if not models_match:
        lines.append(
            "> WARNING: model and/or reasoning effort differ between the two "
            "experiments. Score and especially token comparisons may be "
            "confounded by the model change rather than the skill edit."
        )
        lines.append("")
    lines.extend([
        "| Summary | Value |",
        "|---|---|",
        f"| Shared cases | {len(partition['both'])} |",
        f"| Improved (score) | {summary['improved']} |",
        f"| Regressed (score) | {summary['regressed']} |",
        f"| Unchanged (score) | {summary['unchanged']} |",
    ])
    if partition["only_old"]:
        lines.append(f"| Dropped in v2 (not compared) | {', '.join(partition['only_old'])} |")
    if partition["only_new"]:
        lines.append(f"| Added in v2 (not compared) | {', '.join(partition['only_new'])} |")
    lines.append("")
    if partition["only_old"] or partition["only_new"]:
        lines.append("> WARNING: test-case sets differ; aggregate comparison is weakened.")
        lines.append("")

    if rows:
        lines.append("## Case overview")
        lines.append("")
        lines.append(
            "| Test case | Score (v1 -> v2) | Pass rate | Billed out (v1 -> v2) "
            "| Est. cost (v1 -> v2) | Score verdict |"
        )
        lines.append("|---|---|---|---|---|---|")
        for row in rows:
            diff = row["diff"]
            sc = diff.get("score", {})
            sc_text = f"{_fmt(sc.get('old'))} -> {_fmt(sc.get('new'))}"
            if sc.get("old") is not None and sc.get("new") is not None and sc["old"] != sc["new"]:
                sc_text += f" ({sc['new'] - sc['old']:+g})"
            n_old = diff.get("n_old", 0)
            n_new = diff.get("n_new", 0)
            p_old = diff.get("passed_old")
            p_new = diff.get("passed_new")
            if p_old is not None and p_new is not None and n_old and n_new:
                pr_text = f"{p_old}/{n_old} -> {p_new}/{n_new}"
            else:
                pr = diff.get("pass_rate", {})
                pr_text = f"{_fmt(pr.get('old'))} -> {_fmt(pr.get('new'))}"
            bo = diff.get("billed_output_tokens", {})
            bo_text = f"{_fmt(bo.get('old'))} -> {_fmt(bo.get('new'))}"
            if bo.get("old") and bo.get("new") is not None:
                pct = (bo["new"] - bo["old"]) / bo["old"] * 100
                bo_text += f" ({pct:+.1f}%)"
            c_old = estimate_api_cost(diff.get("_mean_tokens_old", {}), cost_model)
            c_new = estimate_api_cost(diff.get("_mean_tokens_new", {}), cost_model)
            if c_old is not None and c_new is not None:
                cost_text = f"{format_cost(c_old)} -> {format_cost(c_new)}"
                if c_old:
                    cost_text += f" ({(c_new - c_old) / c_old * 100:+.1f}%)"
            else:
                cost_text = "n/a"
            lines.append(
                f"| {row['test_case']} | {sc_text} | {pr_text} | {bo_text} "
                f"| {cost_text} | {sc.get('verdict', 'n/a')} |"
            )
        lines.append("")

    AXES = [
        ("score", "score"),
        ("deterministic_score", "det score"),
        ("semantic_score", "sem score"),
        ("pass_rate", "pass rate"),
        ("uncached_input_tokens", "uncached input"),
        ("output_tokens", "output"),
        ("reasoning_output_tokens", "reasoning"),
        ("billed_output_tokens", "billed output"),
    ]
    for row in rows:
        diff = row["diff"]
        n_old = diff.get("n_old", "?")
        n_new = diff.get("n_new", "?")
        lines.append(
            f"## {row['test_case']}  (mean of n={n_old} v1 trial(s) -> "
            f"n={n_new} v2 trial(s))"
        )
        lines.append("")
        lines.append("| Axis | v1 | v2 | Change | Verdict |")
        lines.append("|---|---|---|---|---|")
        for key, label in AXES:
            a = diff.get(key)
            if not a:
                continue
            lines.append(
                f"| {label} | {_fmt(a['old'])} | {_fmt(a['new'])} | "
                f"{format_delta(a['old'], a['new'])} | {a['verdict']} |"
            )
        cost_old = estimate_api_cost(diff.get("_mean_tokens_old", {}), cost_model)
        cost_new = estimate_api_cost(diff.get("_mean_tokens_new", {}), cost_model)
        if cost_old is not None and cost_new is not None:
            cost_verdict = _axis_verdict(cost_old, cost_new, higher_is_better=False)
        else:
            cost_verdict = "n/a"
        lines.append(
            f"| est. API-equiv. cost | {format_cost(cost_old)} | "
            f"{format_cost(cost_new)} | {format_cost_delta(cost_old, cost_new)} | "
            f"{cost_verdict} |"
        )
        lines.append("")
        if row["confounds"]:
            lines.append(
                f"> CONFOUNDED: {'; '.join(row['confounds'])} "
                "(hold everything but the skill constant)."
            )
            lines.append("")

    lines.append("## Skill language change (v1 -> v2)")
    lines.append("")
    if skill_diff:
        lines.append("```diff")
        lines.extend(skill_diff)
        lines.append("```")
    else:
        lines.append("_No skill-language diff available "
                     "(snapshots missing or identical)._")
    lines.append("")
    lines.append(
        f"_Every per-case number is a MEAN over that cell's trials; "
        f"`billed output` = output + reasoning (lower is better on token/cost axes); "
        f"`est. API-equiv. cost` is an estimate at public API list prices "
        f"({PRICING_AS_OF}, {PRICING_SOURCE}) — NOT actual subscription or credit spend._"
    )
    lines.append("")
    return "\n".join(lines)


def compare_experiments(args: argparse.Namespace, skills: dict[str, Path]) -> int:
    if args.skill not in skills:
        fail(f"no evaluation for {args.skill}; available: {', '.join(skills) or 'none'}")
    config = getattr(args, "config", "with-skill") or "with-skill"
    runs_root = skills[args.skill] / "runs"
    old_path = runs_root / args.old / "experiment.json"
    new_path = runs_root / args.new / "experiment.json"
    old = read_json(old_path)
    new = read_json(new_path)
    if not isinstance(old, dict):
        fail(f"cannot read experiment manifest: {old_path}")
    if not isinstance(new, dict):
        fail(f"cannot read experiment manifest: {new_path}")

    partition = partition_cases(old.get("test_cases", []), new.get("test_cases", []))
    if not partition["both"]:
        print(f"Comparing {args.skill}: {args.old} -> {args.new}")
        print("No shared test cases to compare.")
        return 1

    missing_config = [
        tc for tc in partition["both"]
        if not _cell(old, tc, config) or not _cell(new, tc, config)
    ]
    if len(missing_config) == len(partition["both"]):
        fail(
            f"configuration '{config}' is missing in one or both experiments "
            f"({args.old}, {args.new})"
        )

    improved = regressed = 0
    rows: list[dict] = []
    for test_case in partition["both"]:
        old_cell = _cell(old, test_case, config)
        new_cell = _cell(new, test_case, config)
        if not old_cell or not new_cell:
            continue
        diff = compare_cell(old_cell, new_cell)
        old_rep = _case_representative(old, test_case, config)
        new_rep = _case_representative(new, test_case, config)
        confounds = _confounds(old_rep, new_rep)
        rows.append({"test_case": test_case, "diff": diff, "confounds": confounds})
        if diff["score"]["verdict"] == "improved":
            improved += 1
        elif diff["score"]["verdict"] == "regressed":
            regressed += 1

    unchanged = len(rows) - improved - regressed
    skill_diff = (
        _skill_diff(
            _case_representative(old, partition["both"][0], "with-skill"),
            _case_representative(new, partition["both"][0], "with-skill"),
        )
        if config == "with-skill"
        else []
    )

    # Provenance for the comparability caveat: model + reasoning effort actually
    # resolved, read from a representative trial of each experiment, plus trials
    # per cell. Token/score comparisons are only valid on the same model/effort.
    old_rep0 = _case_representative(old, partition["both"][0], config)
    new_rep0 = _case_representative(new, partition["both"][0], config)
    provenance = {
        "old_model": old_rep0.get("resolved_model"),
        "new_model": new_rep0.get("resolved_model"),
        "old_effort": old_rep0.get("resolved_reasoning_effort"),
        "new_effort": new_rep0.get("resolved_reasoning_effort"),
        "trials": {"old": old.get("trials"), "new": new.get("trials")},
        "config": config,
    }

    # Write the full report to a comparisons/ dir that is a sibling of runs/, so
    # comparison artifacts never mutate an immutable experiment dir.
    summary = {"improved": improved, "regressed": regressed, "unchanged": unchanged}
    markdown = render_comparison_markdown(
        args.skill, args.old, args.new, partition, rows, skill_diff, summary,
        provenance,
    )
    comparisons_root = skills[args.skill] / "comparisons"
    comparisons_root.mkdir(parents=True, exist_ok=True)
    suffix = "-baseline" if config == "baseline" else ""
    out_path = comparisons_root / f"{args.old}-vs-{args.new}{suffix}.md"
    out_path.write_text(markdown, encoding="utf-8")

    # Terminal stays short: the detail lives in the file.
    cfg_label = f" ({config})" if config != "with-skill" else ""
    print(f"Comparing {args.skill}{cfg_label}: {args.old} (v1) -> {args.new} (v2)")
    print(
        f"  {len(rows)} shared case(s): "
        f"{improved} improved, {regressed} regressed, {unchanged} unchanged (by score)"
    )
    if partition["only_old"] or partition["only_new"]:
        print(
            f"  Note: test-case sets differ "
            f"(+{len(partition['only_new'])} new, -{len(partition['only_old'])} dropped); "
            "not compared."
        )
    confounded = [r["test_case"] for r in rows if r["confounds"]]
    if confounded:
        print(f"  Confounded (model/fixture changed): {', '.join(confounded)}")
    print(f"Report: {out_path}")
    return 0


def main() -> int:
    args = parse_args()
    skills = discover_skills(EVALS_ROOT, SKILLS_ROOT)
    if args.command == "list":
        return list_evaluations(args, skills)
    if args.command == "check":
        return check_evaluation(args, skills)
    if args.command == "compare":
        return compare_experiments(args, skills)
    return run_evaluation(args, skills)


if __name__ == "__main__":
    raise SystemExit(main())
