from token_dashboard.ingest.base import ingest_one
from token_dashboard.ingest.claude import ClaudeAdapter
from token_dashboard.ingest.codex import CodexAdapter
from token_dashboard import metrics

from conftest import write_jsonl

TZ = "America/New_York"


def _seed(tmp_path, db, pricing):
    claude = tmp_path / "session.jsonl"
    write_jsonl(
        claude,
        [
            {
                "type": "assistant",
                "uuid": "u1",
                "requestId": "rq1",
                "sessionId": "s1",
                "timestamp": "2026-06-18T12:00:00Z",
                "cwd": "/proj-a",
                "message": {
                    "model": "claude-opus-4-8",
                    "id": "m1",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 4000,
                    },
                },
            }
        ],
    )
    ingest_one(db, pricing, ClaudeAdapter(), claude)

    codex = (
        tmp_path
        / "rollout-2026-06-18T09-00-00-12345678-1234-1234-1234-123456789abc.jsonl"
    )
    write_jsonl(
        codex,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-06-18T09:00:00Z",
                "payload": {"id": "sc1", "cwd": "/proj-b"},
            },
            {
                "type": "turn_context",
                "timestamp": "2026-06-18T09:00:01Z",
                "payload": {"model": "gpt-5.3-codex", "cwd": "/proj-b"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-06-18T09:00:05Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 2000,
                            "cached_input_tokens": 0,
                            "output_tokens": 800,
                            "reasoning_output_tokens": 0,
                        }
                    },
                },
            },
        ],
    )
    ingest_one(db, pricing, CodexAdapter(), codex)


def test_summary_all_time(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    s = metrics.summary(db, TZ)
    assert s["all_time"]["events"] == 2
    # tokens: claude 1000+500+0+4000 = 5500 ; codex 2000+800 = 2800
    assert s["all_time"]["tokens"] == 8300
    assert s["all_time"]["cost"] > 0
    assert len(s["providers"]) == 2


def test_by_model_groups_both_providers(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    models = metrics.by_model(db)
    names = {m["model"] for m in models}
    assert "claude-opus-4-8" in names
    assert "gpt-5.3-codex" in names


def test_heatmap_buckets_by_day(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    hm = metrics.heatmap(db, TZ, days=365)
    assert hm["combined"], "expected at least one day"
    assert hm["series"] == hm["combined"]  # back-compat alias
    for row in hm["combined"]:
        assert isinstance(row["day"], str) and len(row["day"]) == 10


def test_heatmap_splits_by_provider(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    hm = metrics.heatmap(db, TZ, days=365)
    # One claude event + one codex event were seeded.
    assert set(hm["providers"]) == {"claude", "openai"}

    # Combined per-day totals must equal the sum across providers for that day.
    per_day_provider = {}
    for prov, series in hm["providers"].items():
        for row in series:
            per_day_provider.setdefault(row["day"], 0)
            per_day_provider[row["day"]] += row["tokens"]
    for row in hm["combined"]:
        assert row["tokens"] == per_day_provider[row["day"]]


def test_top_turns_sorted_desc(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    turns = metrics.top_turns(db, limit=10)
    costs = [t["cost"] for t in turns]
    assert costs == sorted(costs, reverse=True)
