"""Cost of one LLM call when the provider does not report it (round 7, F6).

OpenRouter returns `usage.cost`; other providers return only token counts. Then the
cost is tokens times a price per million tokens:

- `ZR_PRICE_IN` and `ZR_PRICE_OUT` (USD per million prompt / completion tokens) apply to
  every model when set;
- else the built-in table below (DeepSeek through OpenRouter). The values are upper
  estimates from the pilot runs (pilot 2: 160 calls, `usage.cost` against tokens), so a
  budget stops early rather than late. Set the env values to the provider's real prices.

`price_known(model)` tells the harness whether a run can keep a budget at all; a run
without a known cost source starts only with ZR_ALLOW_UNKNOWN_COST=1.
"""
from __future__ import annotations

import os
from typing import Any

# USD per million tokens: (prompt, completion).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # Round 9. NOT queried from OpenRouter (the harness work runs without network): the
    # values come from the bake-off (2026-10-04). sonnet-5.5: the listed $2 / $10, and the
    # measured cost matched. flash and pro: OpenRouter routes them to several providers;
    # the bake-off measured up to $1.66 per million completion tokens for flash and 1.8 x
    # the listed pro price, so these are the measured upper values, not the list prices
    # (flash $0.028 / $0.056, pro $0.21 / $0.42). The provider's usage.cost always wins.
    "claude-sonnet-5.5": (2.00, 10.00),
    "deepseek-v4-flash": (0.30, 1.70),
    "deepseek-v4-pro": (0.40, 0.80),
}


def _key(model: str) -> str:
    return (model or "").split("/")[-1].split(":")[0].strip().lower()


def price(model: str) -> tuple[float, float] | None:
    pin, pout = os.environ.get("ZR_PRICE_IN"), os.environ.get("ZR_PRICE_OUT")
    if pin and pout:
        return float(pin), float(pout)
    return DEFAULT_PRICES.get(_key(model))


def provider_reports_cost(base_url: str) -> bool:
    return "openrouter.ai" in (base_url or "")


def price_known(model: str, base_url: str) -> bool:
    return provider_reports_cost(base_url) or price(model) is not None


def cost_from_tokens(model: str, usage: dict[str, Any]) -> float | None:
    p = price(model)
    if p is None:
        return None
    pt = float(usage.get("prompt_tokens") or 0)
    ct = float(usage.get("completion_tokens") or 0)
    return round(pt / 1e6 * p[0] + ct / 1e6 * p[1], 6)


def call_cost(model: str, usage: Any) -> tuple[float, str]:
    """(cost, source): source is `provider`, `price-table` or `unknown`."""
    if not isinstance(usage, dict):
        return 0.0, "unknown"
    if usage.get("cost") is not None:
        try:
            return float(usage["cost"]), "provider"
        except (TypeError, ValueError):
            pass
    c = cost_from_tokens(model, usage)
    return (c, "price-table") if c is not None else (0.0, "unknown")
