"""Tests for eval.aggregate() token aggregation.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import eval as eval_mod  # noqa: E402


def _result(test_case, configuration, score, tokens, overall_pass=True):
    return {
        "test_case": test_case,
        "configuration": configuration,
        "score": score,
        "overall_pass": overall_pass,
        "tokens": tokens,
    }


def _tokens(uncached, cached, output, reasoning):
    return {
        "input_tokens": uncached + cached,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": uncached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "billed_output_tokens": output + reasoning,
    }


def test_cell_has_mean_token_breakdown():
    """Each cell should carry mean token usage, not just mean_score."""
    results = [
        _result("case-a", "with-skill", 90, _tokens(100, 300, 20, 5)),
        _result("case-a", "with-skill", 80, _tokens(200, 300, 40, 15)),
    ]

    agg = eval_mod.aggregate(results)
    cell = agg["cells"]["case-a"]["with-skill"]

    assert cell["mean_score"] == 85.0
    assert cell["mean_tokens"]["uncached_input_tokens"] == 150.0
    assert cell["mean_tokens"]["cached_input_tokens"] == 300.0
    assert cell["mean_tokens"]["output_tokens"] == 30.0
    assert cell["mean_tokens"]["reasoning_output_tokens"] == 10.0
    assert cell["mean_tokens"]["billed_output_tokens"] == 40.0


def test_comparison_has_token_delta():
    """A baseline-vs-with-skill comparison should report token_delta on billed output."""
    results = [
        _result("case-a", "baseline", 70, _tokens(100, 0, 100, 0)),
        _result("case-a", "with-skill", 90, _tokens(100, 0, 60, 0)),
    ]

    agg = eval_mod.aggregate(results)
    comp = agg["comparisons"]["case-a"]

    assert comp["delta"] == 20.0  # score improved
    # with-skill used fewer billed-output tokens: 60 vs 100 -> delta -40
    assert comp["token_delta"]["billed_output_tokens"] == -40.0


def test_missing_tokens_does_not_crash():
    """Results without a tokens field must not break aggregation (backward compat)."""
    results = [
        {"test_case": "case-a", "configuration": "with-skill", "score": 50,
         "overall_pass": True},
    ]

    agg = eval_mod.aggregate(results)
    cell = agg["cells"]["case-a"]["with-skill"]

    assert cell["mean_score"] == 50.0
    # mean_tokens present but zeroed when no token data exists
    assert cell["mean_tokens"]["billed_output_tokens"] == 0


def test_needs_review_trials_still_count_in_token_usage():
    decided = _result('case-a', 'with-skill', 90, _tokens(100, 0, 20, 0))
    undecided = {**_result('case-a', 'with-skill', None, _tokens(300, 0, 60, 0), False),
                 'status': 'needs_review', 'needs_review': True}
    cell = eval_mod.aggregate([decided, undecided])['cells']['case-a']['with-skill']
    assert cell['mean_score'] == 90
    assert cell['needs_review_trials'] == 1
    assert cell['mean_tokens']['input_tokens'] == 200
    assert cell['mean_tokens']['billed_output_tokens'] == 40
