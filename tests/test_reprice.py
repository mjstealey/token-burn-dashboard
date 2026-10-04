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


def test_reprice_waits_for_inflight_ingest(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from token_dashboard.ingest.claude import ClaudeAdapter

    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(_pricing_yaml(1.0))
    write_jsonl(tmp_path / "session.jsonl", [_assistant()])
    state = build_state(
        Config(
            db_path=":memory:",
            pricing_path=str(pricing_path),
            scan_roots={"claude": str(tmp_path)},
        )
    )
    parsing = threading.Event()
    resume = threading.Event()
    reprice_started = threading.Event()
    reprice_done = threading.Event()

    class PausedAdapter(ClaudeAdapter):
        def parse(self, path, offset):
            parsing.set()
            assert resume.wait(5)
            return super().parse(path, offset)

    def reprice():
        reprice_started.set()
        result = state.sync_pricing()
        reprice_done.set()
        return result

    state.adapters = [PausedAdapter()]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ingest = pool.submit(state.ingest)
            try:
                assert parsing.wait(5)
                pricing_path.write_text(_pricing_yaml(2.0))
                update = pool.submit(reprice)
                assert reprice_started.wait(5)
                assert not reprice_done.wait(0.1)
            finally:
                resume.set()
            ingest.result(timeout=5)
            update.result(timeout=5)
        assert _cost(state) == 2.0
        assert state.sync_pricing()["repriced"] == 0
    finally:
        state.db.close()


def test_failed_reprice_does_not_publish_new_rates(tmp_path, monkeypatch):
    import pytest
    import token_dashboard.main as main

    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(_pricing_yaml(1.0))
    state = build_state(
        Config(db_path=":memory:", pricing_path=str(pricing_path), scan_roots={})
    )
    original = state.pricing

    def fail(*args, **kwargs):
        raise RuntimeError("database update failed")

    try:
        pricing_path.write_text(_pricing_yaml(2.0))
        monkeypatch.setattr(main, "reprice_if_needed", fail)
        with pytest.raises(RuntimeError, match="database update failed"):
            state.sync_pricing()
        assert state.pricing is original
    finally:
        state.db.close()
