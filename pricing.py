"""
Model pricing for cost tracking.

Prices in USD per million tokens. Updated 2026-06-03 from Anthropic pricing page.
Cache pricing uses the 5-minute TTL tier (default for API).
"""

PRICES = {
    "claude-opus-4-8": {
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-7": {
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-6": {
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-5": {
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-0": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-1": {
        "input": 18.75,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-3-opus-20240229": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-0": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-3-5-sonnet-20241022": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.00,
        "cache_write": 1.00,
        "cache_read": 0.08,
    },
    "claude-3-5-haiku-20241022": {
        "input": 0.80,
        "output": 4.00,
        "cache_write": 1.00,
        "cache_read": 0.08,
    },
}


def _model_family(model: str) -> str:
    """Extract model family: 'claude-sonnet-4-20250514' -> 'claude-sonnet-4'."""
    import re
    return re.sub(r'-\d{8}$', '', model)


def _resolve_model(model: str) -> dict:
    if model in PRICES:
        return PRICES[model]
    for key, prices in PRICES.items():
        if model.startswith(key) or key.startswith(model):
            return prices
    family = _model_family(model)
    if family != model:
        if family in PRICES:
            return PRICES[family]
        for key, prices in PRICES.items():
            if key.startswith(family) or family.startswith(key):
                return prices
    return None


def cost_of(usage: dict, model: str) -> float | None:
    prices = _resolve_model(model)
    if not prices:
        return None

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read", 0)
    cache_write = usage.get("cache_write", 0)

    # Non-cached input tokens = total input minus cache hits and cache writes
    plain_input = max(0, input_tokens - cache_read - cache_write)

    cost = (plain_input * prices["input"] / 1_000_000
            + cache_read * prices["cache_read"] / 1_000_000
            + cache_write * prices["cache_write"] / 1_000_000
            + output_tokens * prices["output"] / 1_000_000)

    return cost


def format_cost(usd: float) -> str:
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
