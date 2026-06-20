"""Read-side aggregation queries.

All day bucketing uses the configured local timezone (`ts AT TIME ZONE tz` converts
the stored UTC timestamp to local wall-clock) so calendar days line up with the
user's day, not UTC.
"""

from __future__ import annotations

from typing import Any

from .db import Database

# Total tokens across every billable class.
TOK = "(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens)"


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
    today = _window(
        db,
        "CAST(ts AT TIME ZONE ? AS DATE) = CAST(now() AT TIME ZONE ? AS DATE)",
        [tz, tz],
    )
    last_7d = _window(db, "ts >= now() - (7 * INTERVAL '1 day')", [])
    last_30d = _window(db, "ts >= now() - (30 * INTERVAL '1 day')", [])
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
        "all_time": all_time,
        "meta": meta,
        "providers": providers,
    }


def heatmap(db: Database, tz: str, days: int = 365, metric: str = "cost") -> dict:
    rows = db.query_dicts(
        f"""
        SELECT CAST(ts AT TIME ZONE ? AS DATE) AS day,
               COALESCE(SUM(cost_usd),0) AS cost,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read_tokens),0) AS cache_read,
               COALESCE(SUM(cache_creation_tokens),0) AS cache_creation
        FROM usage_events
        WHERE ts >= now() - (CAST(? AS INTEGER) * INTERVAL '1 day')
        GROUP BY day ORDER BY day
        """,
        [tz, days],
    )
    for r in rows:
        r["day"] = r["day"].isoformat() if r["day"] else None
    return {"metric": metric, "days": days, "series": rows}


def by_model(db: Database) -> list[dict]:
    return db.query_dicts(
        f"""
        SELECT provider, COALESCE(model, '(unknown)') AS model,
               COUNT(*) AS events,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read_tokens),0) AS cache_read,
               COALESCE(SUM(cache_creation_tokens),0) AS cache_creation
        FROM usage_events
        GROUP BY provider, model
        ORDER BY cost DESC
        """
    )


def by_project(db: Database, limit: int = 30) -> list[dict]:
    return db.query_dicts(
        f"""
        SELECT COALESCE(project, '(none)') AS project, provider,
               COUNT(*) AS events,
               COUNT(DISTINCT session_id) AS sessions,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               MAX(ts) AS last_ts
        FROM usage_events
        GROUP BY project, provider
        ORDER BY cost DESC
        LIMIT ?
        """,
        [limit],
    )


def top_sessions(db: Database, limit: int = 25) -> list[dict]:
    return db.query_dicts(
        f"""
        SELECT session_id, ANY_VALUE(provider) AS provider,
               ANY_VALUE(model) AS model, ANY_VALUE(project) AS project,
               COUNT(*) AS turns,
               COALESCE(SUM({TOK}),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM usage_events
        WHERE session_id IS NOT NULL
        GROUP BY session_id
        ORDER BY cost DESC
        LIMIT ?
        """,
        [limit],
    )


def top_turns(db: Database, limit: int = 25) -> list[dict]:
    """Most expensive single requests — the 'where did the burn go' view."""
    return db.query_dicts(
        f"""
        SELECT provider, model, project, session_id, ts,
               input_tokens AS input, output_tokens AS output,
               cache_read_tokens AS cache_read, cache_creation_tokens AS cache_creation,
               {TOK} AS tokens, cost_usd AS cost
        FROM usage_events
        ORDER BY cost_usd DESC
        LIMIT ?
        """,
        [limit],
    )


def burn(db: Database, tz: str, limit_5h: int | None, limit_week: int | None) -> dict:
    block = _window(db, "ts >= now() - (5 * INTERVAL '1 hour')", [])
    week = _window(db, "ts >= now() - (7 * INTERVAL '1 day')", [])
    day1 = _window(db, "ts >= now() - (1 * INTERVAL '1 day')", [])

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
