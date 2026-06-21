from pathlib import Path

from token_dashboard.config import Config
from token_dashboard.main import build_state

from conftest import write_jsonl


def _pricing_yaml(input_rate: float) -> str:
    return f"""
anthropic:
  default:
    input: {input_rate}
    output: 0
    cache_write_5m: 0
    cache_write_1h: 0
    cache_read: 0
  models:
    claude-opus-4-8:
      input: {input_rate}
      output: 0
      cache_write_5m: 0
      cache_write_1h: 0
      cache_read: 0
"""


def _assistant() -> dict:
    return {
        "type": "assistant",
        "uuid": "u1",
        "requestId": "rq1",
        "sessionId": "s1",
        "timestamp": "2026-06-18T12:00:00Z",
        "message": {
            "model": "claude-opus-4-8",
            "id": "m1",
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def _cost(state) -> float:
    return state.db.query("SELECT cost_usd FROM usage_events")[0][0]


def test_ingest_reprices_only_when_pricing_hash_changes(tmp_path: Path):
    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(_pricing_yaml(1.0))

    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    write_jsonl(claude_root / "session.jsonl", [_assistant()])

    cfg = Config(
        db_path=":memory:",
        pricing_path=str(pricing_path),
        scan_roots={"claude": str(claude_root), "codex": str(tmp_path / "nope")},
        ingest_interval_min=999,
    )
    state = build_state(cfg)
    try:
        assert state.ingest()["claude"]["events_inserted"] == 1
        assert _cost(state) == 1.0
        assert state.last_pricing_sync["changed"] is True
        assert state.last_pricing_sync["repriced"] == 0

        pricing_path.write_text(_pricing_yaml(2.5))
        assert state.ingest()["claude"]["events_inserted"] == 0
        assert _cost(state) == 2.5
        assert state.last_pricing_sync["changed"] is True
        assert state.last_pricing_sync["repriced"] == 1

        state.ingest()
        assert state.last_pricing_sync["changed"] is False
        assert state.last_pricing_sync["repriced"] == 0
    finally:
        state.db.close()
