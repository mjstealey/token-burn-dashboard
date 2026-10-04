import datetime as dt

from token_dashboard.ingest.base import _INSERT, _row, ingest_one
from token_dashboard.ingest.base import UsageEvent
from token_dashboard.ingest.claude import ClaudeAdapter
from token_dashboard.ingest.codex import CodexAdapter
from token_dashboard import metrics

from conftest import write_jsonl

TZ = "America/New_York"


def _insert_event(db, pricing, **kw):
    """Insert one synthetic usage event directly (bypasses file parsing)."""
    defaults = dict(
        event_id=kw.get("event_id", "t:1"),
        provider="claude",
        tool="claude-code",
        ts=metrics.now_utc(),
    )
    ev = UsageEvent(**{**defaults, **kw})
    with db.lock:
        db.con.execute(_INSERT, _row(ev, pricing, "test"))


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


def test_summary_includes_prev_windows(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    s = metrics.summary(db, TZ)
    assert "prev_7d" in s and "prev_30d" in s
    assert {"tokens", "cost", "events"} <= set(s["prev_7d"])


def test_by_model_groups_both_providers(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    models = metrics.by_model(db, TZ)
    names = {m["model"] for m in models}
    assert "claude-opus-4-8" in names
    assert "gpt-5.3-codex" in names
    assert all("reasoning" in m for m in models)


def test_range_scoping_uses_local_days(db, pricing):
    now = metrics.now_utc()
    _insert_event(
        db,
        pricing,
        event_id="t:new",
        model="claude-opus-4-8",
        input_tokens=100,
        output_tokens=10,
        ts=now - dt.timedelta(days=1),
    )
    _insert_event(
        db,
        pricing,
        event_id="t:old",
        provider="openai",
        tool="codex-cli",
        model="gpt-5.3-codex",
        input_tokens=100,
        output_tokens=10,
        ts=now - dt.timedelta(days=40),
    )
    all_time = {m["model"] for m in metrics.by_model(db, TZ)}
    assert all_time == {"claude-opus-4-8", "gpt-5.3-codex"}
    recent = {m["model"] for m in metrics.by_model(db, TZ, days=7)}
    assert recent == {"claude-opus-4-8"}
    # Same scoping applies across the table queries.
    assert len(metrics.top_turns(db, TZ, days=7)) == 1
    assert len(metrics.top_turns(db, TZ)) == 2


def test_unpriced_models_flags_zero_rate_usage(db, pricing):
    _insert_event(
        db,
        pricing,
        event_id="t:gem",
        provider="gemini",
        tool="gemini-cli",
        model="gemini-3-pro",
        input_tokens=1000,
        output_tokens=100,
    )
    _insert_event(
        db,
        pricing,
        event_id="t:ok",
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=100,
    )
    _insert_event(  # local models are free on purpose — never flagged
        db,
        pricing,
        event_id="t:loc",
        model="llama3",
        input_tokens=1000,
        output_tokens=100,
    )
    flagged = metrics.unpriced_models(db, pricing)
    assert [m["model"] for m in flagged] == ["gemini-3-pro"]


def test_cache_savings_reads_minus_write_premium(db, pricing):
    # Opus 4.8: input 5.00, cache_read 0.50, cache_write_5m 6.25 (per 1M tokens).
    _insert_event(
        db,
        pricing,
        event_id="t:c",
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=4_000_000,
        cache_creation_tokens=1_000_000,
        cache_create_5m=1_000_000,
    )
    saved = metrics.cache_savings(db, pricing, TZ)
    # reads: 4M * (5.00 - 0.50)/1M = 18.00 ; write premium: 1M * (6.25 - 5.00)/1M = 1.25
    assert round(saved, 2) == 16.75


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
    turns = metrics.top_turns(db, TZ, limit=10)
    costs = [t["cost"] for t in turns]
    assert costs == sorted(costs, reverse=True)


def test_heatmap_model_filter(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    hm = metrics.heatmap(db, TZ, days=365, model="claude-opus-4-8")
    assert set(hm["providers"]) == {"claude"}
    assert sum(r["tokens"] for r in hm["combined"]) == 5500  # claude event only
    # '(unknown)' (the UI's NULL label) must match NULL models, not the literal.
    assert all(
        r["tokens"] == 0
        for r in metrics.heatmap(db, TZ, days=365, model="(unknown)")["combined"]
    )


def test_heatmap_project_filter(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    hm = metrics.heatmap(db, TZ, days=365, project="/proj-b")
    assert set(hm["providers"]) == {"openai"}
    assert sum(r["tokens"] for r in hm["combined"]) == 2800  # codex event only


def test_punchcard_buckets_local_weekday_hour(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    # 2026-06-18 is a Thursday (dow=4); America/New_York in June is UTC-4:
    # claude 12:00Z -> 08:00 local, codex 09:00:05Z -> 05:00 local.
    rows = metrics.punchcard(db, TZ)
    cells = {(r["dow"], r["hour"]): r["tokens"] for r in rows}
    assert cells == {(4, 8): 5500, (4, 5): 2800}
    assert all(r["events"] == 1 for r in rows)
    # Same model/range filters as the heat map.
    only_codex = metrics.punchcard(db, TZ, model="gpt-5.3-codex")
    assert [(r["dow"], r["hour"]) for r in only_codex] == [(4, 5)]
    assert metrics.punchcard(db, TZ, days=1) == []


def test_export_events_scoped_and_ordered(tmp_path, db, pricing):
    _seed(tmp_path, db, pricing)
    rows = metrics.export_events(db, TZ)
    assert len(rows) == 2
    assert [set(r) == set(metrics.EXPORT_COLUMNS) for r in rows]
    ts = [r["ts"] for r in rows]
    assert ts == sorted(ts)  # oldest first
    only_claude = metrics.export_events(db, TZ, model="claude-opus-4-8")
    assert [r["provider"] for r in only_claude] == ["claude"]
    assert metrics.export_events(db, TZ, days=1) == []  # seeded events are older


def test_heatmap_includes_quiet_days_and_ends_today(db, pricing):
    _insert_event(
        db, pricing, input_tokens=70, ts=metrics.now_utc() - dt.timedelta(days=6)
    )
    result = metrics.heatmap(db, TZ, days=7)
    assert result["start_day"] == "2026-06-14"
    assert result["end_day"] == "2026-06-20"
    assert [row["tokens"] for row in result["combined"]] == [70, 0, 0, 0, 0, 0, 0]
    assert len(result["providers"]["claude"]) == 7


def test_calendar_bounds_across_dst_and_local_midnight(db, pricing, monkeypatch):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(TZ)
    for index, (local_day, expected_hours) in enumerate(
        [
            (dt.date(2026, 3, 8), 23),
            (dt.date(2026, 11, 1), 25),
        ]
    ):
        # 23:30 local is already the following date in UTC.
        now = dt.datetime.combine(local_day, dt.time(23, 30), zone)
        monkeypatch.setattr(metrics, "now_utc", lambda: now.astimezone(dt.timezone.utc))
        start, end = metrics._day_bounds(TZ, 1, metrics.now_utc())
        assert (
            end.astimezone(dt.timezone.utc) - start.astimezone(dt.timezone.utc)
        ).total_seconds() == expected_hours * 3600
        db.execute("DELETE FROM usage_events")
        for suffix, ts in [
            ("before", start - dt.timedelta(microseconds=1)),
            ("start", start),
            ("end", end),
        ]:
            _insert_event(
                db, pricing, event_id=f"{index}:{suffix}", input_tokens=10, ts=ts
            )
        rows = metrics.top_turns(db, TZ, days=1)
        assert len(rows) == 1
        assert rows[0]["ts"] == start
        assert metrics.heatmap(db, TZ, days=1)["end_day"] == local_day.isoformat()


def test_export_batches_keep_snapshot_during_other_queries(db, pricing):
    for i in range(5):
        _insert_event(db, pricing, event_id=f"batch:{i}", input_tokens=i + 1)
    batches = metrics.export_event_batches(db, TZ, batch_size=2)
    first = next(batches)
    assert len(first) == 2
    _insert_event(db, pricing, event_id="later", input_tokens=99)
    assert db.query("SELECT COUNT(*) FROM usage_events")[0][0] == 6
    rest = list(batches)
    assert [len(batch) for batch in rest] == [2, 1]
    assert {row["event_id"] for batch in [first, *rest] for row in batch} == {
        f"batch:{i}" for i in range(5)
    }
