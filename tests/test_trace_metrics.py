"""Tests for trace_metrics token accounting.

Run with:  python3 -m pytest tests/ -q
(from the repository root)
"""

import json
import sys
from pathlib import Path

# Make `_lib` importable when tests run from the repository root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _lib.harness import trace_metrics  # noqa: E402


def _write_trace(tmp_path: Path, events: list[dict]) -> Path:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return trace


def _turn_completed(usage: dict) -> dict:
    return {"type": "turn.completed", "usage": usage}


def test_captures_all_five_usage_fields(tmp_path):
    """trace_metrics must capture all five real usage fields, not just two."""
    trace = _write_trace(
        tmp_path,
        [
            _turn_completed(
                {
                    "input_tokens": 360906,
                    "cached_input_tokens": 321792,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 6770,
                    "reasoning_output_tokens": 1079,
                }
            )
        ],
    )

    metrics = trace_metrics(trace)

    assert metrics["input_tokens"] == 360906
    assert metrics["cached_input_tokens"] == 321792
    assert metrics["cache_write_input_tokens"] == 0
    assert metrics["output_tokens"] == 6770
    assert metrics["reasoning_output_tokens"] == 1079


def test_derived_uncached_and_billed_output(tmp_path):
    """Derived fields: uncached input = input - cached; billed output = output + reasoning."""
    trace = _write_trace(
        tmp_path,
        [
            _turn_completed(
                {
                    "input_tokens": 360906,
                    "cached_input_tokens": 321792,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 6770,
                    "reasoning_output_tokens": 1079,
                }
            )
        ],
    )

    metrics = trace_metrics(trace)

    assert metrics["uncached_input_tokens"] == 360906 - 321792
    assert metrics["billed_output_tokens"] == 6770 + 1079


def test_sums_across_multiple_turns(tmp_path):
    """Usage is summed across every turn.completed event."""
    trace = _write_trace(
        tmp_path,
        [
            _turn_completed(
                {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "cache_write_input_tokens": 10,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                }
            ),
            _turn_completed(
                {
                    "input_tokens": 200,
                    "cached_input_tokens": 60,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 15,
                }
            ),
        ],
    )

    metrics = trace_metrics(trace)

    assert metrics["input_tokens"] == 300
    assert metrics["cached_input_tokens"] == 100
    assert metrics["cache_write_input_tokens"] == 10
    assert metrics["output_tokens"] == 50
    assert metrics["reasoning_output_tokens"] == 20
    assert metrics["uncached_input_tokens"] == 200
    assert metrics["billed_output_tokens"] == 70


def test_missing_fields_default_to_zero(tmp_path):
    """A turn missing the newer fields must not crash; missing fields read as 0."""
    trace = _write_trace(
        tmp_path,
        [_turn_completed({"input_tokens": 50, "output_tokens": 10})],
    )

    metrics = trace_metrics(trace)

    assert metrics["input_tokens"] == 50
    assert metrics["output_tokens"] == 10
    assert metrics["cached_input_tokens"] == 0
    assert metrics["cache_write_input_tokens"] == 0
    assert metrics["reasoning_output_tokens"] == 0
    assert metrics["uncached_input_tokens"] == 50
    assert metrics["billed_output_tokens"] == 10
