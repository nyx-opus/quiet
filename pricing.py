"""
Model pricing for cost tracking.

Prices in USD per million tokens. Updated from Anthropic pricing page.
Cache reads are 90% cheaper than base input; cache writes are 25% more.
"""

PRICES = {
    "claude-opus-4-6": {
        "input": 15.00,
        "output": 75.00,
    },
    "claude-opus-4-5": {
        "input": 15.00,
        "output": 75.00,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "output": 15.00,
    },
    "claude-sonnet-4-0": {
        "input": 3.00,
        "output": 15.00,
    },
    "claude-3-opus-20240229": {
        "input": 15.00,
        "output": 75.00,
    },
    "claude-3-5-sonnet-20241022": {
        "input": 3.00,
        "output": 15.00,
    },
}

CACHE_READ_DISCOUNT = 0.10   # cache reads cost 10% of base input
CACHE_WRITE_PREMIUM = 1.25   # cache writes cost 125% of base input


def _resolve_model(model: str) -> dict:
    if model in PRICES:
        return PRICES[model]
    for prefix, prices in PRICES.items():
        if model.startswith(prefix):
            return prices
    return None


def cost_of(usage: dict, model: str) -> float | None:
    prices = _resolve_model(model)
    if not prices:
        return None

    input_cost = usage.get("input_tokens", 0) * prices["input"] / 1_000_000
    output_cost = usage.get("output_tokens", 0) * prices["output"] / 1_000_000

    cache_read = usage.get("cache_read", 0)
    cache_write = usage.get("cache_write", 0)

    if cache_read:
        input_cost -= cache_read * prices["input"] / 1_000_000
        input_cost += cache_read * prices["input"] * CACHE_READ_DISCOUNT / 1_000_000
    if cache_write:
        input_cost -= cache_write * prices["input"] / 1_000_000
        input_cost += cache_write * prices["input"] * CACHE_WRITE_PREMIUM / 1_000_000

    return input_cost + output_cost


def format_cost(usd: float) -> str:
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
