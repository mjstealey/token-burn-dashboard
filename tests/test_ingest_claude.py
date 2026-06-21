from token_dashboard.ingest.base import ingest_one
from token_dashboard.ingest.claude import ClaudeAdapter

from conftest import write_jsonl


def _assistant(
    request_id,
    uuid,
    usage,
    ts="2026-06-18T12:00:00.000Z",
    message_id="default",
):
    msg = {
        "model": "claude-opus-4-8",
        "usage": usage,
    }
    if message_id is None:
        pass
    elif message_id == "default":
        msg["id"] = "msg_" + request_id
    else:
        msg["id"] = message_id

    rec = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "sess-1",
        "timestamp": ts,
        "cwd": "/proj",
        "gitBranch": "main",
        "message": msg,
    }
    if request_id is not None:
        rec["requestId"] = request_id
    return rec


USAGE = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 8000,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 2000,
        "ephemeral_1h_input_tokens": 0,
    },
    "service_tier": "standard",
}


def test_dedup_by_request_id(tmp_path, db, pricing):
    # One API turn split into 3 sibling lines (thinking/text/tool_use), all repeating
    # the IDENTICAL usage payload, plus a second distinct turn.
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            _assistant("reqA", "uuid-1", USAGE),
            _assistant("reqA", "uuid-2", USAGE),
            _assistant("reqA", "uuid-3", USAGE),
            _assistant("reqB", "uuid-4", USAGE),
        ],
    )
    inserted = ingest_one(db, pricing, ClaudeAdapter(), f)
    assert inserted == 2  # one row per requestId, NOT 4

    rows = db.query(
        "SELECT SUM(input_tokens), SUM(cache_read_tokens) FROM usage_events"
    )
    assert rows[0][0] == 2000  # 2 * 1000, not 4 * 1000
    assert rows[0][1] == 16000  # 2 * 8000


def test_dedup_by_message_id_when_request_id_missing(tmp_path, db, pricing):
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            _assistant(None, "uuid-1", USAGE, message_id="msg_shared"),
            _assistant(None, "uuid-2", USAGE, message_id="msg_shared"),
            _assistant(None, "uuid-3", USAGE, message_id="msg_shared"),
        ],
    )
    assert ingest_one(db, pricing, ClaudeAdapter(), f) == 1
    assert db.query("SELECT SUM(input_tokens) FROM usage_events")[0][0] == 1000


def test_skips_uuid_only_usage_records(tmp_path, db, pricing):
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            _assistant(None, "uuid-1", USAGE, message_id=None),
            _assistant(None, "uuid-2", USAGE, message_id=None),
        ],
    )
    assert ingest_one(db, pricing, ClaudeAdapter(), f) == 0
    assert db.query("SELECT COUNT(*) FROM usage_events")[0][0] == 0


def test_idempotent_reingest(tmp_path, db, pricing):
    f = tmp_path / "session.jsonl"
    write_jsonl(f, [_assistant("reqA", "uuid-1", USAGE)])
    assert ingest_one(db, pricing, ClaudeAdapter(), f) == 1
    # Unchanged file -> watermark skips it entirely.
    assert ingest_one(db, pricing, ClaudeAdapter(), f) == 0
    assert db.query("SELECT COUNT(*) FROM usage_events")[0][0] == 1


def test_append_new_turn_then_resume_no_double_count(tmp_path, db, pricing):
    f = tmp_path / "session.jsonl"
    write_jsonl(f, [_assistant("reqA", "uuid-1", USAGE)])
    ingest_one(db, pricing, ClaudeAdapter(), f)

    # Append a new turn AND re-emit reqA (as a resume would) — only the new one counts.
    with open(f, "a") as fh:
        import json

        fh.write(json.dumps(_assistant("reqB", "uuid-2", USAGE)) + "\n")
        fh.write(json.dumps(_assistant("reqA", "uuid-9", USAGE)) + "\n")
    inserted = ingest_one(db, pricing, ClaudeAdapter(), f)
    assert inserted == 1  # reqB only; reqA conflicts on event_id
    assert db.query("SELECT COUNT(*) FROM usage_events")[0][0] == 2


def test_cost_is_computed(tmp_path, db, pricing):
    f = tmp_path / "session.jsonl"
    write_jsonl(f, [_assistant("reqA", "uuid-1", USAGE)])
    ingest_one(db, pricing, ClaudeAdapter(), f)
    cost = db.query("SELECT cost_usd FROM usage_events")[0][0]
    # 1000*5 + 500*25 + 8000*0.5 + 2000*6.25 = 5000+12500+4000+12500 = 34000 (micro-$)
    assert round(cost, 6) == round(34000 / 1_000_000, 6)
