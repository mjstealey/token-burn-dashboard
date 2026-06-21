"""Runtime configuration. Resolution order: env (TD_*) > YAML config > defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytz
import yaml


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return None


def _int_or_none(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def validate_timezone(name: str) -> str:
    try:
        pytz.timezone(name)
    except pytz.UnknownTimeZoneError as exc:
        raise ValueError(
            f"Unknown timezone {name!r}. Use an IANA timezone like "
            "'America/New_York'."
        ) from exc
    return name


@dataclass
class Config:
    db_path: str = "./data/token.duckdb"
    timezone: str = "America/New_York"
    pricing_path: str = "./pricing.yaml"
    ingest_interval_min: int = 15
    port: int = 8080
    # provider -> filesystem root to scan
    scan_roots: dict[str, str] = field(
        default_factory=lambda: {
            "claude": "~/.claude/projects",
            "codex": "~/.codex/sessions",
        }
    )
    limit_block_5h_tokens: int | None = None
    limit_week_tokens: int | None = None

    @property
    def block_window_hours(self) -> int:
        return 5

    def root_for(self, provider: str) -> Path | None:
        raw = self.scan_roots.get(provider)
        return Path(_expand(raw)) if raw else None


def load_config() -> Config:
    cfg = Config()

    # Layer 1: optional YAML file.
    cfg_path = _env("TD_CONFIG")
    if cfg_path and Path(_expand(cfg_path)).exists():
        data = yaml.safe_load(Path(_expand(cfg_path)).read_text()) or {}
        cfg.db_path = data.get("db_path", cfg.db_path)
        cfg.timezone = data.get("timezone", cfg.timezone)
        cfg.pricing_path = data.get("pricing_path", cfg.pricing_path)
        cfg.ingest_interval_min = int(
            data.get("ingest_interval_min", cfg.ingest_interval_min)
        )
        cfg.port = int(data.get("port", cfg.port))
        if isinstance(data.get("scan_roots"), dict):
            cfg.scan_roots = {k: str(v) for k, v in data["scan_roots"].items()}
        limits = data.get("limits") or {}
        cfg.limit_block_5h_tokens = limits.get("block_5h_tokens")
        cfg.limit_week_tokens = limits.get("week_tokens")

    # Layer 2: environment overrides.
    cfg.db_path = _env("TD_DB_PATH") or cfg.db_path
    cfg.timezone = _env("TZ", "TD_TZ") or cfg.timezone
    cfg.pricing_path = _env("TD_PRICING_PATH") or cfg.pricing_path
    cfg.ingest_interval_min = (
        _int_or_none(_env("TD_INGEST_INTERVAL_MIN")) or cfg.ingest_interval_min
    )
    cfg.port = _int_or_none(_env("TD_PORT")) or cfg.port

    claude_root = _env("TD_CLAUDE_ROOT")
    codex_root = _env("TD_CODEX_ROOT")
    if claude_root:
        cfg.scan_roots["claude"] = claude_root
    if codex_root:
        cfg.scan_roots["codex"] = codex_root

    if (v := _int_or_none(_env("TD_LIMIT_5H_TOKENS"))) is not None:
        cfg.limit_block_5h_tokens = v
    if (v := _int_or_none(_env("TD_LIMIT_WEEK_TOKENS"))) is not None:
        cfg.limit_week_tokens = v

    cfg.timezone = validate_timezone(cfg.timezone)
    return cfg
