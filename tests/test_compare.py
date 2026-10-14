"""Tests for Layer 2b: cross-experiment comparator pure helpers.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# eval.py lives at repo root alongside _lib; import it directly.
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("evalmod", REPO_ROOT / "eval.py")
evalmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evalmod)


# --- _axis_verdict ---


def test_axis_verdict_higher_is_better():
    assert evalmod._axis_verdict(80, 90, higher_is_better=True) == "improved"
    assert evalmod._axis_verdict(90, 80, higher_is_better=True) == "regressed"
    assert evalmod._axis_verdict(80, 80, higher_is_better=True) == "unchanged"


def test_axis_verdict_lower_is_better_tokens():
    # Fewer tokens is better.
    assert evalmod._axis_verdict(600, 400, higher_is_better=False) == "improved"
    assert evalmod._axis_verdict(400, 600, higher_is_better=False) == "regressed"


def test_axis_verdict_unknown_when_missing():
    assert evalmod._axis_verdict(None, 90, higher_is_better=True) == "unknown"
    assert evalmod._axis_verdict(90, None, higher_is_better=True) == "unknown"


# --- partition_cases ---


def test_partition_cases_sets():
    p = evalmod.partition_cases(["a", "b", "c"], ["b", "c", "d"])
    assert p["both"] == ["b", "c"]
    assert p["only_old"] == ["a"]
    assert p["only_new"] == ["d"]


def test_partition_identical():
    p = evalmod.partition_cases(["a", "b"], ["b", "a"])
    assert p["both"] == ["a", "b"]
    assert p["only_old"] == []
    assert p["only_new"] == []


# --- compare_cell ---


def _cell(score, passed, total, output=0, reasoning=0, uncached=0):
    return {
        "mean_score": score,
        "passed_trials": passed,
        "total_trials": total,
        "mean_tokens": {
            "uncached_input_tokens": uncached,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "billed_output_tokens": output + reasoning,
        },
    }


# --- format_delta ---


def test_format_delta_abs_and_percent():
    assert evalmod.format_delta(1000, 900) == "-100 (-10.0%)"
    assert evalmod.format_delta(100, 150) == "+50 (+50.0%)"


def test_format_delta_none_is_na():
    assert evalmod.format_delta(None, 5) == "n/a"
    assert evalmod.format_delta(5, None) == "n/a"


def test_format_delta_zero_baseline():
    assert evalmod.format_delta(0, 0) == "+0"
    assert evalmod.format_delta(0, 10) == "+10 (n/a%)"


def test_format_delta_float():
    assert evalmod.format_delta(1.0, 1.5) == "+0.50 (+50.0%)"


def test_compare_cell_improved_score_fewer_tokens():
    old = _cell(80, 2, 3, output=5000, reasoning=1000)
    new = _cell(92, 3, 3, output=3500, reasoning=600)
    diff = evalmod.compare_cell(old, new)
    assert diff["score"]["verdict"] == "improved"
    assert diff["pass_rate"]["old"] == 2 / 3
    assert diff["pass_rate"]["new"] == 1.0
    assert diff["pass_rate"]["verdict"] == "improved"
    # billed = output + reasoning: 6000 -> 4100, fewer is better
    assert diff["billed_output_tokens"]["old"] == 6000
    assert diff["billed_output_tokens"]["new"] == 4100
    assert diff["billed_output_tokens"]["verdict"] == "improved"


def test_compare_cell_regressed_score():
    old = _cell(90, 3, 3)
    new = _cell(40, 1, 3)
    diff = evalmod.compare_cell(old, new)
    assert diff["score"]["verdict"] == "regressed"
    assert diff["pass_rate"]["verdict"] == "regressed"


def test_compare_cell_unchanged():
    old = _cell(100, 3, 3, output=4000)
    new = _cell(100, 3, 3, output=4000)
    diff = evalmod.compare_cell(old, new)
    assert diff["score"]["verdict"] == "unchanged"
    assert diff["billed_output_tokens"]["verdict"] == "unchanged"


def test_compare_cell_missing_score_is_unknown():
    old = _cell(None, 0, 0)
    new = _cell(80, 1, 1)
    diff = evalmod.compare_cell(old, new)
    assert diff["score"]["verdict"] == "unknown"
    # pass rate: old total 0 -> None -> unknown
    assert diff["pass_rate"]["old"] is None
    assert diff["pass_rate"]["verdict"] == "unknown"


# --- render_comparison_markdown ---


def test_render_markdown_has_sections_and_summary():
    partition = {"both": ["case-a"], "only_old": [], "only_new": ["case-b"]}
    rows = [
        {
            "test_case": "case-a",
            "diff": evalmod.compare_cell(
                _cell(80, 2, 3, output=5000), _cell(92, 3, 3, output=3500)
            ),
            "confounds": ["model changed (x -> y)"],
        }
    ]
    summary = {"improved": 1, "regressed": 0, "unchanged": 0}
    provenance = {
        "old_model": "gpt-x", "new_model": "gpt-x",
        "old_effort": "low", "new_effort": "low",
        "trials": {"old": 2, "new": 2},
    }
    md = evalmod.render_comparison_markdown(
        "myskill", "experiment-001", "experiment-002", partition, rows, [], summary,
        provenance,
    )
    assert "# Comparison: myskill" in md
    assert "mean of n=3 v1 trial(s)" in md
    assert "Added in v2 (not compared) | case-b" in md
    assert "CONFOUNDED" in md
    assert "No skill-language diff available" in md
    assert "| Improved (score) | 1 |" in md
    # enriched axes + change column present
    assert "uncached input" in md
    assert "reasoning" in md
    assert "| Change |" in md
    # provenance/annotation present
    assert "| Model | gpt-x | gpt-x |" in md
    assert "Reasoning effort" in md
    assert "MEAN over" in md


def test_render_markdown_warns_on_model_mismatch():
    partition = {"both": ["case-a"], "only_old": [], "only_new": []}
    rows = [
        {
            "test_case": "case-a",
            "diff": evalmod.compare_cell(_cell(80, 1, 1), _cell(80, 1, 1)),
            "confounds": [],
        }
    ]
    summary = {"improved": 0, "regressed": 0, "unchanged": 1}
    provenance = {
        "old_model": "gpt-x", "new_model": "gpt-y",
        "old_effort": "low", "new_effort": "low",
        "trials": {"old": 1, "new": 1},
    }
    md = evalmod.render_comparison_markdown(
        "s", "e1", "e2", partition, rows, [], summary, provenance
    )
    assert "model and/or reasoning effort differ" in md


def test_render_markdown_includes_skill_diff_fence():
    partition = {"both": ["case-a"], "only_old": [], "only_new": []}
    rows = [
        {
            "test_case": "case-a",
            "diff": evalmod.compare_cell(_cell(80, 1, 1), _cell(80, 1, 1)),
            "confounds": [],
        }
    ]
    summary = {"improved": 0, "regressed": 0, "unchanged": 1}
    diff_lines = ["--- v1/SKILL.md", "+++ v2/SKILL.md", "-old line", "+new line"]
    provenance = {
        "old_model": "m", "new_model": "m", "old_effort": "low",
        "new_effort": "low", "trials": {"old": 1, "new": 1},
    }
    md = evalmod.render_comparison_markdown(
        "myskill", "e1", "e2", partition, rows, diff_lines, summary, provenance
    )
    assert "```diff" in md
    assert "-old line" in md
    assert "+new line" in md


def test_render_markdown_includes_case_overview_and_det_sem_scores():
    old_c = _cell(85, 2, 2, output=5000)
    old_c["mean_deterministic_score"] = 100
    old_c["mean_semantic_score"] = 85
    new_c = _cell(100, 2, 2, output=4000)
    new_c["mean_deterministic_score"] = 100
    new_c["mean_semantic_score"] = 100

    partition = {"both": ["case-a"], "only_old": [], "only_new": []}
    rows = [
        {
            "test_case": "case-a",
            "diff": evalmod.compare_cell(old_c, new_c),
            "confounds": [],
        }
    ]
    summary = {"improved": 1, "regressed": 0, "unchanged": 0}
    provenance = {
        "old_model": "gpt-5.6-luna", "new_model": "gpt-5.6-luna",
        "old_effort": "xhigh", "new_effort": "xhigh",
        "trials": {"old": 2, "new": 2},
    }
    md = evalmod.render_comparison_markdown(
        "myskill", "e1", "e2", partition, rows, [], summary, provenance
    )
    assert "## Case overview" in md
    assert "| case-a | 85 -> 100 (+15) | 2/2 -> 2/2 |" in md
    assert "| det score | 100 | 100 |" in md
    assert "| sem score | 85 | 100 |" in md


def test_cell_backfills_det_and_sem_scores_from_results():
    exp = {
        "aggregates": {
            "cells": {
                "case-a": {
                    "with-skill": {
                        "mean_score": 90,
                        "passed_trials": 2,
                        "total_trials": 2,
                        "mean_tokens": {},
                    }
                }
            }
        },
        "results": [
            {
                "test_case": "case-a",
                "configuration": "with-skill",
                "deterministic_score": 100,
                "semantic_score": 85,
            },
            {
                "test_case": "case-a",
                "configuration": "with-skill",
                "deterministic_score": 100,
                "semantic_score": 95,
            },
        ],
    }
    c = evalmod._cell(exp, "case-a", "with-skill")
    assert c["mean_deterministic_score"] == 100
    assert c["mean_semantic_score"] == 90


def test_compare_experiments_supports_baseline_config(tmp_path):
    import argparse
    import json

    skill_dir = tmp_path / "demo"
    for exp_id, score in (("experiment-001", 80), ("experiment-002", 95)):
        exp_dir = skill_dir / "runs" / exp_id
        exp_dir.mkdir(parents=True)
        manifest = {
            "skill": "demo",
            "trials": 1,
            "test_cases": ["case-a"],
            "aggregates": {
                "cells": {
                    "case-a": {
                        "baseline": {
                            "mean_score": score,
                            "passed_trials": 1,
                            "total_trials": 1,
                            "mean_tokens": {
                                "uncached_input_tokens": 1000,
                                "output_tokens": 500,
                                "reasoning_output_tokens": 100,
                                "billed_output_tokens": 600,
                            },
                        }
                    }
                }
            },
            "results": [
                {
                    "test_case": "case-a",
                    "configuration": "baseline",
                    "resolved_model": "gpt-5.6-luna",
                    "resolved_reasoning_effort": "low",
                    "deterministic_score": 100,
                    "semantic_score": score,
                }
            ],
        }
        (exp_dir / "experiment.json").write_text(json.dumps(manifest), encoding="utf-8")

    args = argparse.Namespace(
        skill="demo", old="experiment-001", new="experiment-002", config="baseline"
    )
    rc = evalmod.compare_experiments(args, {"demo": skill_dir})
    assert rc == 0
    out_file = skill_dir / "comparisons" / "experiment-001-vs-experiment-002-baseline.md"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# Comparison: demo (baseline) — experiment-001 (v1) vs experiment-002 (v2)" in content
    assert "| Configuration | baseline | baseline |" in content


