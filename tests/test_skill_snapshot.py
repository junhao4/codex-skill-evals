"""Tests for Layer 2a: immutable skill snapshot capture + read helper.

Run with:  python3 -m pytest tests/ -q  (from the repository root)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.harness import read_skill_snapshot_text  # noqa: E402


def _make_snapshot(run_dir: Path, name: str, body: str) -> dict:
    """Create a run dir with a skill snapshot and return a matching manifest."""
    snapshot = run_dir / "skill-snapshot" / name
    snapshot.mkdir(parents=True)
    (snapshot / "SKILL.md").write_text(body, encoding="utf-8")
    return {"skill_snapshot": f"skill-snapshot/{name}"}


def test_reads_snapshot_text(tmp_path):
    meta = _make_snapshot(tmp_path, "commit", "# Commit\nStage explicit paths.\n")
    text = read_skill_snapshot_text(tmp_path, meta)
    assert text == "# Commit\nStage explicit paths.\n"


def test_none_when_no_snapshot_recorded(tmp_path):
    # Baseline trial: skill_snapshot is null.
    assert read_skill_snapshot_text(tmp_path, {"skill_snapshot": None}) is None
    assert read_skill_snapshot_text(tmp_path, {}) is None


def test_none_when_snapshot_file_missing(tmp_path):
    # Manifest points at a snapshot dir that has no SKILL.md.
    (tmp_path / "skill-snapshot" / "commit").mkdir(parents=True)
    meta = {"skill_snapshot": "skill-snapshot/commit"}
    assert read_skill_snapshot_text(tmp_path, meta) is None


def test_snapshot_diff_is_recoverable(tmp_path):
    # Two runs with different skill wording -> their snapshots differ, so a
    # later compare can diff them. This is the whole point of Layer 2a.
    run_v1 = tmp_path / "experiment-001"
    run_v2 = tmp_path / "experiment-002"
    run_v1.mkdir()
    run_v2.mkdir()
    meta_v1 = _make_snapshot(run_v1, "commit", "Ask before committing.\n")
    meta_v2 = _make_snapshot(run_v2, "commit", "Commit without asking.\n")
    v1 = read_skill_snapshot_text(run_v1, meta_v1)
    v2 = read_skill_snapshot_text(run_v2, meta_v2)
    assert v1 != v2
    assert "Ask before" in v1
    assert "without asking" in v2
