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


def _cell(score, passed, total, output=0, reasoning=0):
    return {
        "mean_score": score,
        "passed_trials": passed,
        "total_trials": total,
        "mean_tokens": {
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
        },
    }


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
