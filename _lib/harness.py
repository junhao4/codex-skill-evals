#!/usr/bin/env python3
"""Internal harness for evaluating project-local Codex skills."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
EXPERIMENT_NAME = re.compile(r"^experiment-(\d+)$")
INTERACTION_NAME = re.compile(r"^\d{2}-.+\.md$")
SPINNER_FRAMES = ("◐", "◓", "◑", "◒")


class HarnessError(ValueError):
    """Raised when an evaluation definition or run is invalid."""


@dataclass(frozen=True)
class Case:
    name: str
    root: Path
    fixture: Path
    prompt: Path
    expected: Path | None
    checker: Path | None
    supporting_skills: tuple[Path, ...]
    interactions: tuple[Path, ...]


class TerminalProgress:
    """Render one spinner line for all concurrently running subprocesses."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active: dict[int, tuple[str, float]] = {}
        self.next_id = 0
        self.rendered_width = 0
        self.thread: threading.Thread | None = None

    def _clear(self) -> None:
        if self.rendered_width:
            sys.stdout.write("\r" + " " * self.rendered_width + "\r")
            self.rendered_width = 0

    def _animate(self) -> None:
        frame = 0
        while True:
            time.sleep(0.12)
            with self.lock:
                if not self.active:
                    continue
                running = list(self.active.values())
                oldest = min(started for _, started in running)
                if len(running) == 1:
                    description = running[0][0]
                else:
                    description = f"{len(running)} operations"
                line = (
                    f"      {SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]} "
                    f"running: {description} "
                    f"({elapsed(time.monotonic() - oldest)} elapsed)"
                )
                self._clear()
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
                self.rendered_width = len(line)
                frame += 1

    def start(self, label: str, started: float) -> int | None:
        if not sys.stdout.isatty():
            print(f"      started: {label}", flush=True)
            return None
        with self.lock:
            self._clear()
            print(f"      started: {label}", flush=True)
            self.next_id += 1
            token = self.next_id
            self.active[token] = (label, started)
            if self.thread is None:
                self.thread = threading.Thread(target=self._animate, daemon=True)
                self.thread.start()
            return token

    def finish(self, token: int | None, label: str, status: str, duration: str) -> None:
        if token is None:
            print(f"      {status}: {label} ({duration})", flush=True)
            return
        with self.lock:
            self.active.pop(token, None)
            self._clear()
            print(f"      {status}: {label} ({duration})", flush=True)


TERMINAL_PROGRESS = TerminalProgress()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def trace_thread_id(trace: Path) -> str | None:
    """Return the persisted Codex thread ID recorded in a JSONL trace."""
    if not trace.is_file():
        return None
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
    return None


def concatenate_files(sources: list[Path], destination: Path) -> None:
    """Combine text evidence files while preserving valid line boundaries."""
    with destination.open("w", encoding="utf-8") as output:
        for source in sources:
            if not source.is_file():
                continue
            content = source.read_text(encoding="utf-8", errors="replace")
            output.write(content)
            if content and not content.endswith("\n"):
                output.write("\n")


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def untracked_paths(repository: Path) -> list[str]:
    result = git(repository, "ls-files", "--others", "--exclude-standard", "-z")
    if result.returncode != 0:
        raise HarnessError(f"could not inspect untracked files in {repository}")
    return sorted(name for name in result.stdout.split("\0") if name)


def require_clean_repository(repository: Path, label: str) -> str:
    if not repository.is_dir() or not (repository / ".git").is_dir():
        raise HarnessError(f"{label} must be a standalone Git repository: {repository}")
    revision = git(repository, "rev-parse", "HEAD")
    status = git(repository, "status", "--short")
    if revision.returncode != 0 or status.returncode != 0:
        raise HarnessError(f"could not inspect {label}: {repository}")
    if status.stdout.strip():
        raise HarnessError(f"{label} must be clean before a trial: {repository}")
    return revision.stdout.strip()


def initialize_fixture_repository(workspace: Path) -> str:
    """Create reproducible Git history around a copied plain-directory fixture."""
    initialized = git(workspace, "init", "--quiet")
    if initialized.returncode != 0:
        raise HarnessError(f"could not initialize fixture repository: {workspace}")

    common_commit_arguments = (
        "-c",
        "user.name=Codex Eval Harness",
        "-c",
        "user.email=eval-harness@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
    )
    base = git(
        workspace,
        *common_commit_arguments,
        "--allow-empty",
        "-m",
        "eval: create empty fixture base",
    )
    if base.returncode != 0 or git(workspace, "tag", "fixture-base").returncode != 0:
        raise HarnessError(f"could not create fixture-base revision: {workspace}")

    staged = git(workspace, "add", "--all", "--force")
    target = git(
        workspace,
        *common_commit_arguments,
        "--allow-empty",
        "-m",
        "eval: capture fixture target",
    )
    tagged = git(workspace, "tag", "fixture-target")
    if staged.returncode != 0 or target.returncode != 0 or tagged.returncode != 0:
        raise HarnessError(f"could not create fixture-target revision: {workspace}")
    return require_clean_repository(workspace, "fixture target")


def commit_workspace_base(workspace: Path) -> str:
    """Commit harness-injected inputs so later evidence contains worker changes only."""
    staged = git(workspace, "add", "--all", "--force")
    if staged.returncode != 0:
        raise HarnessError(
            f"could not stage the controlled workspace base: {workspace}"
        )
    committed = git(
        workspace,
        "-c",
        "user.name=Codex Eval Harness",
        "-c",
        "user.email=eval-harness@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-m",
        "eval: prepare controlled workspace",
    )
    if committed.returncode != 0:
        raise HarnessError(
            f"could not commit the controlled workspace base: {workspace}"
        )
    return require_clean_repository(workspace, "controlled workspace base")


def directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def discover_skills(evals_root: Path, skills_root: Path) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    if not evals_root.is_dir():
        return discovered
    for candidate in sorted(evals_root.iterdir()):
        if (
            not candidate.is_dir()
            or candidate.name.startswith("_")
            or not SKILL_NAME.fullmatch(candidate.name)
        ):
            continue
        skill_file = skills_root / candidate.name / "SKILL.md"
        test_cases = candidate / "test-cases"
        if skill_file.is_file() and test_cases.is_dir():
            discovered[candidate.name] = candidate
    return discovered


def discover_cases(eval_root: Path, skill_root: Path) -> dict[str, Case]:
    cases_root = eval_root / "test-cases"
    if not cases_root.is_dir():
        raise HarnessError(f"missing test-cases directory: {cases_root}")

    cases: dict[str, Case] = {}
    for root in sorted(cases_root.iterdir()):
        if not root.is_dir() or root.name.startswith("_"):
            continue
        prompt = root / "prompt.md"
        fixture = root / "fixture"
        expected = root / "expected.md"
        checker = root / "check.py"
        supporting_root = root / "supporting-skills"
        interactions_root = root / "interactions"
        if not prompt.is_file():
            raise HarnessError(f"test case {root.name} is missing prompt.md")
        if not fixture.is_dir():
            raise HarnessError(f"test case {root.name} is missing fixture/")
        if (fixture / ".git").exists():
            raise HarnessError(
                f"test case {root.name} fixture must not contain .git; "
                "the harness creates disposable Git history"
            )
        if (fixture / ".agents").exists():
            raise HarnessError(
                f"test case {root.name} fixture must not contain .agents"
            )
        if not expected.is_file() and not checker.is_file():
            raise HarnessError(
                f"test case {root.name} needs expected.md, check.py, or both"
            )

        supporting_skills: list[Path] = []
        if supporting_root.exists() and not supporting_root.is_dir():
            raise HarnessError(
                f"test case {root.name} supporting-skills must be a directory"
            )
        if supporting_root.is_dir():
            for supporting in sorted(supporting_root.iterdir()):
                if supporting.name.startswith("."):
                    continue
                if not supporting.is_dir() or not SKILL_NAME.fullmatch(supporting.name):
                    raise HarnessError(
                        f"test case {root.name} has invalid supporting skill: {supporting.name}"
                    )
                if supporting.name == skill_root.name:
                    raise HarnessError(
                        f"test case {root.name} repeats the target skill under supporting-skills"
                    )
                if not (supporting / "SKILL.md").is_file():
                    raise HarnessError(
                        f"supporting skill {supporting.name} is missing SKILL.md"
                    )
                supporting_skills.append(supporting)

        interactions: list[Path] = []
        if interactions_root.exists() and not interactions_root.is_dir():
            raise HarnessError(
                f"test case {root.name} interactions must be a directory"
            )
        if interactions_root.is_dir():
            for interaction in sorted(interactions_root.iterdir()):
                if interaction.name.startswith("."):
                    continue
                if not interaction.is_file() or not INTERACTION_NAME.fullmatch(
                    interaction.name
                ):
                    raise HarnessError(
                        f"interaction {interaction.name} must match NN-description.md"
                    )
                interactions.append(interaction)

        cases[root.name] = Case(
            name=root.name,
            root=root,
            fixture=fixture,
            prompt=prompt,
            expected=expected if expected.is_file() else None,
            checker=checker if checker.is_file() else None,
            supporting_skills=tuple(supporting_skills),
            interactions=tuple(interactions),
        )
    if not cases:
        raise HarnessError(f"no test cases found in {cases_root}")
    return cases


def next_experiment_id(runs_root: Path) -> str:
    numbers: list[int] = []
    if runs_root.is_dir():
        for candidate in runs_root.iterdir():
            match = EXPERIMENT_NAME.fullmatch(candidate.name)
            if match:
                numbers.append(int(match.group(1)))
    return f"experiment-{max(numbers, default=0) + 1:03d}"


def elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def run_command(
    command: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
    label: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    progress_token = TERMINAL_PROGRESS.start(label, started)
    completed: subprocess.CompletedProcess[str]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_file,
            stderr_path.open("w", encoding="utf-8") as stderr_file,
        ):
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=environment,
                )
            except FileNotFoundError:
                completed = subprocess.CompletedProcess(
                    command, 127, "", "command not found"
                )
            except subprocess.TimeoutExpired:
                completed = subprocess.CompletedProcess(command, 124, "", "timeout")
    except BaseException:
        TERMINAL_PROGRESS.finish(
            progress_token,
            label,
            "stopped",
            elapsed(time.monotonic() - started),
        )
        raise

    duration = elapsed(time.monotonic() - started)
    if completed.returncode == 0:
        status = "completed"
    elif completed.returncode == 124:
        status = "timed out"
    else:
        status = f"failed with exit code {completed.returncode}"
    TERMINAL_PROGRESS.finish(progress_token, label, status, duration)
    return completed


def git_state(workspace: Path) -> dict[str, Any]:
    revision = git(workspace, "rev-parse", "HEAD")
    status = git(workspace, "status", "--short")
    return {
        "revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "status": status.stdout.splitlines() if status.returncode == 0 else [],
        "clean": status.returncode == 0 and not status.stdout.strip(),
    }


def trace_metrics(trace: Path, skill_name: str | None = None) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    parse_errors = 0
    if trace.is_file():
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(value, dict):
                events.append(value)

    commands: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    for event in events:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            commands.append(item)
        if event.get("type") == "turn.completed" and isinstance(
            event.get("usage"), dict
        ):
            usage = event["usage"]
            input_tokens += int(usage.get("input_tokens", 0) or 0)
            output_tokens += int(usage.get("output_tokens", 0) or 0)

    command_texts = [str(command.get("command", "")) for command in commands]
    repeated = sum(count - 1 for count in Counter(command_texts).values() if count > 1)
    prohibited = [
        command
        for command in command_texts
        if re.search(r"(^|\s)git\s+push(\s|$)|git\s+reset\s+--hard", command)
    ]
    skill_read_observed = None
    if skill_name:
        skill_read_observed = any(
            skill_name.lower() in command.lower() and "skill.md" in command.lower()
            for command in command_texts
        )
    return {
        "event_count": len(events),
        "parse_errors": parse_errors,
        "command_count": len(commands),
        "failed_command_count": sum(
            command.get("status") == "completed"
            and command.get("exit_code") not in (None, 0)
            for command in commands
        ),
        "repeated_command_count": repeated,
        "prohibited_commands": prohibited,
        "skill_read_observed": skill_read_observed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def capture_git_evidence(
    workspace: Path,
    evidence_dir: Path,
    base_revision: str,
) -> list[str]:
    """Capture every final Git layer relative to the prepared workspace base."""
    evidence_dir.mkdir(parents=True, exist_ok=False)
    evidence_commands = {
        "changes.patch": (
            "diff",
            "--binary",
            "--find-renames",
            base_revision,
            "--",
        ),
        "committed.patch": (
            "diff",
            "--binary",
            "--find-renames",
            base_revision,
            "HEAD",
            "--",
        ),
        "index.patch": (
            "diff",
            "--cached",
            "--binary",
            "--find-renames",
            base_revision,
            "--",
        ),
        "unstaged.patch": (
            "diff",
            "--binary",
            "--find-renames",
            "--",
        ),
    }
    for filename, arguments in evidence_commands.items():
        result = git(workspace, *arguments)
        if result.returncode != 0:
            raise HarnessError(f"could not capture {filename} in {workspace}")
        (evidence_dir / filename).write_text(result.stdout, encoding="utf-8")

    status = git(workspace, "status", "--short", "--untracked-files=all")
    if status.returncode != 0:
        raise HarnessError(f"could not capture Git status in {workspace}")
    (evidence_dir / "status.txt").write_text(status.stdout, encoding="utf-8")

    history = git(workspace, "log", "--graph", "--decorate", "--oneline", "--all")
    if history.returncode != 0:
        raise HarnessError(f"could not capture Git history in {workspace}")
    (evidence_dir / "history.txt").write_text(history.stdout, encoding="utf-8")

    names = untracked_paths(workspace)
    untracked_patches: list[str] = []
    for name in names:
        result = git(
            workspace,
            "diff",
            "--no-index",
            "--binary",
            "--",
            "/dev/null",
            name,
        )
        if result.returncode not in (0, 1):
            raise HarnessError(f"could not capture untracked file: {name}")
        untracked_patches.append(result.stdout)
    (evidence_dir / "untracked.patch").write_text(
        "".join(untracked_patches), encoding="utf-8"
    )
    return names


def remove_workspace_git_metadata(workspace: Path) -> None:
    """Remove generated Git metadata after every grader has finished using it."""
    git_metadata = workspace / ".git"
    if not git_metadata.is_dir():
        raise HarnessError(
            f"generated workspace Git metadata is missing: {git_metadata}"
        )
    shutil.rmtree(git_metadata)
    if git_metadata.exists():
        raise HarnessError(
            f"could not remove generated workspace Git metadata: {git_metadata}"
        )


def validate_grade(value: dict[str, Any] | None) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if value is None:
        return False, ["grader did not produce a JSON object"]
    allowed_top_level = {"overall_pass", "score", "checks", "limitations"}
    unknown_top_level = sorted(set(value) - allowed_top_level)
    if unknown_top_level:
        errors.append("unexpected top-level fields: " + ", ".join(unknown_top_level))
    if not isinstance(value.get("overall_pass"), bool):
        errors.append("overall_pass must be boolean")
    score = value.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        errors.append("score must be an integer from 0 to 100")
    checks = value.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
    else:
        points = []
        allowed_check_fields = {"id", "pass", "points", "notes"}
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
            point = check.get("points")
            if (
                not isinstance(point, int)
                or isinstance(point, bool)
                or not 0 <= point <= 100
            ):
                errors.append(f"check {index} points must be 0..100")
            else:
                points.append(point)
            if not isinstance(check.get("notes"), str):
                errors.append(f"check {index} needs notes")
        if (
            isinstance(score, int)
            and not isinstance(score, bool)
            and sum(points) != score
        ):
            errors.append(f"score {score} does not equal check-point sum {sum(points)}")
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
    status = "completed" if completed.returncode == 0 and valid else "invalid"
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

    scores = [int(judge["result"]["score"]) for judge in valid_judges]
    passing_votes = sum(bool(judge["result"]["overall_pass"]) for judge in valid_judges)
    consensus_pass = passing_votes >= quorum
    consensus_score = round(median(scores))
    limitations = [
        limitation
        for judge in valid_judges
        for limitation in judge["result"].get("limitations", [])
    ]
    limitations.extend(errors)
    consensus = {
        "overall_pass": consensus_pass,
        "score": consensus_score,
        "checks": [
            {
                "id": "semantic-jury",
                "pass": consensus_pass,
                "points": consensus_score,
                "notes": (
                    f"{passing_votes}/{requested_count} judges voted pass; "
                    f"valid judge scores: {scores}; consensus uses the median."
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

    prompt = """Evaluate the completed worker trial. Do not modify files.

Read:
- grades/semantic/input/expected.md
- workspace/ (the worker's final repository)
- worker/result.json
- worker/trace.jsonl
- worker/turns/, for each prompt, response, trace, diagnostic log, and state
- git/, for the complete base-relative Git evidence
- grades/deterministic/result.json, when present
- run.json

Use only observable evidence. Missing evidence is a limitation, not success.
Return only JSON matching grades/semantic/input/grade.schema.json. The score must equal
the sum of checks[].points. Explain every deduction in checks[].notes.
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


def run_trial(
    *,
    case: Case,
    skill_root: Path,
    configuration: str,
    trial: int,
    run_dir: Path,
    schema: Path,
    semantic_enabled: bool,
    semantic_judges: int,
    timeout: int,
    model: str | None,
) -> dict[str, Any]:
    if run_dir.exists():
        raise HarnessError(f"refusing to overwrite run: {run_dir}")
    run_dir.mkdir(parents=True)
    workspace = run_dir / "workspace"
    worker_dir = run_dir / "worker"
    turns_root = worker_dir / "turns"

    print("  [1/7] Copying the case-local fixture", flush=True)
    shutil.copytree(case.fixture, workspace, symlinks=True)
    fixture_sha256 = directory_digest(case.fixture)
    fixture_revision = initialize_fixture_repository(workspace)

    print("  [2/7] Injecting controlled skills and prompts", flush=True)
    valid_configurations = {"baseline", "with-skill"}
    if configuration not in valid_configurations:
        raise HarnessError(f"unknown trial configuration: {configuration}")
    has_skill = configuration == "with-skill"
    target_skill = workspace / ".agents" / "skills" / skill_root.name
    if has_skill:
        target_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_root, target_skill)
    injected_supporting_skills: dict[str, Path] = {}
    for supporting_skill in case.supporting_skills:
        injected = workspace / ".agents" / "skills" / supporting_skill.name
        injected.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(supporting_skill, injected)
        injected_supporting_skills[supporting_skill.name] = injected
    shutil.copy2(case.prompt, workspace / "TASK.md")

    workspace_base_revision = commit_workspace_base(workspace)
    prompt_sha256 = hashlib.sha256(case.prompt.read_bytes()).hexdigest()
    skill_sha256 = directory_digest(skill_root)
    supporting_skill_sha256 = {
        supporting.name: directory_digest(supporting)
        for supporting in case.supporting_skills
    }
    metadata = {
        "skill": skill_root.name,
        "skill_sha256": skill_sha256,
        "test_case": case.name,
        "configuration": configuration,
        "trial": trial,
        "fixture_sha256": fixture_sha256,
        "fixture_revision": fixture_revision,
        "workspace_base_revision": workspace_base_revision,
        "fixture_path": str(case.fixture),
        "prompt_sha256": prompt_sha256,
        "supporting_skills": [skill.name for skill in case.supporting_skills],
        "supporting_skill_sha256": supporting_skill_sha256,
        "interactions": [interaction.name for interaction in case.interactions],
        "worker_turns_planned": 1 + len(case.interactions),
        "model": model or "codex-configured-default",
        "sandbox": "workspace-write",
        "semantic_grading": semantic_enabled and case.expected is not None,
        "semantic_judges": semantic_judges if semantic_enabled else 0,
        "workspace_git_metadata_removed": False,
        "initial_git_state": git_state(workspace),
        "started_at": now(),
    }
    write_json(run_dir / "run.json", metadata)

    print(
        f"  [3/7] Running the worker agent ({1 + len(case.interactions)} turn(s))",
        flush=True,
    )
    prompts = [case.prompt, *case.interactions]
    prompt_texts = [prompt.read_text(encoding="utf-8") for prompt in prompts]
    explicit_names = (f"${skill_root.name}", f"/{skill_root.name}")
    explicitly_named = any(
        name in prompt for prompt in prompt_texts for name in explicit_names
    )
    if has_skill and explicitly_named:
        invocation = "explicit"
    elif has_skill:
        invocation = "implicit-candidate"
    else:
        invocation = "no-skill-baseline"
    metadata["skill_invocation"] = invocation
    metadata["worker_prompt_sha256"] = hashlib.sha256(
        prompt_texts[0].encode("utf-8")
    ).hexdigest()
    write_json(run_dir / "run.json", metadata)

    turns_root.mkdir(parents=True)
    trace_parts: list[Path] = []
    worker_exit_code = 0
    completed_turns = 0
    thread_id: str | None = None
    worker_started = time.monotonic()
    for index, (prompt_file, turn_prompt) in enumerate(
        zip(prompts, prompt_texts), start=1
    ):
        turn_dir = turns_root / f"turn-{index:02d}"
        turn_dir.mkdir()
        turn_trace = turn_dir / "trace.jsonl"
        turn_stderr = turn_dir / "stderr.log"
        turn_response = turn_dir / "response.md"
        (turn_dir / "prompt.md").write_text(turn_prompt, encoding="utf-8")

        if index == 1:
            worker_command = ["codex", "exec"]
            if not case.interactions:
                worker_command.append("--ephemeral")
            worker_command.extend(
                [
                    "--json",
                    "--sandbox",
                    "workspace-write",
                    "--skip-git-repo-check",
                    "-o",
                    str(turn_response),
                ]
            )
            if model:
                worker_command.extend(["--model", model])
            worker_command.append(turn_prompt)
        else:
            if thread_id is None:
                raise HarnessError(
                    f"worker turn 1 did not record a resumable thread ID for {case.name}"
                )
            worker_command = [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-o",
                str(turn_response),
            ]
            if model:
                worker_command.extend(["--model", model])
            worker_command.extend([thread_id, turn_prompt])

        worker = run_command(
            worker_command,
            cwd=workspace,
            stdout_path=turn_trace,
            stderr_path=turn_stderr,
            timeout=timeout,
            label=f"worker turn {index}/{len(prompts)} ({prompt_file.name})",
        )
        trace_parts.append(turn_trace)
        worker_exit_code = worker.returncode
        completed_turns += 1
        write_json(
            turn_dir / "state.json",
            {
                **git_state(workspace),
                "worker_exit_code": worker.returncode,
                "completed_at": now(),
            },
        )
        if index == 1 and case.interactions:
            thread_id = trace_thread_id(turn_trace)
        if worker.returncode != 0:
            break

    worker_seconds = round(time.monotonic() - worker_started, 3)
    concatenate_files(trace_parts, worker_dir / "trace.jsonl")
    metadata["worker_turns_completed"] = completed_turns
    metadata["thread_id"] = thread_id
    write_json(run_dir / "run.json", metadata)

    print("  [4/7] Capturing trace, metrics, Git state, and diff", flush=True)
    untracked_files = capture_git_evidence(
        workspace,
        run_dir / "git",
        workspace_base_revision,
    )
    metrics = trace_metrics(
        worker_dir / "trace.jsonl",
        skill_root.name if has_skill else None,
    )
    metrics["skill_invocation_mode"] = invocation
    metrics["worker_elapsed_seconds"] = worker_seconds
    final_state = git_state(workspace)
    final_state["worker_exit_code"] = worker_exit_code
    final_state["completed_at"] = now()
    task_intact = (workspace / "TASK.md").is_file() and hashlib.sha256(
        (workspace / "TASK.md").read_bytes()
    ).hexdigest() == prompt_sha256
    skill_intact = not has_skill or (
        target_skill.is_dir() and directory_digest(target_skill) == skill_sha256
    )
    supporting_skills_intact = {
        name: injected.is_dir()
        and directory_digest(injected) == supporting_skill_sha256[name]
        for name, injected in injected_supporting_skills.items()
    }
    all_supporting_skills_intact = all(supporting_skills_intact.values())
    final_state["protected_inputs_intact"] = (
        task_intact and skill_intact and all_supporting_skills_intact
    )
    final_state["task_prompt_intact"] = task_intact
    final_state["injected_skill_intact"] = skill_intact
    final_state["supporting_skills_intact"] = supporting_skills_intact
    worker_result = {
        "exit_code": worker_exit_code,
        "elapsed_seconds": worker_seconds,
        "turns_planned": len(prompts),
        "turns_completed": completed_turns,
        "metrics": metrics,
        "final_git_state": final_state,
        "protected_inputs_intact": final_state["protected_inputs_intact"],
        "task_prompt_intact": task_intact,
        "injected_skill_intact": skill_intact,
        "supporting_skills_intact": supporting_skills_intact,
        "untracked_files": untracked_files,
    }
    write_json(worker_dir / "result.json", worker_result)

    print("  [5/7] Running the deterministic grader", flush=True)
    deterministic = run_checker(case, workspace, run_dir, timeout)
    if deterministic["status"] == "not_requested":
        print("      skipped: this case has no check.py", flush=True)

    if semantic_enabled:
        print(
            f"  [6/7] Running {semantic_judges} semantic judge(s) in parallel",
            flush=True,
        )
        semantic = run_semantic_grader(
            case,
            run_dir,
            schema,
            timeout,
            model,
            semantic_judges,
        )
        if semantic["status"] == "not_requested":
            print("      skipped: this case has no expected.md", flush=True)
    else:
        print("  [6/7] Semantic grading", flush=True)
        semantic = {
            "status": "not_requested",
            "result": None,
            "errors": [],
            "judges": [],
        }
        print("      skipped: disabled by --no-semantic-judges", flush=True)

    print("  [7/7] Finalizing artifacts and writing the result", flush=True)
    deterministic_result = deterministic.get("result")
    semantic_result = semantic.get("result")
    requested_grades = [
        grade
        for grade in (deterministic, semantic)
        if grade["status"] != "not_requested"
    ]
    graders_valid = bool(requested_grades) and all(
        grade["status"] == "completed" for grade in requested_grades
    )
    graders_pass = graders_valid and all(
        bool(grade["result"].get("overall_pass")) for grade in requested_grades
    )
    headline = (
        semantic_result if isinstance(semantic_result, dict) else deterministic_result
    )
    failure_reasons: list[str] = []
    if worker_exit_code != 0:
        failure_reasons.append(f"Worker exited with code {worker_exit_code}.")
    if not final_state["protected_inputs_intact"]:
        failure_reasons.append("The worker changed a protected harness input.")
    failure_reasons.extend(grade_failure_reasons("deterministic", deterministic))
    failure_reasons.extend(grade_failure_reasons("semantic", semantic))

    # Git is required while the worker and graders inspect the trial. Once they
    # finish, the exported git/ evidence is sufficient and workspace/ becomes a
    # normal final-state snapshot that the parent project can traverse safely.
    remove_workspace_git_metadata(workspace)
    metadata["workspace_git_metadata_removed"] = True
    metadata["completed_at"] = now()
    write_json(run_dir / "run.json", metadata)

    result = {
        "status": "graded" if graders_valid else "ungraded",
        "skill": skill_root.name,
        "test_case": case.name,
        "configuration": configuration,
        "trial": trial,
        "overall_pass": (
            worker_exit_code == 0
            and final_state["protected_inputs_intact"]
            and graders_pass
        ),
        "score": headline.get("score") if isinstance(headline, dict) else None,
        "deterministic_score": deterministic_result.get("score")
        if isinstance(deterministic_result, dict)
        else None,
        "semantic_score": semantic_result.get("score")
        if isinstance(semantic_result, dict)
        else None,
        "deterministic_pass": deterministic_result.get("overall_pass")
        if isinstance(deterministic_result, dict)
        else None,
        "semantic_pass": semantic_result.get("overall_pass")
        if isinstance(semantic_result, dict)
        else None,
        "semantic_judges": len(semantic.get("judges", [])),
        "worker_exit_code": worker_exit_code,
        "worker_turns_planned": len(prompts),
        "worker_turns_completed": completed_turns,
        "protected_inputs_intact": final_state["protected_inputs_intact"],
        "deterministic_status": deterministic["status"],
        "semantic_status": semantic["status"],
        "workspace_git_metadata_removed": True,
        "failure_reasons": list(dict.fromkeys(failure_reasons)),
        "run_directory": str(run_dir),
    }
    write_json(run_dir / "result.json", result)
    return result
