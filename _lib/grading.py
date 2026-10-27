"""Score authority, schema validation, deterministic checker, and semantic jury grading."""

from __future__ import annotations

import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from typing import Any

from _lib.common import (
    Case,
    HarnessError,
    read_json,
    run_command,
    write_json,
)


# Grading model (step 1 part 3 — simplified). `score` (0..100) is the SINGLE
# source of truth. `overall_pass` is DERIVED: pass iff score >= PASS_THRESHOLD.
# The categorical `verdict` (pass/partial/fail) is a DISPLAY-ONLY band also
# derived from the score — it is never an independent input and is never
# cross-checked against `overall_pass` (that cross-check caused a `partial` +
# `overall_pass:true` grade to be wrongly rejected as invalid). The ONE
# categorical signal that is NOT derivable from the score is abstention:
# "insufficient_evidence" means the judge could not evaluate at all (distinct
# from a low score, which is a real judgement). Abstention routes a trial to
# `needs_review`. A deterministic check.py can always compute an answer, so only
# an LLM judge ever abstains.
PASS_THRESHOLD = 70
VERDICTS = ("pass", "partial", "fail", "insufficient_evidence")
PASSING_VERDICTS = ("pass",)
ABSTAIN_VERDICT = "insufficient_evidence"


def score_pass(score: float) -> bool:
    """Derive the boolean pass sense from a 0..100 score. Pure."""
    return score >= PASS_THRESHOLD


def score_band(score: float) -> str:
    """Derive the display band from a 0..100 score. Pure.

    pass    : score >= PASS_THRESHOLD
    partial : 0 < score < PASS_THRESHOLD   (some credit, not enough to pass)
    fail    : score <= 0                   (no credit)
    """
    if score >= PASS_THRESHOLD:
        return "pass"
    if score > 0:
        return "partial"
    return "fail"


def verdict_implies_pass(verdict: str) -> bool:
    """Map a categorical verdict to its boolean pass sense. Pure.

    Retained for backward compatibility: only "pass" is a pass. The current
    model derives both pass and band from the score instead, but a judge may
    still emit a categorical verdict and this keeps its pass-sense well-defined.
    """
    return verdict in PASSING_VERDICTS


def validate_grade(value: dict[str, Any] | None) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if value is None:
        return False, ["grader did not produce a JSON object"]
    allowed_top_level = {"overall_pass", "verdict", "score", "checks", "limitations"}
    unknown_top_level = sorted(set(value) - allowed_top_level)
    if unknown_top_level:
        errors.append("unexpected top-level fields: " + ", ".join(unknown_top_level))
    if not isinstance(value.get("overall_pass"), bool):
        errors.append("overall_pass must be boolean")
    # Optional categorical verdict (display-only in the simplified model). The
    # strict output schema makes it required-but-nullable, so null means "not
    # supplied". When a non-null value is present it only has to be a known enum
    # value. It is NOT cross-checked against `overall_pass`: pass and the band
    # are derived from `score`, so a judge emitting e.g. verdict "partial" with
    # overall_pass true is no longer a contradiction to reject — the score is
    # authoritative. (This cross-check previously discarded valid grades.)
    if value.get("verdict") is not None and value.get("verdict") not in VERDICTS:
        errors.append("verdict must be one of " + ", ".join(VERDICTS))
    score = value.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        errors.append("score must be an integer from 0 to 100")
    checks = value.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
    else:
        allowed_check_fields = {"id", "pass", "verdict", "points", "notes"}
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                errors.append(f"check {index} must be an object")
                continue
            unknown_check_fields = sorted(set(check) - allowed_check_fields)
            if unknown_check_fields:
                errors.append(
                    f"check {index} has unexpected fields: "
                    + ", ".join(unknown_check_fields)
                )
            if not isinstance(check.get("id"), str) or not check["id"]:
                errors.append(f"check {index} needs an id")
            if not isinstance(check.get("pass"), bool):
                errors.append(f"check {index} pass must be boolean")
            if check.get("verdict") is not None and check.get("verdict") not in VERDICTS:
                errors.append(
                    f"check {index} verdict must be one of " + ", ".join(VERDICTS)
                )
            point = check.get("points")
            if (
                not isinstance(point, int)
                or isinstance(point, bool)
                or not 0 <= point <= 100
            ):
                errors.append(f"check {index} points must be 0..100")
            if not isinstance(check.get("notes"), str):
                errors.append(f"check {index} needs notes")
    limitations = value.get("limitations")
    if not isinstance(limitations, list):
        errors.append("limitations must be a list")
    elif any(not isinstance(limitation, str) for limitation in limitations):
        errors.append("every limitation must be a string")
    return not errors, errors


def validate_grade_schema(schema: Path) -> list[str]:
    """Check the small set of schema guarantees required by this harness."""
    try:
        value = json.loads(schema.read_text(encoding="utf-8"))
    except OSError as error:
        return [f"cannot read grader schema: {error}"]
    except json.JSONDecodeError as error:
        return [f"grader schema is not valid JSON: {error}"]
    if not isinstance(value, dict):
        return ["grader schema must contain a JSON object"]

    errors: list[str] = []
    required_fields = {"overall_pass", "score", "checks", "limitations"}
    if value.get("type") != "object":
        errors.append("grader schema root type must be object")
    required = value.get("required")
    if not isinstance(required, list) or not required_fields.issubset(required):
        errors.append("grader schema is missing required grade fields")
    properties = value.get("properties")
    if not isinstance(properties, dict) or not required_fields.issubset(properties):
        errors.append("grader schema is missing grade field definitions")
    return errors


def run_checker(
    case: Case, workspace: Path, run_dir: Path, timeout: int
) -> dict[str, Any]:
    if case.checker is None:
        return {"status": "not_requested", "result": None, "errors": []}
    grade_dir = run_dir / "grades" / "deterministic"
    command = [
        sys.executable,
        str(case.checker),
        "--workspace",
        str(workspace),
        "--trace",
        str(run_dir / "worker" / "trace.jsonl"),
        "--run",
        str(run_dir / "run.json"),
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = run_command(
        command,
        cwd=case.root,
        stdout_path=grade_dir / "output.txt",
        stderr_path=grade_dir / "stderr.log",
        timeout=timeout,
        label="deterministic grader",
        environment=environment,
    )
    result = read_json(grade_dir / "output.txt")
    valid, errors = validate_grade(result)
    if completed.returncode != 0:
        errors.append(f"grader exited with code {completed.returncode}")
    if isinstance(result, dict) and result.get("verdict") == ABSTAIN_VERDICT:
        valid = False
        errors.append("deterministic graders cannot abstain; report a score or an explicit checker error")
    status = "completed" if completed.returncode == 0 and valid else "invalid"
    if status == "completed" and isinstance(result, dict):
        # Score is authoritative for deterministic graders just as for the
        # semantic jury: derive overall_pass and the display band from score so
        # a check.py that sets overall_pass = all(c["pass"] ...) cannot report
        # Score 80 | Pass No.
        score = int(result["score"])
        result = {
            **result,
            "overall_pass": score_pass(score),
            "verdict": score_band(score),
        }
    wrapped = {
        "status": status,
        "exit_code": completed.returncode,
        "result": result,
        "errors": errors,
    }
    write_json(grade_dir / "result.json", wrapped)
    return wrapped


def combine_semantic_judges(
    judges: list[dict[str, Any]],
    requested_count: int,
) -> dict[str, Any]:
    """Combine independent, schema-valid semantic grades into one jury result."""
    if requested_count < 1:
        raise HarnessError("semantic judge count must be at least 1")

    judges = sorted(judges, key=lambda judge: judge["judge"])
    valid_judges = [judge for judge in judges if judge["status"] == "completed"]

    # A valid judge whose categorical verdict is "insufficient_evidence" ABSTAINS:
    # it is excluded from both the pass-vote tally and the score median. Quorum is
    # measured against the DECIDING judges (valid minus abstainers), so an honest
    # "cannot decide" is not silently reported as FAIL.
    def abstains(judge: dict[str, Any]) -> bool:
        result = judge.get("result")
        return isinstance(result, dict) and result.get("verdict") == ABSTAIN_VERDICT

    abstaining_judges = [judge for judge in valid_judges if abstains(judge)]
    deciding_judges = [judge for judge in valid_judges if not abstains(judge)]
    quorum = requested_count // 2 + 1
    errors = [
        f"judge {judge['judge']}: "
        + "; ".join(judge["errors"] or ["grader command failed"])
        for judge in judges
        if judge["status"] != "completed"
    ]
    if len(valid_judges) < quorum:
        return {
            "status": "invalid",
            "result": None,
            "errors": [
                f"only {len(valid_judges)}/{requested_count} valid judges; quorum is {quorum}"
            ]
            + errors,
            "quorum": quorum,
            "judges": judges,
        }

    # Enough judges ran, but too few could DECIDE (the rest abstained). This is a
    # distinct terminal state from "invalid" (graders errored) and from "fail"
    # (judges decided it failed): there is not enough evidence to decide.
    if len(deciding_judges) < quorum:
        return {
            "status": "needs_review",
            "result": None,
            "errors": [
                f"only {len(deciding_judges)}/{requested_count} deciding judges "
                f"(quorum {quorum}); {len(abstaining_judges)} abstained with "
                f"{ABSTAIN_VERDICT}"
            ]
            + errors,
            "quorum": quorum,
            "deciding_judges": len(deciding_judges),
            "abstaining_judges": len(abstaining_judges),
            "judges": judges,
        }

    scores = [int(judge["result"]["score"]) for judge in deciding_judges]
    # Score is authoritative: the consensus score is the median of deciding
    # judges, and pass + band are DERIVED from it via PASS_THRESHOLD rather than
    # by counting each judge's self-reported `overall_pass` boolean. (A judge may
    # report overall_pass inconsistently with its own score; the score wins.)
    consensus_score = round(median(scores))
    consensus_pass = score_pass(consensus_score)
    consensus_band = score_band(consensus_score)
    # For transparency, still note how many judges' OWN score cleared the bar.
    passing_votes = sum(score_pass(s) for s in scores)
    limitations = [
        limitation
        for judge in deciding_judges
        for limitation in judge["result"].get("limitations", [])
    ]
    limitations.extend(errors)
    consensus = {
        "overall_pass": consensus_pass,
        "verdict": consensus_band,
        "score": consensus_score,
        "checks": [
            {
                "id": "semantic-jury",
                "pass": consensus_pass,
                "verdict": consensus_band,
                "points": consensus_score,
                "notes": (
                    f"consensus score {consensus_score} (median of {scores}) -> "
                    f"band {consensus_band!r}, pass={consensus_pass} at threshold "
                    f"{PASS_THRESHOLD}; {passing_votes}/{len(deciding_judges)} "
                    f"deciding judges individually cleared the bar "
                    f"({len(abstaining_judges)} abstained of {requested_count} "
                    f"requested)."
                ),
            }
        ],
        "limitations": list(dict.fromkeys(limitations)),
    }
    return {
        "status": "completed",
        "result": consensus,
        "errors": errors,
        "quorum": quorum,
        "deciding_judges": len(deciding_judges),
        "abstaining_judges": len(abstaining_judges),
        "judges": judges,
    }


def run_semantic_grader(
    case: Case,
    run_dir: Path,
    schema: Path,
    timeout: int,
    model: str | None,
    judge_count: int,
) -> dict[str, Any]:
    if case.expected is None:
        return {
            "status": "not_requested",
            "result": None,
            "errors": [],
            "judges": [],
        }

    semantic_dir = run_dir / "grades" / "semantic"
    grader_input = semantic_dir / "input"
    grader_input.mkdir(parents=True, exist_ok=False)
    shutil.copy2(case.expected, grader_input / "expected.md")
    shutil.copy2(schema, grader_input / "grade.schema.json")

    prompt = f"""Evaluate the completed worker trial. Do not modify files.

Read:
- grades/semantic/input/expected.md
- workspace/ (the worker's final repository)
- worker/result.json
- worker/metrics.json
- worker/trace.jsonl
- worker/turns/, for each prompt, response, trace, diagnostic log, and state
- worker/subagents/, when present (captured subagent rollout traces and manifest.json)
- git/, for the complete base-relative Git evidence
- grades/deterministic/result.json, when present
- run.json

Use only observable evidence. Missing evidence is a limitation, not success.
Return only JSON matching grades/semantic/input/grade.schema.json.
`score` (0..100) is authoritative (`>= {PASS_THRESHOLD}` passes). Record points earned per
criterion in `checks[].points` and explain every deduction in `checks[].notes`. If an
essential pass condition or hard gate in `expected.md` fails, cap `score` below
{PASS_THRESHOLD} (or at 0 for a complete disqualifier) even if other criteria earned points.
"""
    if judge_count < 1:
        raise HarnessError("semantic judge count must be at least 1")

    def run_judge(index: int) -> dict[str, Any]:
        judge_dir = semantic_dir / f"judge-{index:02d}"
        output = judge_dir / "output.json"
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-schema",
            str(grader_input / "grade.schema.json"),
            "-o",
            str(output),
        ]
        if model:
            command.extend(["--model", model])
        command.append(
            f"You are semantic judge {index} of {judge_count}. "
            "Grade independently of the other judges.\n\n" + prompt
        )
        completed = run_command(
            command,
            cwd=run_dir,
            stdout_path=judge_dir / "trace.jsonl",
            stderr_path=judge_dir / "stderr.log",
            timeout=timeout,
            label=f"semantic judge {index}/{judge_count}",
        )
        result = read_json(output)
        valid, errors = validate_grade(result)
        if completed.returncode != 0:
            errors.append(f"judge exited with code {completed.returncode}")
        status = "completed" if completed.returncode == 0 and valid else "invalid"
        wrapped = {
            "judge": index,
            "status": status,
            "exit_code": completed.returncode,
            "result": result,
            "errors": errors,
        }
        return wrapped

    judges: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=judge_count) as executor:
        futures = {
            executor.submit(run_judge, index): index
            for index in range(1, judge_count + 1)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                judges.append(future.result())
            except Exception as error:  # Preserve the other independent judges.
                wrapped = {
                    "judge": index,
                    "status": "invalid",
                    "exit_code": None,
                    "result": None,
                    "errors": [f"unexpected grader failure: {error}"],
                }
                judges.append(wrapped)
    wrapped = combine_semantic_judges(judges, judge_count)
    write_json(semantic_dir / "result.json", wrapped)
    return wrapped


def grade_failure_reasons(label: str, grade: dict[str, Any]) -> list[str]:
    """Extract concise human-readable failures from a grader wrapper."""
    reasons = [f"{label}: {error}" for error in grade.get("errors", [])]
    candidates = []
    result = grade.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    for judge in grade.get("judges", []):
        judge_result = judge.get("result") if isinstance(judge, dict) else None
        if isinstance(judge_result, dict):
            candidates.append(judge_result)
    for candidate in candidates:
        for check in candidate.get("checks", []):
            if isinstance(check, dict) and check.get("pass") is False:
                check_id = check.get("id", "failed-check")
                notes = check.get("notes", "No explanation was provided.")
                reasons.append(f"{label}/{check_id}: {notes}")
    return reasons


def _grade_passed(grade: dict[str, Any]) -> bool:
    """Return whether a completed grader passed, using `score >= PASS_THRESHOLD`
    as the single source of truth when a numeric score is present."""
    result = grade.get("result")
    if not isinstance(result, dict):
        return False
    score = result.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return score_pass(int(score))
    return bool(result.get("overall_pass"))


def grade_outcome(deterministic: dict, semantic: dict) -> dict[str, Any]:
    """Combine deterministic and semantic grades into the trial verdict.

    Pure (no I/O). A grade dict has `status` and `result`. Only grades whose
    status is not "not_requested" count. `graders_valid` requires at least one
    requested grade and all requested grades "completed"; `graders_pass`
    requires every requested result's `score >= PASS_THRESHOLD` (`score_pass`).
    When both graders produce a result, `headline` uses the lower-scoring result
    (`min(deterministic_score, semantic_score)`) so a failing grader is never
    masked by a passing one.
    """
    requested_grades = [
        grade
        for grade in (deterministic, semantic)
        if grade["status"] != "not_requested"
    ]
    graders_valid = bool(requested_grades) and all(
        grade["status"] == "completed" for grade in requested_grades
    )
    graders_pass = graders_valid and all(
        _grade_passed(grade) for grade in requested_grades
    )
    # A trial "needs review" when no requested grader errored outright but at
    # least one reached the distinct `needs_review` terminal state (jury
    # abstention) — an honest "not enough evidence to decide", neither pass nor
    # fail. It never overrides a genuine grader error ("invalid").
    any_invalid = any(
        grade["status"] not in ("completed", "needs_review")
        for grade in requested_grades
    )
    needs_review = (
        bool(requested_grades)
        and not any_invalid
        and not any(grade["status"] == "completed" and not _grade_passed(grade) for grade in requested_grades)
        and any(grade["status"] == "needs_review" for grade in requested_grades)
    )
    deterministic_result = deterministic.get("result")
    semantic_result = semantic.get("result")
    if isinstance(deterministic_result, dict) and isinstance(semantic_result, dict):
        det_score = deterministic_result.get("score")
        sem_score = semantic_result.get("score")
        if (
            isinstance(det_score, (int, float))
            and not isinstance(det_score, bool)
            and isinstance(sem_score, (int, float))
            and not isinstance(sem_score, bool)
            and det_score < sem_score
        ):
            chosen = deterministic_result
        else:
            chosen = semantic_result
    elif isinstance(semantic_result, dict):
        chosen = semantic_result
    elif isinstance(deterministic_result, dict):
        chosen = deterministic_result
    else:
        chosen = None

    if isinstance(chosen, dict):
        headline = dict(chosen)
        raw_score = headline.get("score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            headline["overall_pass"] = score_pass(int(raw_score))
            headline["verdict"] = score_band(int(raw_score))
    else:
        headline = None
    return {
        "graders_valid": graders_valid,
        "graders_pass": graders_pass,
        "needs_review": needs_review,
        "headline": headline,
    }
