"""Git repository initialization, state inspection, and workspace evidence capture."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from _lib.common import HarnessError


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


def git_state(workspace: Path) -> dict[str, Any]:
    revision = git(workspace, "rev-parse", "HEAD")
    status = git(workspace, "status", "--short")
    return {
        "revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "status": status.stdout.splitlines() if status.returncode == 0 else [],
        "clean": status.returncode == 0 and not status.stdout.strip(),
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

