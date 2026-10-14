"""Tests for step 1 part 2: categorical verdict + insufficient_evidence
abstention + relaxed sum(points)==score invariant.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.harness import (  # noqa: E402
    ABSTAIN_VERDICT,
    PASS_THRESHOLD,
    combine_semantic_judges,
    grade_outcome,
    score_band,
    score_pass,
    validate_grade,
    verdict_implies_pass,
)


# --------------------------------------------------------------------------
# score_pass / score_band — the simplified model's single source of truth
# --------------------------------------------------------------------------


def test_score_pass_threshold():
    assert score_pass(PASS_THRESHOLD) is True
    assert score_pass(PASS_THRESHOLD - 1) is False
    assert score_pass(100) is True
    assert score_pass(0) is False


def test_score_band_mapping():
    assert score_band(100) == "pass"
    assert score_band(PASS_THRESHOLD) == "pass"
    assert score_band(PASS_THRESHOLD - 1) == "partial"
    assert score_band(1) == "partial"
    assert score_band(0) == "fail"


def test_consensus_pass_follows_median_score_not_emitted_bool():
    # Score is authoritative: even if judges self-report overall_pass=True, a
    # median score below the threshold must yield a non-pass consensus.
    judges = [
        _judge(1, True, 50),
        _judge(2, True, 55),
        _judge(3, True, 60),
    ]
    out = combine_semantic_judges(judges, 3)
    assert out["status"] == "completed"
    assert out["result"]["score"] == 55  # median
    assert out["result"]["overall_pass"] is False  # 55 < 70, despite 3 "True"s
    assert out["result"]["verdict"] == "partial"


def test_consensus_band_is_derived_and_present():
    judges = [_judge(1, True, 90), _judge(2, True, 95), _judge(3, True, 100)]
    out = combine_semantic_judges(judges, 3)
    assert out["result"]["overall_pass"] is True
    assert out["result"]["verdict"] == "pass"


# --------------------------------------------------------------------------
# verdict_implies_pass
# --------------------------------------------------------------------------


def test_verdict_implies_pass_mapping():
    assert verdict_implies_pass("pass") is True
    assert verdict_implies_pass("fail") is False
    assert verdict_implies_pass("partial") is False
    assert verdict_implies_pass("insufficient_evidence") is False


# --------------------------------------------------------------------------
# validate_grade — backward compat (verdict absent) + relaxed invariant
# --------------------------------------------------------------------------


def _grade(**overrides):
    base = {
        "overall_pass": True,
        "score": 80,
        "checks": [{"id": "c1", "pass": True, "points": 80, "notes": "ok"}],
        "limitations": [],
    }
    base.update(overrides)
    return base


def test_verdict_absent_is_valid_regression_guard():
    valid, errors = validate_grade(_grade())
    assert valid, errors


def test_score_need_not_equal_sum_points_now_valid():
    # The sum(points)==score invariant is relaxed: score decoupled from points.
    valid, errors = validate_grade(_grade(score=50))  # check points=80, score=50
    assert valid, errors


def test_score_still_range_validated():
    valid, errors = validate_grade(_grade(score=150))
    assert not valid
    assert any("score" in e for e in errors)


def test_check_points_still_range_validated():
    bad = _grade(checks=[{"id": "c1", "pass": True, "points": 200, "notes": "x"}])
    valid, errors = validate_grade(bad)
    assert not valid
    assert any("points" in e for e in errors)


# --------------------------------------------------------------------------
# validate_grade — verdict present
# --------------------------------------------------------------------------


def test_grade_level_verdict_pass_agrees():
    valid, errors = validate_grade(_grade(verdict="pass", overall_pass=True))
    assert valid, errors


def test_grade_level_verdict_null_is_treated_as_absent():
    # The strict output schema makes verdict required-but-nullable, so the model
    # emits verdict:null when it has no categorical verdict. That must validate
    # exactly like an absent verdict (regression guard for the schema-400 bug).
    valid, errors = validate_grade(_grade(verdict=None, overall_pass=True))
    assert valid, errors


def test_check_level_verdict_null_is_treated_as_absent():
    grade = _grade(
        checks=[{"id": "c1", "pass": True, "verdict": None, "points": 80,
                 "notes": "ok"}],
    )
    valid, errors = validate_grade(grade)
    assert valid, errors


def test_grade_level_verdict_fail_agrees():
    valid, errors = validate_grade(_grade(verdict="fail", overall_pass=False))
    assert valid, errors


def test_abstaining_grade_validates():
    valid, errors = validate_grade(
        _grade(verdict=ABSTAIN_VERDICT, overall_pass=False)
    )
    assert valid, errors


def test_verdict_disagreement_is_no_longer_rejected():
    # Simplified model: verdict is display-only and NOT cross-checked against
    # overall_pass (score is authoritative). A grade whose verdict and
    # overall_pass "disagree" must still validate — this is the regression guard
    # for the partial+overall_pass:true bug.
    valid, errors = validate_grade(_grade(verdict="pass", overall_pass=False))
    assert valid, errors
    valid2, errors2 = validate_grade(_grade(verdict="partial", overall_pass=True))
    assert valid2, errors2


def test_unknown_verdict_rejected():
    valid, errors = validate_grade(_grade(verdict="maybe", overall_pass=True))
    assert not valid
    assert any("verdict" in e for e in errors)


def test_per_check_verdict_accepted():
    g = _grade(
        checks=[
            {
                "id": "c1",
                "pass": True,
                "verdict": "pass",
                "points": 80,
                "notes": "ok",
            }
        ]
    )
    valid, errors = validate_grade(g)
    assert valid, errors


def test_per_check_unknown_verdict_rejected():
    g = _grade(
        checks=[
            {"id": "c1", "pass": True, "verdict": "nope", "points": 80, "notes": "x"}
        ]
    )
    valid, errors = validate_grade(g)
    assert not valid
    assert any("verdict" in e for e in errors)


# --------------------------------------------------------------------------
# combine_semantic_judges — abstention + needs_review
# --------------------------------------------------------------------------


def _judge(index, overall_pass, score, verdict=None, status="completed"):
    result = None
    if status == "completed":
        result = {
            "overall_pass": overall_pass,
            "score": score,
            "limitations": [],
        }
        if verdict is not None:
            result["verdict"] = verdict
    return {"judge": index, "status": status, "result": result, "errors": []}


def test_no_abstentions_normal_pass():
    judges = [
        _judge(1, True, 90),
        _judge(2, True, 80),
        _judge(3, False, 40),
    ]
    out = combine_semantic_judges(judges, 3)
    assert out["status"] == "completed"
    assert out["result"]["overall_pass"] is True  # 2/3 pass, quorum 2


def test_some_abstain_but_quorum_still_met():
    # 3 requested, quorum 2. One abstains, two decide and both pass.
    judges = [
        _judge(1, True, 90),
        _judge(2, True, 85),
        _judge(3, False, 0, verdict=ABSTAIN_VERDICT),
    ]
    out = combine_semantic_judges(judges, 3)
    assert out["status"] == "completed"
    assert out["result"]["overall_pass"] is True
    assert out["deciding_judges"] == 2
    assert out["abstaining_judges"] == 1
    # Abstainer's score (0) must NOT drag the median.
    assert out["result"]["score"] == 88  # median(90, 85) = 87.5 -> 88


def test_all_judges_abstain_needs_review():
    judges = [
        _judge(1, False, 0, verdict=ABSTAIN_VERDICT),
        _judge(2, False, 0, verdict=ABSTAIN_VERDICT),
        _judge(3, False, 0, verdict=ABSTAIN_VERDICT),
    ]
    out = combine_semantic_judges(judges, 3)
    assert out["status"] == "needs_review"
    assert out["result"] is None  # no crash on median([]) / empty votes
    assert out["abstaining_judges"] == 3


def test_too_many_abstain_below_quorum_needs_review():
    # 3 requested, quorum 2. Two abstain -> only 1 deciding < quorum.
    judges = [
        _judge(1, True, 90),
        _judge(2, False, 0, verdict=ABSTAIN_VERDICT),
        _judge(3, False, 0, verdict=ABSTAIN_VERDICT),
    ]
    out = combine_semantic_judges(judges, 3)
    assert out["status"] == "needs_review"
    assert out["deciding_judges"] == 1


def test_invalid_still_wins_over_abstention():
    # Not enough valid judges at all -> invalid, not needs_review.
    judges = [
        _judge(1, True, 90),
        _judge(2, None, None, status="error"),
        _judge(3, None, None, status="error"),
    ]
    out = combine_semantic_judges(judges, 3)
    assert out["status"] == "invalid"


def test_even_jury_tie_among_deciding_still_fails():
    # 2 requested, quorum 2. One pass, one fail -> 1 passing vote < quorum.
    judges = [
        _judge(1, True, 90),
        _judge(2, False, 10),
    ]
    out = combine_semantic_judges(judges, 2)
    assert out["status"] == "completed"
    assert out["result"]["overall_pass"] is False


# --------------------------------------------------------------------------
# grade_outcome — needs_review propagation
# --------------------------------------------------------------------------

NR = {"status": "not_requested", "result": None}


def _wrapped(status, overall_pass=None, score=None):
    r = None if overall_pass is None else {"overall_pass": overall_pass, "score": score}
    return {"status": status, "result": r}


def test_grade_outcome_needs_review_surfaced():
    det = _wrapped("completed", True, 100)
    sem = _wrapped("needs_review", None, None)
    out = grade_outcome(det, sem)
    assert out["needs_review"] is True
    assert out["graders_valid"] is False  # not fully graded
    assert out["graders_pass"] is False


def test_grade_outcome_invalid_overrides_needs_review():
    det = _wrapped("error", None, None)
    sem = _wrapped("needs_review", None, None)
    out = grade_outcome(det, sem)
    # A genuine grader error is not an honest abstention.
    assert out["needs_review"] is False


def test_grade_outcome_no_needs_review_when_all_decided():
    det = _wrapped("completed", True, 100)
    sem = _wrapped("completed", True, 90)
    out = grade_outcome(det, sem)
    assert out["needs_review"] is False
    assert out["graders_pass"] is True
