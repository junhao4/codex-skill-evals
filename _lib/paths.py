"""Project locations and read-only lookup of relocated trial evidence."""

from __future__ import annotations

from pathlib import Path


EVALS_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EVALS_ROOT.parents[1]
SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"


def trial_directory(result: dict) -> Path | None:
    """Prefer this checkout's evidence without editing recorded execution paths."""
    recorded = result.get("run_directory")
    if not isinstance(recorded, str) or not recorded:
        return None
    path = Path(recorded)
    parts = path.parts
    for index in range(len(parts) - 1):
        if parts[index : index + 2] in ((".agents", "evals"), ("workflow", "evals")):
            suffix = parts[index + 2 :]
            if ".." not in suffix:
                local = EVALS_ROOT.joinpath(*suffix)
                if local.is_dir():
                    return local
    return path
