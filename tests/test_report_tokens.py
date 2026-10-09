"""Test that write_report renders the token cost section.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import eval as eval_mod  # noqa: E402


def _result(test_case, configuration, score, billed_output, overall_pass=True):
    return {
        "test_case": test_case,
        "configuration": configuration,
        "trial": 1,
        "score": score,
        "overall_pass": overall_pass,
        "run_directory": "trials/x",
        "tokens": {
            "input_tokens": 1000,
            "cached_input_tokens": 400,
            "cache_write_input_tokens": 0,
            "uncached_input_tokens": 600,
            "output_tokens": billed_output,
            "reasoning_output_tokens": 0,
            "billed_output_tokens": billed_output,
        },
    }


def test_report_renders_token_section(tmp_path):
    results = [
        _result("case-a", "baseline", 70, 100),
        _result("case-a", "with-skill", 90, 60),
    ]
    aggregates = eval_mod.aggregate(results)
    experiment = {
        "skill": "demo",
        "status": "completed",
        "overall_pass": True,
        "mean_score": 80,
        "passed_trials": 2,
        "worker_trials": 2,
        "semantic_judges": 0,
    }

    report_path = eval_mod.write_report(tmp_path, experiment, aggregates, results)
    text = report_path.read_text(encoding="utf-8")

    assert "## Token usage" in text
    assert "Billed output" in text
    # delta row shows with-skill used 40 fewer billed-output tokens
    assert "-40" in text
    # both config rows present
    assert "| case-a | baseline |" in text
    assert "| case-a | with-skill |" in text


def test_report_renders_tokens_without_compare(tmp_path):
    """Tokens are a measurement like the score: shown even for a single-config
    run (no --compare), just without a delta row."""
    results = [
        _result("case-a", "with-skill", 90, 60),
        _result("case-a", "with-skill", 90, 80),
    ]
    aggregates = eval_mod.aggregate(results)
    experiment = {
        "skill": "demo",
        "status": "completed",
        "overall_pass": True,
        "mean_score": 90,
        "passed_trials": 2,
        "worker_trials": 2,
        "semantic_judges": 0,
    }

    report_path = eval_mod.write_report(tmp_path, experiment, aggregates, results)
    text = report_path.read_text(encoding="utf-8")

    # Token section and the with-skill mean row must appear
    assert "## Token" in text
    assert "| case-a | with-skill |" in text
    # mean billed output = (60 + 80) / 2 = 70
    assert "70" in text
    # No baseline, so no delta row
    assert "**delta**" not in text


def test_report_renders_provenance_header(tmp_path):
    """The report states the model + reasoning effort that produced the numbers."""
    results = [
        {
            "test_case": "case-a", "configuration": "with-skill", "trial": 1,
            "score": 90, "overall_pass": True, "run_directory": "trials/x",
            "resolved_model": "gpt-5.6-luna", "resolved_reasoning_effort": "low",
            "tokens": {
                "input_tokens": 100, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "uncached_input_tokens": 100,
                "output_tokens": 10, "reasoning_output_tokens": 0,
                "billed_output_tokens": 10,
            },
        },
    ]
    aggregates = eval_mod.aggregate(results)
    experiment = {
        "skill": "demo", "status": "completed", "overall_pass": True,
        "mean_score": 90, "passed_trials": 1, "worker_trials": 1,
        "semantic_judges": 0, "trials": 1, "started_at": "2026-10-09T00:00:00Z",
    }

    report_path = eval_mod.write_report(tmp_path, experiment, aggregates, results)
    text = report_path.read_text(encoding="utf-8")

    assert "gpt-5.6-luna" in text
    assert "low" in text  # reasoning effort
    # provenance should be labelled so a reader understands it
    assert "Model" in text and "Reasoning" in text


def test_report_marks_provenance_unknown_when_absent(tmp_path):
    """If model/effort could not be captured, say so rather than omit silently."""
    results = [
        {
            "test_case": "case-a", "configuration": "with-skill", "trial": 1,
            "score": 90, "overall_pass": True, "run_directory": "trials/x",
            "resolved_model": None, "resolved_reasoning_effort": None,
            "tokens": {
                "input_tokens": 100, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "uncached_input_tokens": 100,
                "output_tokens": 10, "reasoning_output_tokens": 0,
                "billed_output_tokens": 10,
            },
        },
    ]
    aggregates = eval_mod.aggregate(results)
    experiment = {
        "skill": "demo", "status": "completed", "overall_pass": True,
        "mean_score": 90, "passed_trials": 1, "worker_trials": 1,
        "semantic_judges": 0, "trials": 1, "started_at": "2026-10-09T00:00:00Z",
    }
    report_path = eval_mod.write_report(tmp_path, experiment, aggregates, results)
    text = report_path.read_text(encoding="utf-8")
    assert "not captured" in text.lower() or "unknown" in text.lower()
