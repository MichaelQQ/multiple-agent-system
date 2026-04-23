"""Pricing table for MAS providers. To add a new provider or model, add an entry to _PRICING with rates in USD per million tokens (input, output)."""
from __future__ import annotations

# (input_per_mtok, output_per_mtok) in USD
_PRICING: dict[str, dict[str, tuple[float, float]]] = {
    "claude-code": {
        "claude-opus-4-7": (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (0.80, 4.0),
        "claude-haiku-4-5-20251001": (0.80, 4.0),
    },
    "gemini-cli": {
        "gemini-2.5-pro": (1.25, 10.0),
        "gemini-2.5-flash": (0.15, 0.60),
    },
    "opencode": {
        "claude-opus-4-7": (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (0.80, 4.0),
    },
    "codex": {
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.60),
    },
    "ollama": {},
    "script": {},
}


def compute_cost_usd(
    provider: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
) -> float:
    """Return USD cost for provider/model/token usage; 0.0 for unknown or missing values."""
    if tokens_in is None or tokens_out is None:
        return 0.0
    models = _PRICING.get(provider, {})
    pricing = models.get(model)
    if pricing is None:
        return 0.0
    input_per_mtok, output_per_mtok = pricing
    return (tokens_in * input_per_mtok + tokens_out * output_per_mtok) / 1_000_000
