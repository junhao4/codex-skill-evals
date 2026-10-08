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

    assert "## Token cost comparison" in text
    assert "Billed output" in text
    # delta row shows with-skill used 40 fewer billed-output tokens
    assert "-40" in text
    # both config rows present
    assert "| case-a | baseline |" in text
    assert "| case-a | with-skill |" in text
