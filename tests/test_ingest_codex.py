from token_dashboard.ingest.base import ingest_one
from token_dashboard.ingest.codex import CodexAdapter

from conftest import write_jsonl


def _token_count(last, total, ts):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": last, "total_token_usage": total},
        },
    }


def _records():
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-06-18T09:00:00Z",
            "payload": {"id": "sess-codex-1", "cwd": "/proj"},
        },
        {
            "type": "turn_context",
            "timestamp": "2026-06-18T09:00:01Z",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/proj"},
        },
        _token_count(
            {
                "input_tokens": 1000,
                "cached_input_tokens": 200,
                "output_tokens": 300,
                "reasoning_output_tokens": 100,
            },
            {"input_tokens": 1000, "output_tokens": 300},  # cumulative
            "2026-06-18T09:00:05Z",
        ),
        _token_count(
            {
                "input_tokens": 500,
                "cached_input_tokens": 100,
                "output_tokens": 200,
                "reasoning_output_tokens": 50,
            },
            {"input_tokens": 1500, "output_tokens": 500},  # cumulative grows
            "2026-06-18T09:00:09Z",
        ),
    ]


def test_uses_deltas_not_cumulative(tmp_path, db, pricing):
    f = (
        tmp_path
        / "rollout-2026-06-18T09-00-00-1a2b3c4d-5e6f-7a8b-9c0d-112233445566.jsonl"
    )
    write_jsonl(f, _records())
    inserted = ingest_one(db, pricing, CodexAdapter(), f)
    assert inserted == 2  # two token_count records

    row = db.query(
        "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
        "SUM(reasoning_tokens) FROM usage_events"
    )[0]
    # uncached input = (1000-200)+(500-100)=1200 ; output=500 ; cached=300 ; reasoning=150
    # If we wrongly summed total_token_usage, input would be 2500.
    assert row[0] == 1200
    assert row[1] == 500
    assert row[2] == 300
    assert row[3] == 150


def test_model_joined_from_turn_context(tmp_path, db, pricing):
    f = (
        tmp_path
        / "rollout-2026-06-18T09-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    write_jsonl(f, _records())
    ingest_one(db, pricing, CodexAdapter(), f)
    models = db.query("SELECT DISTINCT model FROM usage_events")
    assert models == [("gpt-5.3-codex",)]


def test_stable_event_ids_idempotent(tmp_path, db, pricing):
    f = (
        tmp_path
        / "rollout-2026-06-18T09-00-00-99999999-8888-7777-6666-555555555555.jsonl"
    )
    write_jsonl(f, _records())
    assert ingest_one(db, pricing, CodexAdapter(), f) == 2
    # Unchanged -> skipped.
    assert ingest_one(db, pricing, CodexAdapter(), f) == 0
    assert db.query("SELECT COUNT(*) FROM usage_events")[0][0] == 2
