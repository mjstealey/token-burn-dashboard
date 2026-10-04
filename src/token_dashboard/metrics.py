"""Read-side aggregation queries.

All day bucketing uses the configured local timezone (`ts AT TIME ZONE tz` converts
the stored UTC timestamp to local wall-clock) so calendar days line up with the
user's day, not UTC.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo
from typing import Any, Iterator

from .db import Database
from .pricing import PER_TOKEN, Pricing, pricing_key

# Total tokens across every billable class.
TOK = "(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens)"


def now_utc() -> dt.datetime:
    """Single clock boundary; tests can inject a fixed reference instant."""
    return dt.datetime.now(dt.timezone.utc)


def _day_bounds(
    tz: str, days: int, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    start = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time(), zone)
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), zone)
    return start, end


def _since(
    tz: str, days: int | None, now: dt.datetime | None = None
) -> tuple[str, list[Any]]:
    """Local calendar days, with UTC timestamp bounds that preserve DST changes."""
    if not days or days <= 0:
        return "1=1", []
    start, end = _day_bounds(tz, days, now or now_utc())
    return "ts >= ? AND ts < ?", [start, end]


def _filters(
    tz: str,
    days: int | None,
    model: str | None = None,
    project: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[str, list[Any]]:
    """Range clause plus optional exact model/project filters. The UI's grouped
    views label NULLs '(unknown)'/'(none)', so those spellings match the NULLs."""
    where, params = _since(tz, days, now)
    clauses = [where]
    if model is not None:
        if model == "(unknown)":
            clauses.append("model IS NULL")
        else:
            clauses.append("model = ?")
            params.append(model)
    if project is not None:
        if project == "(none)":
            clauses.append("project IS NULL")
        else:
            clauses.append("project = ?")
            params.append(project)
    return " AND ".join(clauses), params


def _one(db: Database, sql: str, params: list[Any] | None = None) -> dict:
    rows = db.query_dicts(sql, params)
    return rows[0] if rows else {}


def _window(db: Database, where: str, params: list[Any]) -> dict:
    sql = (
        f"SELECT COALESCE(SUM({TOK}),0) AS tokens, "
        f"COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS events "
        f"FROM usage_events WHERE {where}"
    )
    return _one(db, sql, params)


def summary(db: Database, tz: str) -> dict:
    now = now_utc()
    start, end = _day_bounds(tz, 1, now)
    today = _window(db, "ts >= ? AND ts < ?", [start, end])

    def rolling(days: int, offset: int = 0) -> dict:
        return _window(
            db,
            "ts >= ? AND ts < ?",
            [now - dt.timedelta(days=days + offset), now - dt.timedelta(days=offset)],
        )

    last_7d = rolling(7)
    last_30d = rolling(30)
    prev_7d = rolling(7, 7)
    prev_30d = rolling(30, 30)
    all_time = _window(db, "1=1", [])

    meta = _one(
        db,
        "SELECT COUNT(*) AS events, COUNT(DISTINCT session_id) AS sessions, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM usage_events",
    )
    providers = db.query_dicts(
        f"SELECT provider, COALESCE(SUM({TOK}),0) AS tokens, "
        "COALESCE(SUM(cost_usd),0) AS cost, MAX(ts) AS last_ts "
        "FROM usage_events GROUP BY provider ORDER BY cost DESC"
    )
    return {
        "today": today,
        "last_7d": last_7d,
        "last_30d": last_30d,
        "prev_7d": prev_7d,
        "prev_30d": prev_30d,
        "all_time": all_time,
        "meta": meta,
        "providers": providers,
    }


_DAY_FIELDS = ("cost", "tokens", "input", "output", "cache_read", "cache_creation")


def heatmap(
    db: Database,
    tz: str,
    days: int = 365,
    metric: str = "cost",
    model: str | None = None,
    project: str | None = None,
) -> dict:
    """Daily series bucketed by local day, split per provider plus a combined total.

    Returns ``combined`` (all providers summed per day) and ``providers`` (a map of
    provider -> daily series). ``series`` is kept as an alias for ``combined`` for
    backwards compatibility. ``model``/``project`` scope the series to one model or
    one project directory (the dashboard's click-to-filter).
    """
    now = now_utc()
    where, params = _filters(tz, days, model, project, now)
    rows = db.query_dicts(
        f"""
        SELECT CAST(ts AT TIME ZONE ? AS DATE) AS day, provider,
               COALESCE(SUM(cost_usd),0) AS cost,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read_tokens),0) AS cache_read,
               COALESCE(SUM(cache_creation_tokens),0) AS cache_creation
        FROM usage_events
        WHERE {where}
        GROUP BY day, provider ORDER BY day
        """,
        [tz, *params],
    )

    providers: dict[str, list[dict]] = {}
    combined: dict[str, dict] = {}
    for r in rows:
        day = r["day"].isoformat() if r["day"] else None
        if day is None:
            continue
        prov = r["provider"]
        entry = {"day": day, **{f: r[f] for f in _DAY_FIELDS}}
        providers.setdefault(prov, []).append(entry)

        agg = combined.setdefault(day, {"day": day, **{f: 0 for f in _DAY_FIELDS}})
        for f in _DAY_FIELDS:
            agg[f] += r[f]

    start, end = _day_bounds(tz, days, now)
    dates = [(start.date() + dt.timedelta(days=i)).isoformat() for i in range(days)]

    def fill(series: list[dict]) -> list[dict]:
        indexed = {row["day"]: row for row in series}
        return [
            indexed.get(day, {"day": day, **{f: 0 for f in _DAY_FIELDS}})
            for day in dates
        ]

    combined_series = fill(list(combined.values()))
    return {
        "metric": metric,
        "days": days,
        "start_day": dates[0],
        "end_day": dates[-1],
        "combined": combined_series,
        "providers": {provider: fill(series) for provider, series in providers.items()},
        "series": combined_series,
    }


def punchcard(
    db: Database,
    tz: str,
    days: int | None = None,
    model: str | None = None,
    project: str | None = None,
) -> list[dict]:
    """Local weekday x hour totals — the 'when does the burn happen' view.

    ``dow`` is 0=Sunday..6=Saturday (DuckDB/Postgres EXTRACT semantics), matching
    the calendar heat map's Sunday-first rows.
    """
    where, params = _filters(tz, days, model, project)
    return db.query_dicts(
        f"""
        SELECT EXTRACT(dow FROM (ts AT TIME ZONE ?)) AS dow,
               EXTRACT(hour FROM (ts AT TIME ZONE ?)) AS hour,
               COALESCE(SUM(cost_usd),0) AS cost,
               COALESCE(SUM({TOK}),0) AS tokens,
               COUNT(*) AS events
        FROM usage_events
        WHERE {where}
        GROUP BY dow, hour
        ORDER BY dow, hour
        """,
        [tz, tz, *params],
    )


def by_model(db: Database, tz: str, days: int | None = None) -> list[dict]:
    where, params = _since(tz, days)
    return db.query_dicts(
        f"""
        SELECT provider, COALESCE(model, '(unknown)') AS model,
               COUNT(*) AS events,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read_tokens),0) AS cache_read,
               COALESCE(SUM(cache_creation_tokens),0) AS cache_creation,
               COALESCE(SUM(reasoning_tokens),0) AS reasoning
        FROM usage_events
        WHERE {where}
        GROUP BY provider, model
        ORDER BY cost DESC
        """,
        params,
    )


def by_project(
    db: Database, tz: str, days: int | None = None, limit: int = 30
) -> list[dict]:
    where, params = _since(tz, days)
    return db.query_dicts(
        f"""
        SELECT COALESCE(project, '(none)') AS project, provider,
               COUNT(*) AS events,
               COUNT(DISTINCT session_id) AS sessions,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               MAX(ts) AS last_ts
        FROM usage_events
        WHERE {where}
        GROUP BY project, provider
        ORDER BY cost DESC
        LIMIT ?
        """,
        [*params, limit],
    )


def top_sessions(
    db: Database, tz: str, days: int | None = None, limit: int = 25
) -> list[dict]:
    where, params = _since(tz, days)
    return db.query_dicts(
        f"""
        SELECT session_id, ANY_VALUE(provider) AS provider,
               ANY_VALUE(model) AS model, ANY_VALUE(project) AS project,
               COUNT(*) AS turns,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM usage_events
        WHERE session_id IS NOT NULL AND {where}
        GROUP BY session_id
        ORDER BY cost DESC
        LIMIT ?
        """,
        [*params, limit],
    )


def top_turns(
    db: Database, tz: str, days: int | None = None, limit: int = 25
) -> list[dict]:
    """Most expensive single requests — the 'where did the burn go' view."""
    where, params = _since(tz, days)
    return db.query_dicts(
        f"""
        SELECT provider, model, project, session_id, ts,
               input_tokens AS input, output_tokens AS output,
               cache_read_tokens AS cache_read, cache_creation_tokens AS cache_creation,
               {TOK} AS tokens, cost_usd AS cost
        FROM usage_events
        WHERE {where}
        ORDER BY cost_usd DESC
        LIMIT ?
        """,
        [*params, limit],
    )


# Raw-event export, analytically-primary columns first.
EXPORT_COLUMNS = [
    "ts",
    "provider",
    "tool",
    "model",
    "project",
    "git_branch",
    "session_id",
    "request_id",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "cache_create_5m",
    "cache_create_1h",
    "reasoning_tokens",
    "web_search_requests",
    "web_fetch_requests",
    "service_tier",
    "cost_usd",
    "event_id",
    "source_file",
]


def export_events(
    db: Database,
    tz: str,
    days: int | None = None,
    model: str | None = None,
    project: str | None = None,
) -> list[dict]:
    """Raw usage events for downstream analysis, oldest first, honoring the same
    range and model/project filters as the dashboard."""
    where, params = _filters(tz, days, model, project)
    return db.query_dicts(
        f"SELECT {', '.join(EXPORT_COLUMNS)} FROM usage_events "
        f"WHERE {where} ORDER BY ts",
        params,
    )


def export_event_batches(
    db: Database,
    tz: str,
    days: int | None = None,
    model: str | None = None,
    project: str | None = None,
    batch_size: int = 1000,
) -> Iterator[list[dict]]:
    """Stream one query snapshot in bounded Python batches."""
    where, params = _filters(tz, days, model, project)
    return db.query_batches(
        f"SELECT {', '.join(EXPORT_COLUMNS)} FROM usage_events "
        f"WHERE {where} ORDER BY ts, event_id",
        params,
        batch_size=batch_size,
    )


def unpriced_models(db: Database, pricing: Pricing) -> list[dict]:
    """Models with real usage whose resolved rate is all-zero — every $ figure on
    the page silently undercounts while any of these exist."""
    rows = db.query_dicts(
        f"SELECT provider, model, COALESCE(SUM({TOK}),0) AS tokens "
        "FROM usage_events GROUP BY provider, model ORDER BY tokens DESC"
    )
    return [
        {"provider": r["provider"], "model": r["model"], "tokens": r["tokens"]}
        for r in rows
        if (r["tokens"] or 0) > 0 and pricing.is_unpriced(r["provider"], r["model"])
    ]


def cache_savings(
    db: Database, pricing: Pricing, tz: str, days: int | None = None
) -> float:
    """Notional $ saved by prompt caching vs billing the same tokens uncached:
    reads billed at cache_read instead of input, minus the write-tier premium."""
    where, params = _since(tz, days)
    rows = db.query_dicts(
        "SELECT provider, model, "
        "COALESCE(SUM(cache_read_tokens),0) AS cr, "
        "COALESCE(SUM(cache_create_5m),0) AS c5, "
        "COALESCE(SUM(cache_create_1h),0) AS c1 "
        f"FROM usage_events WHERE {where} GROUP BY provider, model",
        params,
    )
    total = 0.0
    for r in rows:
        rate = pricing.rate(pricing_key(r["provider"], r["model"]), r["model"])
        total += (
            r["cr"] * (rate.input - rate.cache_read)
            - r["c5"] * (rate.cache_write_5m - rate.input)
            - r["c1"] * (rate.cache_write_1h - rate.input)
        )
    return total / PER_TOKEN


def burn(db: Database, tz: str, limit_5h: int | None, limit_week: int | None) -> dict:
    now = now_utc()

    def trailing(hours: int) -> dict:
        return _window(
            db, "ts >= ? AND ts <= ?", [now - dt.timedelta(hours=hours), now]
        )

    block = trailing(5)
    week = trailing(7 * 24)
    day1 = trailing(24)

    daily_avg_tokens = (week.get("tokens") or 0) / 7.0
    daily_avg_cost = (week.get("cost") or 0) / 7.0

    out = {
        "block_5h": {
            "tokens": block.get("tokens", 0),
            "cost": block.get("cost", 0),
            "limit_tokens": limit_5h,
            "utilization": (block.get("tokens", 0) / limit_5h) if limit_5h else None,
        },
        "week": {
            "tokens": week.get("tokens", 0),
            "cost": week.get("cost", 0),
            "limit_tokens": limit_week,
            "utilization": (week.get("tokens", 0) / limit_week) if limit_week else None,
        },
        "rate": {
            "tokens_per_hour_24h": (day1.get("tokens") or 0) / 24.0,
            "cost_per_hour_24h": (day1.get("cost") or 0) / 24.0,
        },
        "forecast_30d": {
            "tokens": daily_avg_tokens * 30,
            "cost": daily_avg_cost * 30,
            "daily_avg_tokens": daily_avg_tokens,
            "daily_avg_cost": daily_avg_cost,
        },
    }
    if limit_5h:
        rate_per_h = (day1.get("tokens") or 0) / 24.0
        remaining = max(limit_5h - block.get("tokens", 0), 0)
        out["block_5h"]["hours_to_limit"] = (
            (remaining / rate_per_h) if rate_per_h else None
        )
    return out
