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
    # headline takes min(det_score, sem_score) = min(100, 90) = 90
    assert out["headline"]["score"] == 90
    assert out["headline"]["overall_pass"] is True
    assert out["headline"]["verdict"] == "pass"


def test_grade_outcome_one_fails():
    det = _grade("completed", False, 40)
    sem = _grade("completed", True, 90)
    out = grade_outcome(det, sem)
    assert out["graders_valid"] is True
    assert out["graders_pass"] is False
    # headline uses the lower-scoring result so a failing deterministic grader
    # is never hidden behind a passing semantic score
    assert out["headline"]["score"] == 40
    assert out["headline"]["overall_pass"] is False
    assert out["headline"]["verdict"] == "partial"


def test_grade_outcome_score_is_authoritative_over_self_reported_bool():
    # A deterministic checker that sets overall_pass=False via all(c["pass"])
    # while scoring 80 (>= 70) still passes under the unified score >= 70 rule.
    det = _grade("completed", False, 80)
    out = grade_outcome(det, NR)
    assert out["graders_valid"] is True
    assert out["graders_pass"] is True
    assert out["headline"]["score"] == 80
    assert out["headline"]["overall_pass"] is True
    assert out["headline"]["verdict"] == "pass"

    # Conversely, overall_pass=True with score 55 (< 70) fails.
    det_low = _grade("completed", True, 55)
    out_low = grade_outcome(det_low, NR)
    assert out_low["graders_valid"] is True
    assert out_low["graders_pass"] is False
    assert out_low["headline"]["score"] == 55
    assert out_low["headline"]["overall_pass"] is False
    assert out_low["headline"]["verdict"] == "partial"


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

