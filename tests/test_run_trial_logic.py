"""Tests for pure decision logic extracted from run_trial.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.harness import classify_invocation, grade_outcome  # noqa: E402


def test_no_skill_baseline():
    # No skill injected -> always baseline, regardless of prompt wording.
    assert classify_invocation(False, ["Use $code-review please"], "code-review") == "no-skill-baseline"
    assert classify_invocation(False, ["review the diff"], "code-review") == "no-skill-baseline"


def test_explicit_when_skill_named_with_dollar():
    assert classify_invocation(True, ["Use $code-review to review"], "code-review") == "explicit"


def test_explicit_when_skill_named_with_slash():
    assert classify_invocation(True, ["Run /code-review now"], "code-review") == "explicit"


def test_implicit_candidate_when_skill_present_but_not_named():
    assert classify_invocation(True, ["Review the implementation"], "code-review") == "implicit-candidate"


def test_explicit_detected_in_any_turn():
    # Named in a later interaction turn, not the first.
    turns = ["Please review the code", "Actually use $code-review"]
    assert classify_invocation(True, turns, "code-review") == "explicit"


# --- grade_outcome ---

NR = {"status": "not_requested", "result": None}


def _grade(status, overall_pass=None, score=None):
    r = None if overall_pass is None else {"overall_pass": overall_pass, "score": score}
    return {"status": status, "result": r}


def test_grade_outcome_both_pass():
    det = _grade("completed", True, 100)
    sem = _grade("completed", True, 90)
    out = grade_outcome(det, sem)
    assert out["graders_valid"] is True
    assert out["graders_pass"] is True
    # headline prefers semantic
    assert out["headline"]["score"] == 90


def test_grade_outcome_one_fails():
    det = _grade("completed", False, 40)
    sem = _grade("completed", True, 90)
    out = grade_outcome(det, sem)
    assert out["graders_valid"] is True
    assert out["graders_pass"] is False


def test_grade_outcome_invalid_when_a_requested_grader_errored():
    det = _grade("completed", True, 100)
    sem = _grade("error", None, None)
    out = grade_outcome(det, sem)
    assert out["graders_valid"] is False
    assert out["graders_pass"] is False


def test_grade_outcome_deterministic_only_headline():
    det = _grade("completed", True, 75)
    out = grade_outcome(det, NR)
    assert out["graders_valid"] is True
    assert out["graders_pass"] is True
    assert out["headline"]["score"] == 75


def test_grade_outcome_nothing_requested_is_invalid():
    out = grade_outcome(NR, NR)
    assert out["graders_valid"] is False
    assert out["graders_pass"] is False
    assert out["headline"] is None
