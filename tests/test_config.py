import pytest

from token_dashboard.config import load_config, validate_timezone


def test_validate_timezone_accepts_iana_name():
    assert validate_timezone("America/New_York") == "America/New_York"


def test_load_config_rejects_unknown_timezone(monkeypatch):
    monkeypatch.delenv("TD_CONFIG", raising=False)
    monkeypatch.setenv("TZ", "Not/AZone")

    with pytest.raises(ValueError, match="Unknown timezone"):
        load_config()


def test_load_config_preserves_numeric_env_overrides(monkeypatch):
    monkeypatch.delenv("TD_CONFIG", raising=False)
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("TD_PORT", "9090")
    monkeypatch.setenv("TD_INGEST_INTERVAL_MIN", "7")
    monkeypatch.setenv("TD_LIMIT_5H_TOKENS", "123")
    monkeypatch.setenv("TD_LIMIT_WEEK_TOKENS", "456")

    cfg = load_config()

    assert cfg.port == 9090
    assert cfg.ingest_interval_min == 7
    assert cfg.limit_block_5h_tokens == 123
    assert cfg.limit_week_tokens == 456
