"""Explicit, protected project-agent dependencies for disposable eval workspaces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil

from _lib.common import HarnessError


def discover_profiles(case_root: Path, project_root: Path) -> tuple[Path, ...]:
    manifest = case_root / "agent-profiles.json"
    if not manifest.exists():
        return ()
    try:
        names = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HarnessError(f"invalid agent-profiles.json in {case_root.name}: {error}") from error
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
        for name in names
    ):
        raise HarnessError("agent-profiles.json must list agent names, not paths")
    if len(set(names)) != len(names):
        raise HarnessError("agent-profiles.json contains duplicate names")
    profiles = tuple(project_root / ".codex" / "agents" / f"{name}.toml" for name in names)
    for profile in profiles:
        if not profile.is_file() or profile.is_symlink():
            raise HarnessError(f"missing or symlinked agent profile: {profile}")
    return profiles


def profile_hashes(profiles: tuple[Path, ...]) -> dict[str, str | None]:
    return {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in profiles}


def inject_profiles(profiles: tuple[Path, ...], workspace: Path, run_dir: Path) -> tuple[Path, ...]:
    if not profiles:
        return ()
    target = workspace / ".codex" / "agents"
    snapshot = run_dir / "agent-profile-snapshot"
    target.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir()
    for profile in profiles:
        shutil.copy2(profile, target / profile.name)
        shutil.copy2(profile, snapshot / profile.name)
    return tuple(target / p.name for p in profiles)


def profile_overrides(profiles: tuple[Path, ...]) -> list[str]:
    # Explicit registration also supports CLIs predating standalone discovery.
    overrides = []
    for profile in profiles:
        for key, value in (("description", f"Configured evaluation agent: {profile.stem}"),
                           ("config_file", str(profile.resolve()))):
            overrides.extend(["-c", f"agents.{profile.stem}.{key}={json.dumps(value)}"])
    return overrides
