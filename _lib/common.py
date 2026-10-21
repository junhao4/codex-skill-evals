"""Shared constants, data types, JSON/file I/O, and subprocess progress helpers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
    agent_profiles: tuple[Path, ...] = ()


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


def markdown_cell(value: object) -> str:
    """Make a value safe for a compact Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)

