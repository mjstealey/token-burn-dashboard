"""Pricing table and cost calculation.

Rates live in pricing.yaml as USD per million tokens. The cost formula bills each
token class at its own rate — critically, cache tokens are NOT billed at the base
input rate (cache reads ~0.1x, cache writes 1.25x/2x), and on agentic coding logs
cache tokens dominate, so getting this right is the difference between a believable
burn number and one that's off by an order of magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

PER_TOKEN = 1_000_000.0


@dataclass(frozen=True)
class Rate:
    input: float = 0.0
    output: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    cache_read: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "Rate":
        return cls(
            input=float(d.get("input", 0.0)),
            output=float(d.get("output", 0.0)),
            cache_write_5m=float(d.get("cache_write_5m", 0.0)),
            cache_write_1h=float(d.get("cache_write_1h", 0.0)),
            cache_read=float(d.get("cache_read", 0.0)),
        )


@dataclass
class UsageBreakdown:
    """Token counts for one billable request, in the canonical (Anthropic-shaped) form.

    For OpenAI/Codex: input = uncached input, cache_read = cached input,
    cache_create_* = 0 (no separate write tier).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_5m: int = 0
    cache_create_1h: int = 0


class Pricing:
    def __init__(self, table: dict) -> None:
        self._table = table

    @classmethod
    def load(cls, path: str) -> "Pricing":
        data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
        return cls(data)

    def rate(self, provider: str, model: str | None) -> Rate:
        prov = self._table.get(provider) or {}
        models = prov.get("models") or {}
        default = prov.get("default") or {}
        if model:
            if model in models:
                return Rate.from_dict(models[model])
            # longest-prefix match (e.g. "gpt-5.4-mini-2026..." -> "gpt-5.4-mini")
            best_key, best_len = None, -1
            for key in models:
                if model.startswith(key) and len(key) > best_len:
                    best_key, best_len = key, len(key)
            if best_key is not None:
                return Rate.from_dict(models[best_key])
        return Rate.from_dict(default)

    def cost(self, provider: str, model: str | None, u: UsageBreakdown) -> float:
        r = self.rate(provider, model)
        total = (
            u.input_tokens * r.input
            + u.output_tokens * r.output
            + u.cache_read_tokens * r.cache_read
            + u.cache_create_5m * r.cache_write_5m
            + u.cache_create_1h * r.cache_write_1h
        )
        return total / PER_TOKEN
