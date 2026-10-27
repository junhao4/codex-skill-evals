"""Model-free relocation checks, including immutable historical evidence."""

import argparse
import contextlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib import paths
from _lib.compare import _run_field, _skill_diff, compare_experiments
from _lib.report import aggregate, write_report
from _lib.timing import worker_seconds
from _lib.usage import discover_project_inventory


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evals = self.root / "workflow" / "evals"
        self.suite = self.evals / "review"
        self.suite.mkdir(parents=True)
        self.patch = patch.object(paths, "EVALS_ROOT", self.evals)
        self.patch.start()
        self.addCleanup(self.patch.stop)


    def recorded_trial(self, experiment, marker=".agents/evals"):
        relative = f"review/runs/{experiment}/trials/case/with-skill/trial-01"
        directory = self.evals / relative
        (directory / "worker").mkdir(parents=True)
        snapshot = directory / "skill-snapshot" / "review"
        snapshot.mkdir(parents=True)
        (snapshot / "SKILL.md").write_text(f"# {experiment}\n")
        metadata = {
            "skill_snapshot": "skill-snapshot/review",
            "fixture_sha256": "fixture",
            "agent_profile_sha256": {"reviewer": experiment},
        }
        (directory / "run.json").write_text(json.dumps(metadata))
        (directory / "worker" / "metrics.json").write_text(
            '{"worker_elapsed_seconds": 12.5}'
        )
        result = {
            "test_case": "case",
            "configuration": "with-skill",
            "trial": 1,
            "score": 100,
            "deterministic_score": 100,
            "semantic_score": 100,
            "overall_pass": True,
            "status": "graded",
            "tokens": {},
            "resolved_model": "synthetic",
            "resolved_reasoning_effort": "low",
            "run_directory": f"/former-checkout/{marker}/{relative}",
        }
        return result, directory

    def test_project_discovery_and_cli_ignore_working_directory(self):
        actual = Path(__file__).resolve().parents[1]
        for layout in (".agents/evals", "workflow/evals"):
            with self.subTest(layout=layout):
                project = self.root / layout.split("/")[0].strip(".")
                installed = project / layout
                installed.mkdir(parents=True)
                shutil.copy2(actual / "eval.py", installed / "eval.py")
                shutil.copytree(actual / "_lib", installed / "_lib", ignore=shutil.ignore_patterns("__pycache__"))
                skill = project / ".agents/skills/example/SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text("# Example skill\n")
                case = installed / "example/test-cases/example"
                (case / "fixture").mkdir(parents=True)
                (case / "prompt.md").write_text("Run the example task")
                (case / "expected.md").write_text("Verify the example task")
                for command in (("list",), ("check", "example")):
                    result = subprocess.run(
                        [sys.executable, "-B", str(installed / "eval.py"), *command],
                        cwd=self.root, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("example", result.stdout)

    def test_legacy_and_current_recorded_paths_find_local_evidence(self):
        for index, marker in enumerate((".agents/evals", "workflow/evals"), 1):
            result, directory = self.recorded_trial(f"experiment-{index:03d}", marker)
            before = dict(result)
            self.assertEqual(paths.trial_directory(result), directory)
            self.assertEqual(_run_field(result, "fixture_sha256"), "fixture")
            self.assertEqual(worker_seconds(result), 12.5)
            self.assertEqual(result, before)
        for value in (None, "", 5):
            self.assertIsNone(paths.trial_directory({"run_directory": value}))
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        self.assertEqual(
            paths.trial_directory({"run_directory": str(ordinary)}), ordinary
        )

    def test_absent_evidence_and_parent_traversal_are_not_remapped(self):
        recorded = "/former/.agents/evals/review/runs/missing/trial-01"
        result = {"run_directory": recorded}
        self.assertEqual(paths.trial_directory(result), Path(recorded))
        self.assertIsNone(worker_seconds(result))
        traversal = "/former/.agents/evals/../../outside"
        self.assertEqual(
            paths.trial_directory({"run_directory": traversal}), Path(traversal)
        )

    def test_comparison_reads_relocated_snapshots_without_rewriting_history(self):
        old, old_dir = self.recorded_trial("experiment-001")
        new, new_dir = self.recorded_trial("experiment-002", "workflow/evals")
        for name, result, directory in (
            ("experiment-001", old, old_dir),
            ("experiment-002", new, new_dir),
        ):
            manifest = {
                "id": name,
                "test_cases": ["case"],
                "trials": 1,
                "results": [result],
                "aggregates": {
                    "cells": {
                        "case": {
                            "with-skill": {
                                "total_trials": 1,
                                "passed_trials": 1,
                                "mean_score": 100,
                                "mean_tokens": {},
                            }
                        }
                    }
                },
            }
            path = self.suite / "runs" / name / "experiment.json"
            path.write_text(json.dumps(manifest))
        originals = {
            path: path.read_bytes() for path in self.suite.rglob("*") if path.is_file()
        }
        self.assertTrue(_skill_diff(old, new))
        with contextlib.redirect_stdout(io.StringIO()):
            code = compare_experiments(
                argparse.Namespace(
                    skill="review",
                    old="experiment-001",
                    new="experiment-002",
                    config="with-skill",
                ),
                {"review": self.suite},
            )
        self.assertEqual(code, 0)
        report = (
            self.suite / "comparisons/experiment-001-vs-experiment-002.md"
        ).read_text()
        self.assertIn("agent profiles changed", report)
        self.assertIn("12.500s", report)
        self.assertIn("-# experiment-001", report)
        self.assertIn("+# experiment-002", report)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_report_links_use_relocated_trial(self):
        result, directory = self.recorded_trial("experiment-001")
        result["score"] = 95
        grade = directory / "grades/semantic/result.json"
        grade.parent.mkdir(parents=True)
        grade.write_text(
            json.dumps(
                {
                    "judges": [
                        {
                            "result": {
                                "score": 95,
                                "checks": [
                                    {
                                        "id": "coverage",
                                        "pass": False,
                                        "notes": "Retain the historical coverage caveat.",
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        )
        root = self.suite / "runs" / "experiment-001"
        experiment = {
            "id": "experiment-001",
            "skill": "review",
            "status": "completed",
            "overall_pass": True,
            "worker_trials": 1,
            "passed_trials": 1,
        }
        report = write_report(
            root, experiment, aggregate([result]), [result]
        ).read_text()
        self.assertIn(
            "[result.json](trials/case/with-skill/trial-01/result.json)", report
        )
        self.assertNotIn("/former-checkout/", report)
        self.assertIn("Retain the historical coverage caveat.", report)
