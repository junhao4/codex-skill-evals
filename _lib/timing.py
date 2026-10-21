"""Worker wall-clock timing, including read-only legacy evidence lookup."""

from __future__ import annotations

import math
from _lib.common import read_json
from _lib.paths import trial_directory


def valid_seconds(value) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def worker_seconds(result: dict) -> float | None:
    value = result.get("worker_elapsed_seconds")
    if "worker_elapsed_seconds" not in result and result.get("run_directory"):
        # Older manifests omitted timing; never rewrite the original evidence.
        run_dir = trial_directory(result)
        metrics = (read_json(run_dir / "worker" / "metrics.json") if run_dir else None) or {}
        value = metrics.get("worker_elapsed_seconds")
    return float(value) if valid_seconds(value) else None


def timing_summary(results: list[dict]) -> dict:
    values = [value for result in results if (value := worker_seconds(result)) is not None]
    return {
        "timed_trials": len(values),
        "mean_worker_seconds": round(sum(values) / len(values), 3) if values else None,
        "min_worker_seconds": min(values) if values else None,
        "max_worker_seconds": max(values) if values else None,
    }


def format_seconds(value) -> str:
    if not valid_seconds(value):
        return "N/A"
    minutes, milliseconds = divmod(round(value * 1000), 60_000)
    hours, minutes = divmod(minutes, 60)
    seconds = f"{milliseconds / 1000:.3f}s"
    if hours:
        return f"{hours}h {minutes:02d}m {seconds}"
    return f"{minutes}m {seconds}" if minutes else seconds


def time_delta(old, new) -> str:
    if not valid_seconds(old) or not valid_seconds(new):
        return "N/A"
    change = f"{new - old:+.3f}s"
    return f"{change} ({(new - old) / old * 100:+.1f}%)" if old else f"{change} (N/A %)"
