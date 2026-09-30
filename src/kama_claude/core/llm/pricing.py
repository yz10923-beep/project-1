"""List prices, first-party API, $ per million tokens (input, output).

Cache writes cost 1.25x input and cache reads 0.1x input. Cost is always derived from
the model that *served* a call and its recorded usage, never assumed per run.
"""

from __future__ import annotations

from collections.abc import Mapping

PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, usage: Mapping[str, int]) -> float | None:
    """None if the model has no known price (better than a silent $0)."""
    # Longest prefix first, so "claude-opus-5-5" is not priced as "claude-opus-5".
    match = next((m for m in sorted(PRICES, key=len, reverse=True) if model.startswith(m)), None)
    if match is None:
        return None
    pin, pout = PRICES[match]
    return (
        usage.get("input_tokens", 0) * pin
        + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
        + usage.get("cache_read_input_tokens", 0) * pin * 0.1
        + usage.get("output_tokens", 0) * pout
    ) / 1e6
