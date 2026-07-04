"""Pricing table and cost calculation.

Rates live in pricing.yaml as USD per million tokens. The cost formula bills each
token class at its own rate — critically, cache tokens are NOT billed at the base
input rate (cache reads ~0.1x, cache writes 1.25x/2x), and on agentic coding logs
cache tokens dominate, so getting this right is the difference between a believable
burn number and one that's off by an order of magnitude.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

PER_TOKEN = 1_000_000.0


def pricing_key(provider: str, model: str | None) -> str:
    """Map a usage event's provider/model to a pricing.yaml top-level key.

    Claude Code can route to local models (zero-rate "local" table), but Bedrock/
    Vertex model ids carry prefixes ("us.anthropic.claude-...") — match "claude"
    anywhere in the id, not just at the start, or those bill as $0 silently.
    """
    if provider == "claude":
        return "anthropic" if "claude" in (model or "") else "local"
    return provider


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
    def __init__(self, table: dict, source_hash: str | None = None) -> None:
        self._table = table
        self.source_hash = source_hash

    @classmethod
    def load(cls, path: str) -> "Pricing":
        raw = Path(path).expanduser().read_bytes()
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        return cls(data, hashlib.sha256(raw).hexdigest())

    def is_unpriced(self, provider: str, model: str | None) -> bool:
        """True when the resolved rate is all-zero for a key that should have rates
        (i.e. not the intentionally-free "local" table) — cost silently reads $0."""
        key = pricing_key(provider, model)
        if key == "local":
            return False
        r = self.rate(key, model)
        return not any(
            (r.input, r.output, r.cache_read, r.cache_write_5m, r.cache_write_1h)
        )

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
