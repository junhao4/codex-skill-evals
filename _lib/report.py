"""Experiment aggregation, summary calculation, and Markdown report generation."""

from __future__ import annotations

from pathlib import Path

from _lib.common import _fmt, markdown_cell, read_json, write_json
from _lib.grading import score_band
from _lib.pricing import (
    PRICING,
    PRICING_AS_OF,
    estimate_api_cost,
    format_cost,
    format_cost_delta,
    format_delta,
)


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "billed_output_tokens",
)


def aggregate(results: list[dict]) -> dict:
    """Summarize repeated trials and baseline-versus-skill differences."""
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

