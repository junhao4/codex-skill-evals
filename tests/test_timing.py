"""Model-free tests: python3 -B -m pytest tests -q."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib.compare import _cell, compare_cell, render_comparison_markdown
from _lib.report import aggregate, write_report
from _lib.timing import format_seconds, time_delta, timing_summary, worker_seconds


def trial(seconds=None, **changes):
    result = {
        "test_case": "review", "configuration": "with-skill", "trial": 1,
        "status": "graded", "score": 100, "overall_pass": True,
        "run_directory": "/nonexistent-timing-test/trial-01",
        "worker_elapsed_seconds": seconds,
    }
    return {**result, **changes}


class TimingTests(unittest.TestCase):
    def test_missing_invalid_and_zero(self):
        for value in (None, True, False, -1, "10", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(worker_seconds(trial(value)))
                self.assertEqual(format_seconds(value), "N/A")
        self.assertEqual(worker_seconds(trial(0)), 0)
        self.assertEqual(format_seconds(0), "0.000s")
        self.assertEqual(format_seconds(59.9999), "1m 0.000s")
        self.assertEqual(format_seconds(3661.25), "1h 01m 1.250s")

    def test_mean_includes_failure_and_abstention(self):
        results = [trial(10), trial(20, score=0, overall_pass=False),
                   trial(30, status="needs_review", needs_review=True), trial()]
        cell = aggregate(results)["cells"]["review"]["with-skill"]
        self.assertEqual(cell["total_trials"], 4)
        self.assertEqual(cell["timed_trials"], 3)
        self.assertEqual(cell["mean_worker_seconds"], 20)
        self.assertEqual(cell["min_worker_seconds"], 10)
        self.assertEqual(cell["max_worker_seconds"], 30)
        self.assertEqual(timing_summary([trial()])["mean_worker_seconds"], None)

    def test_legacy_lookup_is_read_only_and_missing_stays_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker" / "metrics.json"
            path.parent.mkdir()
            content = '{"worker_elapsed_seconds": 42.5}\n'
            path.write_text(content)
            legacy = {"run_directory": folder}
            self.assertEqual(worker_seconds(legacy), 42.5)
            manifest = {"aggregates": {"cells": {"review": {"with-skill": {
                "total_trials": 1,
            }}}}, "results": [{**legacy, "test_case": "review", "configuration": "with-skill"}]}
            self.assertEqual(_cell(manifest, "review", "with-skill")["mean_worker_seconds"], 42.5)
            self.assertNotIn("timed_trials", manifest["aggregates"]["cells"]["review"]["with-skill"])
            self.assertEqual(path.read_text(), content)
            path.write_text("not valid JSON")
            self.assertIsNone(worker_seconds(legacy))
            path.unlink()
            self.assertIsNone(worker_seconds(legacy))

    def test_comparison_direction_and_zero_baseline(self):
        old = {"mean_worker_seconds": 100, "timed_trials": 3, "total_trials": 3}
        new = {"mean_worker_seconds": 80, "timed_trials": 2, "total_trials": 3}
        diff = compare_cell(old, new)
        self.assertEqual(diff["worker_time"]["verdict"], "improved")
        self.assertEqual(diff["timed_new"], 2)
        self.assertEqual(time_delta(100, 80), "-20.000s (-20.0%)")
        self.assertEqual(time_delta(0, 10), "+10.000s (N/A %)")
        self.assertEqual(time_delta(None, 10), "N/A")
        self.assertEqual(compare_cell({}, {})["worker_time"]["verdict"], "unknown")

    def test_report_and_comparison_rendering(self):
        results = [trial(90), trial(None, trial=2)]
        aggregates = aggregate(results)
        with tempfile.TemporaryDirectory() as folder:
            experiment = {
                "id": "test", "skill": "code-review", "status": "completed",
                "overall_pass": True, "worker_trials": 2, "passed_trials": 2,
            }
            text = write_report(Path(folder), experiment, aggregates, results).read_text()
        self.assertIn("## Worker runtime", text)
        self.assertIn("| 1/2 | 1m 30.000s |", text)
        self.assertIn("| Worker time | Evidence |", text)
        self.assertIn("| N/A | [result.json]", text)
        cell = aggregates["cells"]["review"]["with-skill"]
        diff = compare_cell(cell, cell)
        text = render_comparison_markdown(
            "code-review", "old", "new", {"both": ["review"], "only_old": [], "only_new": []},
            [{"test_case": "review", "diff": diff, "confounds": []}], [],
            {"improved": 0, "regressed": 0, "unchanged": 1}, {},
        )
        self.assertIn("Mean worker time (v1 -> v2)", text)
        self.assertIn("+0.000s (+0.0%)", text)
        self.assertIn("| timed trials / total | 1/2 | 1/2 |", text)


if __name__ == "__main__":
    unittest.main()
