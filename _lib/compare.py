"""Cross-experiment version/regression comparator and Markdown rendering."""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path
from typing import Callable

from _lib.common import _fmt, read_json
from _lib.pricing import (
    PRICING,
    PRICING_AS_OF,
    PRICING_SOURCE,
    estimate_api_cost,
    format_cost,
    format_cost_delta,
    format_delta,
)
from _lib.trace import read_skill_snapshot_text


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


def compare_experiments(
    args: argparse.Namespace,
    skills: dict[str, Path],
    fail_fn: Callable[[str], None] | None = None,
) -> int:
    def _fail(msg: str) -> None:
        if fail_fn is not None:
            fail_fn(msg)
        raise SystemExit(1)

    if args.skill not in skills:
        _fail(f"no evaluation for {args.skill}; available: {', '.join(skills) or 'none'}")
    config = getattr(args, "config", "with-skill") or "with-skill"
    runs_root = skills[args.skill] / "runs"
    old_path = runs_root / args.old / "experiment.json"
    new_path = runs_root / args.new / "experiment.json"
    old = read_json(old_path)
    new = read_json(new_path)
    if not isinstance(old, dict):
        _fail(f"cannot read experiment manifest: {old_path}")
    if not isinstance(new, dict):
        _fail(f"cannot read experiment manifest: {new_path}")

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
        _fail(
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

