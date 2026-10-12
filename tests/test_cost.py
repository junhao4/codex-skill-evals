"""Unit tests for the API-equivalent cost estimate (Layer 1c).

Run with:  python3 -m pytest tests/ -q  (from the repository root)

These tests pin the HONESTY contract: cost is only produced for a known,
priced model; an unknown or missing model yields None (never a guessed figure);
and every rendered surface carries the "not actual spend" caveat.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("evalmod", REPO_ROOT / "eval.py")
evalmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evalmod)


def _tokens(uncached=0, cached=0, cache_write=0, billed=0):
    return {
        "uncached_input_tokens": uncached,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "billed_output_tokens": billed,
    }


# --- estimate_api_cost ---


def test_cost_known_model_matches_rate_card():
    # 2M input context crosses 272k -> LONG-context gpt-5.6-luna rates
    # (0.40 in / 0.04 cached / 0.50 cache-write / 1.80 out per 1M).
    # 1M uncached + 1M cached + 1M cache-write + 100k billed-out
    # = 0.40 + 0.04 + 0.50 + 0.18 = 1.12
    cost = evalmod.estimate_api_cost(
        _tokens(uncached=1_000_000, cached=1_000_000,
                cache_write=1_000_000, billed=100_000),
        "gpt-5.6-luna",
    )
    assert cost == 1.12


def test_cost_prices_cache_writes():
    # cache-write alone (input_context 0 -> short tier),
    # gpt-6-astra short cache-write rate 12.50/1M.
    cost = evalmod.estimate_api_cost(_tokens(cache_write=1_000_000), "gpt-6-astra")
    assert cost == 12.50


def test_cost_cache_write_rate_absent_is_zero_not_error():
    # gpt-5.5 lists no cache-write rate; 1M input context -> LONG tier
    # (input 10.00). cache-write tokens contribute 0, no crash.
    cost = evalmod.estimate_api_cost(
        _tokens(uncached=1_000_000, cache_write=5_000_000), "gpt-5.5"
    )
    # only the 1M input at the long rate 10.00 counts -> 10.00
    assert cost == 10.00


def test_cost_unknown_model_is_none():
    # A made-up model is NOT in the table.
    assert evalmod.estimate_api_cost(_tokens(uncached=1000), "gpt-9-imaginary") is None


def test_cost_missing_model_is_none():
    assert evalmod.estimate_api_cost(_tokens(uncached=1000), None) is None
    assert evalmod.estimate_api_cost(_tokens(uncached=1000), "") is None


def test_cost_live_model_is_priced():
    # Regression guard: the live model gpt-5.6-luna MUST be in the table now.
    assert "gpt-5.6-luna" in evalmod.PRICING
    # 1M uncached crosses 272k -> long input rate 0.40 -> 0.40
    assert evalmod.estimate_api_cost(_tokens(uncached=1_000_000), "gpt-5.6-luna") == 0.40


def test_cost_zero_tokens_is_zero_for_known_model():
    assert evalmod.estimate_api_cost(_tokens(), "gpt-6-luna") == 0.0


def test_cost_handles_missing_token_keys():
    # No KeyError when the dict is sparse.
    assert evalmod.estimate_api_cost({}, "gpt-6-astra") == 0.0


def test_cost_rounds_to_six_places():
    cost = evalmod.estimate_api_cost(_tokens(uncached=1), "gpt-6-astra")
    # 1 * 10.00 / 1e6 = 0.00001 -> 0.00001
    assert cost == 0.00001


# --- format_cost ---


def test_format_cost_known():
    assert evalmod.format_cost(0.0034) == "$0.0034"


def test_format_cost_none_is_labelled_na():
    assert "n/a" in evalmod.format_cost(None)
    assert "price table" in evalmod.format_cost(None)


# --- pricing table provenance ---


def test_pricing_table_is_dated_and_sourced():
    assert isinstance(evalmod.PRICING_AS_OF, str) and evalmod.PRICING_AS_OF
    assert "API" in evalmod.PRICING_SOURCE
    # every priced model has at least one tier, each a 4-tuple
    # (input, cached, cache_write, output); cached/cache_write may be None.
    for tiers in evalmod.PRICING.values():
        assert isinstance(tiers, dict) and tiers
        for rates in tiers.values():
            assert len(rates) == 4


def test_threshold_is_positive_int():
    assert isinstance(evalmod.LONG_CONTEXT_THRESHOLD, int)
    assert evalmod.LONG_CONTEXT_THRESHOLD > 0


# --- context-tier selection ---


def test_small_context_uses_short_tier():
    # 10k input well under the 272k threshold -> short-context rate.
    # gpt-5.6-luna short input rate 0.20/1M: 10,000 * 0.20 / 1e6 = 0.002
    cost = evalmod.estimate_api_cost(_tokens(uncached=10_000), "gpt-5.6-luna")
    assert cost == 0.002


def test_large_context_uses_long_tier():
    # 300k input (uncached) crosses the threshold -> long rate (0.40/1M) on the
    # WHOLE request. 300,000 * 0.40 / 1e6 = 0.12
    cost = evalmod.estimate_api_cost(_tokens(uncached=300_000), "gpt-5.6-luna")
    assert cost == 0.12


def test_threshold_counts_uncached_plus_cached():
    # 200k uncached + 100k cached = 300k context -> long tier, even though
    # uncached alone is under the threshold. input 0.40, cached 0.04 (long).
    # 200,000*0.40 + 100,000*0.04 = 0.08 + 0.004 = 0.084
    cost = evalmod.estimate_api_cost(
        _tokens(uncached=200_000, cached=100_000), "gpt-5.6-luna"
    )
    assert cost == 0.084


def test_model_with_only_short_tier_falls_back():
    # gpt-5.4 has no long tier; a huge context still uses its short rate (2.50).
    # 300,000 * 2.50 / 1e6 = 0.75
    cost = evalmod.estimate_api_cost(_tokens(uncached=300_000), "gpt-5.4")
    assert cost == 0.75


def test_threshold_boundary_is_long_at_exactly_the_cutoff():
    # Lock the boundary direction: input_context == threshold uses `>=` -> LONG.
    at = evalmod.LONG_CONTEXT_THRESHOLD
    # gpt-5.6-luna long input 0.40; cost = at * 0.40 / 1e6
    cost_at = evalmod.estimate_api_cost(_tokens(uncached=at), "gpt-5.6-luna")
    assert cost_at == round(at * 0.40 / 1_000_000, 6)
    # one token below the cutoff uses the SHORT input rate 0.20
    cost_below = evalmod.estimate_api_cost(_tokens(uncached=at - 1), "gpt-5.6-luna")
    assert cost_below == round((at - 1) * 0.20 / 1_000_000, 6)


def test_compare_render_cost_na_when_models_differ():
    # Both models individually priced, but DIFFERENT -> must not price one card;
    # show n/a rather than a confounded figure.
    partition = {"both": ["case-a"], "only_old": [], "only_new": []}
    rows = [{
        "test_case": "case-a",
        "diff": evalmod.compare_cell(
            _cell(100, 1, 1000, 100), _cell(100, 1, 1000, 100)
        ),
        "confounds": [],
    }]
    summary = {"improved": 0, "regressed": 0, "unchanged": 1}
    provenance = {
        "old_model": "gpt-5.6-luna", "new_model": "gpt-6-luna",
        "old_effort": "low", "new_effort": "low",
        "trials": {"old": 1, "new": 1},
    }
    md = evalmod.render_comparison_markdown(
        "s", "e1", "e2", partition, rows, [], summary, provenance
    )
    assert "est. API-equiv. cost" in md
    # the cost row must be n/a (model mismatch), never a priced number
    cost_line = [ln for ln in md.splitlines() if "est. API-equiv. cost" in ln and "|" in ln]
    assert cost_line and "n/a" in cost_line[0]
    assert "$" not in cost_line[0]


# --- rendering: compare report cost row + caveat ---


def _cell(score, total, uncached, billed, model_tokens_extra=None):
    mt = {
        "uncached_input_tokens": uncached,
        "cached_input_tokens": 0,
        "output_tokens": billed,
        "reasoning_output_tokens": 0,
        "billed_output_tokens": billed,
    }
    return {"mean_score": score, "passed_trials": total, "total_trials": total,
            "mean_tokens": mt}


def test_compare_render_shows_cost_when_same_priced_model():
    partition = {"both": ["case-a"], "only_old": [], "only_new": []}
    rows = [{
        "test_case": "case-a",
        "diff": evalmod.compare_cell(
            _cell(100, 1, 1_000_000, 100_000),
            _cell(100, 1, 500_000, 100_000),
        ),
        "confounds": [],
    }]
    summary = {"improved": 0, "regressed": 0, "unchanged": 1}
    provenance = {
        "old_model": "gpt-5.6-luna", "new_model": "gpt-5.6-luna",
        "old_effort": "low", "new_effort": "low",
        "trials": {"old": 1, "new": 1},
    }
    md = evalmod.render_comparison_markdown(
        "s", "e1", "e2", partition, rows, [], summary, provenance
    )
    assert "est. API-equiv. cost" in md
    assert "estimate at public API list prices" in md
    assert "NOT actual" in md
    # 1M and 0.5M inputs both cross 272k -> LONG gpt-5.6-luna rates
    # (input 0.40/1M, output 1.80/1M).
    # old: 1M uncached * 0.40 + 100k billed * 1.80 = 0.40 + 0.18 = 0.58
    # new: 0.5M uncached * 0.40 + 100k billed * 1.80 = 0.20 + 0.18 = 0.38
    assert "$0.5800" in md
    assert "$0.3800" in md


def test_compare_render_cost_na_when_model_unpriced():
    partition = {"both": ["case-a"], "only_old": [], "only_new": []}
    rows = [{
        "test_case": "case-a",
        "diff": evalmod.compare_cell(
            _cell(100, 1, 1000, 100), _cell(100, 1, 1000, 100)
        ),
        "confounds": [],
    }]
    summary = {"improved": 0, "regressed": 0, "unchanged": 1}
    provenance = {
        "old_model": "gpt-9-imaginary", "new_model": "gpt-9-imaginary",
        "old_effort": "low", "new_effort": "low",
        "trials": {"old": 1, "new": 1},
    }
    md = evalmod.render_comparison_markdown(
        "s", "e1", "e2", partition, rows, [], summary, provenance
    )
    # row present but value n/a (unpriced model), never a guessed number
    assert "est. API-equiv. cost" in md
    assert "n/a" in md
