import datetime
import os
import re
from dataclasses import dataclass, fields

# USD per 1M tokens, OpenAI list prices. Override with env vars like PRICE_GPT_REALTIME_MINI_AUDIO_OUT=20.
DEFAULT_PRICES = {
    "gpt-realtime-mini": {
        "audio_in": 10.0, "audio_in_cached": 0.30, "audio_out": 20.0,
        "text_in": 0.60, "text_in_cached": 0.06, "text_out": 2.40,
    },
    "gpt-realtime": {
        "audio_in": 32.0, "audio_in_cached": 0.40, "audio_out": 64.0,
        "text_in": 4.0, "text_in_cached": 0.40, "text_out": 16.0,
    },
}


def prices_for(model: str) -> dict[str, float] | None:
    base = DEFAULT_PRICES.get(model)
    if base is None:
        return None
    prefix = "PRICE_" + re.sub(r"[^A-Z0-9]", "_", model.upper()) + "_"
    return {k: float(os.environ.get(prefix + k.upper(), v)) for k, v in base.items()}


@dataclass
class TokenTotals:
    audio_in: int = 0
    audio_in_cached: int = 0
    audio_out: int = 0
    text_in: int = 0
    text_in_cached: int = 0
    text_out: int = 0

    def add_usage(self, usage) -> None:
        """Add a Voice Live TokenUsage. Cached tokens are counted apart from uncached ones."""
        inp, out = usage.input_token_details, usage.output_token_details
        cached = getattr(inp, "cached_tokens_details", None)
        cached_audio = (cached.audio_tokens or 0) if cached else 0
        cached_text = (cached.text_tokens or 0) if cached else 0
        self.audio_in += (inp.audio_tokens or 0) - cached_audio
        self.audio_in_cached += cached_audio
        self.text_in += (inp.text_tokens or 0) - cached_text
        self.text_in_cached += cached_text
        self.audio_out += out.audio_tokens or 0
        self.text_out += out.text_tokens or 0

    def cost(self, prices: dict[str, float] | None) -> float:
        if not prices:
            return 0.0
        return sum(getattr(self, f.name) * prices[f.name] for f in fields(self)) / 1_000_000

    def describe(self) -> str:
        return (
            f"audio in {self.audio_in + self.audio_in_cached} (cached {self.audio_in_cached}), "
            f"audio out {self.audio_out}, text in {self.text_in + self.text_in_cached} "
            f"(cached {self.text_in_cached}), text out {self.text_out}"
        )


class DailyUsage:
    """Estimated cost for the current day, kept in memory (resets when the service restarts)."""

    def __init__(self) -> None:
        self.day = datetime.date.today()
        self.cost = 0.0

    def add(self, cost: float) -> float:
        today = datetime.date.today()
        if today != self.day:
            self.day, self.cost = today, 0.0
        self.cost += cost
        return self.cost
