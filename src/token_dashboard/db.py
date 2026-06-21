"""DuckDB connection, schema, and a small thread-safe access wrapper.

DuckDB is single-writer; we keep one process-wide connection guarded by a lock.
Reads and writes both go through the lock — fine at single-user scale and it keeps
the scheduler's ingest pass from racing the request handlers.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    event_id              VARCHAR PRIMARY KEY,
    provider              VARCHAR NOT NULL,
    tool                  VARCHAR NOT NULL,
    request_id            VARCHAR,
    session_id            VARCHAR,
    model                 VARCHAR,
    ts                    TIMESTAMPTZ NOT NULL,
    input_tokens          BIGINT  DEFAULT 0,
    output_tokens         BIGINT  DEFAULT 0,
    cache_creation_tokens BIGINT  DEFAULT 0,
    cache_read_tokens     BIGINT  DEFAULT 0,
    cache_create_5m       BIGINT  DEFAULT 0,
    cache_create_1h       BIGINT  DEFAULT 0,
    reasoning_tokens      BIGINT  DEFAULT 0,
    web_search_requests   BIGINT  DEFAULT 0,
    web_fetch_requests    BIGINT  DEFAULT 0,
    service_tier          VARCHAR,
    project               VARCHAR,
    git_branch            VARCHAR,
    source_file           VARCHAR,
    cost_usd              DOUBLE  DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_usage_ts       ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_events(provider);
CREATE INDEX IF NOT EXISTS idx_usage_model    ON usage_events(model);

-- Incremental-ingestion watermark, one row per source file.
CREATE TABLE IF NOT EXISTS ingest_state (
    source_file VARCHAR PRIMARY KEY,
    last_offset BIGINT,
    last_mtime  DOUBLE,
    last_size   BIGINT,
    rows        BIGINT,
    updated_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR,
    updated_at TIMESTAMPTZ
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = duckdb.connect(
            str(Path(path).expanduser()) if path != ":memory:" else path
        )
        with self._lock:
            self._con.execute(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        return self._con

    def query(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, params or []).fetchall()

    def query_dicts(
        self, sql: str, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        with self._lock:
            self._con.execute(sql, params or [])

    def close(self) -> None:
        with self._lock:
            self._con.close()
