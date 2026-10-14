"""API-equivalent cost estimation table and delta formatting helpers."""

from __future__ import annotations


# --- API-equivalent cost estimation (Layer 1c) --------------------------------
#
# IMPORTANT HONESTY NOTE. These rates are PUBLIC OpenAI *API* list prices. Many
# users (including ChatGPT Plus / Edu / Enterprise subscription or credit-pool
# accounts) are NOT billed per token at these rates at all. For those users this
# figure is a HYPOTHETICAL "what an API user would pay" number — useful only as a
# model-agnostic unit for comparing one run against another. It is NEVER the
# user's actual subscription or credit spend. Every surface that prints it MUST
# carry that caveat. This mirrors how Langfuse/Braintrust label cost as an
# *estimate* derived from a price table times token usage.
#
# Keep this table pinned and dated. Rates are USD per 1,000,000 tokens, from the
# STANDARD processing tier of platform.openai.com/pricing. Each model carries
# BOTH context tiers the card lists: "short" and "long". A request is billed at
# the long-context rate once its input context crosses LONG_CONTEXT_THRESHOLD
# tokens, and crossing re-prices the WHOLE request (a cliff, not a marginal
# rate) — per OpenAI's note that standard rates apply "for context lengths under
# 272K". (The card also lists Batch/Flex/Fast processing tiers, not encoded.)
# Update PRICING_AS_OF when you refresh the numbers.
PRICING_AS_OF = "2026-10-12"
PRICING_SOURCE = (
    "platform.openai.com/pricing (API list prices, Standard tier, "
    "short+long context)"
)
# Input context size (uncached + cached) at/above which the long-context rate
# applies to the whole request. OpenAI states standard rates apply under ~272K.
LONG_CONTEXT_THRESHOLD = 272_000
# model id -> {"short"/"long": (input, cached, cache_write, output)} per 1M.
# reasoning tokens bill as output, so billed_output uses the output rate.
# cache_write None means the card lists no separate cache-write rate. A model
# may omit a tier (e.g. its long-context column was not published/legible); the
# estimator falls back to the tier that exists.
PRICING: dict[str, dict[str, tuple[float, float | None, float | None, float]]] = {
    "gpt-6-astra": {
        "short": (10.00, 1.00, 12.50, 50.00),
        "long": (20.00, 2.00, 25.00, 75.00),
    },
    "gpt-6.1-sol": {
        "short": (2.00, 0.10, 2.50, 10.00),
        "long": (4.00, 0.20, 5.00, 15.00),
    },
    "gpt-6-luna": {
        "short": (0.10, 0.01, 0.125, 0.50),
        "long": (0.20, 0.02, 0.25, 0.75),
    },
    "gpt-6-sol": {
        "short": (2.00, 0.20, 2.50, 10.00),
        "long": (4.00, 0.40, 5.00, 15.00),
    },
    "gpt-5.6-sol": {
        "short": (4.00, 0.40, 5.00, 20.00),
        "long": (8.00, 0.80, 10.00, 30.00),
    },
    "gpt-5.6-terra": {
        "short": (2.00, 0.20, 2.50, 12.00),
        "long": (4.00, 0.40, 5.00, 18.00),
    },
    "gpt-5.6-luna": {
        "short": (0.20, 0.02, 0.25, 1.20),
        "long": (0.40, 0.04, 0.50, 1.80),
    },
    "gpt-5.5": {
        "short": (5.00, 0.50, None, 30.00),
        "long": (10.00, 1.00, None, 45.00),
    },
    "gpt-5.5-pro": {
        "short": (30.00, None, None, 180.00),
        "long": (60.00, None, None, 270.00),
    },
    # gpt-5.4 long-context column was not fully legible on the source; short only.
    "gpt-5.4": {
        "short": (2.50, 0.25, None, 15.00),
    },
}


def _select_tier(model_rates: dict, input_context: float) -> tuple:
    """Pick the (input, cached, cache_write, output) tuple for a request's input
    context size. Uses long-context rates at/above the threshold; falls back to
    whichever tier exists when a model omits one."""
    want = "long" if input_context >= LONG_CONTEXT_THRESHOLD else "short"
    if want in model_rates:
        return model_rates[want]
    # Fall back to the only tier published for this model.
    other = "short" if want == "long" else "long"
    return model_rates[other]


def estimate_api_cost(mean_tokens: dict, model: str | None) -> float | None:
    """Pure: estimate API-equivalent USD cost for one trial's mean token usage.

    Returns None when the model is unknown or not supplied (never guess). Picks
    the short- or long-context rate tier from the request's input context size
    (uncached + cached), then prices uncached-input at the input rate,
    cached-input at the cached rate, cache-write input at the cache-write rate
    (when the card lists one), and billed-output (output + reasoning) at the
    output rate.
    """
    if not model:
        return None
    model_rates = PRICING.get(model)
    if model_rates is None:
        return None
    uncached = mean_tokens.get("uncached_input_tokens", 0) or 0
    cached = mean_tokens.get("cached_input_tokens", 0) or 0
    cache_write = mean_tokens.get("cache_write_input_tokens", 0) or 0
    billed_out = mean_tokens.get("billed_output_tokens", 0) or 0
    input_context = uncached + cached
    input_rate, cached_rate, cache_write_rate, output_rate = _select_tier(
        model_rates, input_context
    )
    cost = (
        uncached * input_rate
        + cached * (cached_rate or 0)
        + cache_write * (cache_write_rate or 0)
        + billed_out * output_rate
    ) / 1_000_000
    return round(cost, 6)


def format_cost(cost: float | None) -> str:
    """Render a cost estimate, or a clear 'n/a' when the model is unpriced."""
    if cost is None:
        return "n/a (model not in price table)"
    return f"${cost:.4f}"


def format_delta(old, new) -> str:
    """Render a signed absolute + percent change, e.g. '-73 (-6.7%)'. Pure.

    Returns 'n/a' when either value is missing, and omits the percent when the
    old value is zero (percent change is undefined against a zero baseline).
    """
    if old is None or new is None:
        return "n/a"
    diff = new - old
    whole = float(diff).is_integer()
    abs_text = f"{int(diff):+d}" if whole else f"{diff:+.2f}"
    if old == 0:
        return abs_text if diff == 0 else f"{abs_text} (n/a%)"
    percent = diff / old * 100
    return f"{abs_text} ({percent:+.1f}%)"


def format_cost_delta(old_cost: float | None, new_cost: float | None) -> str:
    """Render a signed 4-decimal USD + percent change, e.g. '-$0.0009 (-14.2%)'. Pure."""
    if old_cost is None or new_cost is None:
        return "n/a"
    diff = new_cost - old_cost
    sign = "+" if diff >= 0 else "-"
    abs_text = f"{sign}${abs(diff):.4f}"
    if old_cost == 0:
        return abs_text if diff == 0 else f"{abs_text} (n/a%)"
    percent = diff / old_cost * 100
    return f"{abs_text} ({percent:+.1f}%)"

