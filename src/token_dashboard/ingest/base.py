"""Adapter protocol, canonical event, and the incremental/idempotent ingest runner.

Adding a new tool = one new Adapter subclass in this package + an entry in
registry.py. Everything below (watermarking, dedup, cost, insertion) is shared.
"""

from __future__ import annotations

import datetime as dt
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..db import Database
from ..pricing import Pricing, UsageBreakdown

_INGEST_LOCK = threading.Lock()
_PRICING_HASH_KEY = "pricing_hash"


@dataclass
class UsageEvent:
    event_id: str
    provider: str  # "claude" | "openai" | "local" ...
    tool: str  # "claude-code" | "codex-cli" ...
    ts: dt.datetime
    request_id: str | None = None
    session_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_5m: int = 0
    cache_create_1h: int = 0
    reasoning_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    service_tier: str | None = None
    project: str | None = None
    git_branch: str | None = None

    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


class Adapter(ABC):
    name: str  # config / scan-root key (e.g. "codex")
    provider: str  # vendor / pricing label (e.g. "openai")
    tool: str
    glob: str = "**/*.jsonl"
    #: If True the runner passes the stored byte offset and trusts new_offset for
    #: tailing. If False the adapter re-parses the whole file whenever it changes
    #: (needed when records depend on earlier context within the file).
    incremental: bool = True

    def discover(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return sorted(p for p in root.glob(self.glob) if p.is_file())

    @abstractmethod
    def parse(self, path: Path, from_offset: int) -> tuple[list[UsageEvent], int]:
        """Return (events, new_offset). new_offset is the byte position up to which
        the file has been fully consumed (a trailing partial line is left for next time)."""
        raise NotImplementedError


_COLUMNS = [
    "event_id",
    "provider",
    "tool",
    "request_id",
    "session_id",
    "model",
    "ts",
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
    "project",
    "git_branch",
    "source_file",
    "cost_usd",
]
_INSERT = (
    f"INSERT INTO usage_events ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * len(_COLUMNS))}) "
    f"ON CONFLICT (event_id) DO NOTHING"
)


def _pricing_key(provider: str, model: str | None) -> str:
    if provider == "claude":
        return "anthropic" if (model or "").startswith("claude") else "local"
    return provider


def _cost_for_usage(
    pricing: Pricing,
    provider: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_5m: int,
    cache_create_1h: int,
) -> float:
    return pricing.cost(
        _pricing_key(provider, model),
        model,
        UsageBreakdown(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_create_5m=cache_create_5m,
            cache_create_1h=cache_create_1h,
        ),
    )


def _row(ev: UsageEvent, pricing: Pricing, source_file: str) -> list:
    cost = _cost_for_usage(
        pricing,
        ev.provider,
        ev.model,
        ev.input_tokens,
        ev.output_tokens,
        ev.cache_read_tokens,
        ev.cache_create_5m,
        ev.cache_create_1h,
    )
    return [
        ev.event_id,
        ev.provider,
        ev.tool,
        ev.request_id,
        ev.session_id,
        ev.model,
        ev.ts,
        ev.input_tokens,
        ev.output_tokens,
        ev.cache_creation_tokens,
        ev.cache_read_tokens,
        ev.cache_create_5m,
        ev.cache_create_1h,
        ev.reasoning_tokens,
        ev.web_search_requests,
        ev.web_fetch_requests,
        ev.service_tier,
        ev.project,
        ev.git_branch,
        source_file,
        cost,
    ]


def _file_state(db: Database, source_file: str) -> tuple[int, float, int] | None:
    rows = db.query(
        "SELECT last_offset, last_mtime, last_size FROM ingest_state WHERE source_file = ?",
        [source_file],
    )
    return rows[0] if rows else None


def _metadata_value(db: Database, key: str) -> str | None:
    row = db.con.execute("SELECT value FROM app_metadata WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def reprice_if_needed(db: Database, pricing: Pricing, force: bool = False) -> dict:
    """Recompute stored costs only when the pricing file changed, unless forced."""
    current_hash = pricing.source_hash
    now = dt.datetime.now(dt.timezone.utc)

    with db.lock:
        previous_hash = _metadata_value(db, _PRICING_HASH_KEY)
        changed = current_hash is not None and current_hash != previous_hash
        if not force and not changed:
            return {
                "changed": False,
                "repriced": 0,
                "pricing_hash": current_hash,
                "previous_pricing_hash": previous_hash,
            }

        rows = db.con.execute(
            "SELECT event_id, provider, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_create_5m, cache_create_1h FROM usage_events"
        ).fetchall()
        updates = [
            (
                _cost_for_usage(
                    pricing,
                    provider,
                    model,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(cache_read_tokens or 0),
                    int(cache_create_5m or 0),
                    int(cache_create_1h or 0),
                ),
                event_id,
            )
            for (
                event_id,
                provider,
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_create_5m,
                cache_create_1h,
            ) in rows
        ]

        db.con.execute("BEGIN TRANSACTION")
        try:
            if updates:
                db.con.executemany(
                    "UPDATE usage_events SET cost_usd = ? WHERE event_id = ?",
                    updates,
                )
            if current_hash is not None:
                db.con.execute(
                    "INSERT INTO app_metadata (key, value, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                    "value = excluded.value, updated_at = excluded.updated_at",
                    [_PRICING_HASH_KEY, current_hash, now],
                )
            db.con.execute("COMMIT")
        except Exception:
            db.con.execute("ROLLBACK")
            raise

    return {
        "changed": changed,
        "repriced": len(updates),
        "pricing_hash": current_hash,
        "previous_pricing_hash": previous_hash,
    }


def ingest_one(db: Database, pricing: Pricing, adapter: Adapter, path: Path) -> int:
    """Ingest a single file; returns the number of new rows inserted."""
    stat = path.stat()
    source_file = str(path)
    prev = _file_state(db, source_file)
    if prev is not None:
        last_offset, last_mtime, last_size = prev
        if last_size == stat.st_size and last_mtime == stat.st_mtime:
            return 0  # unchanged — skip
        from_offset = last_offset if adapter.incremental else 0
    else:
        from_offset = 0

    events, new_offset = adapter.parse(path, from_offset)

    # Collapse within-batch duplicates (Claude repeats usage across sibling lines).
    deduped: dict[str, UsageEvent] = {}
    for ev in events:
        deduped.setdefault(ev.event_id, ev)

    rows = [_row(ev, pricing, source_file) for ev in deduped.values()]
    now = dt.datetime.now(dt.timezone.utc)
    with db.lock:
        before = db.con.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        if rows:
            db.con.executemany(_INSERT, rows)
        after = db.con.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        db.con.execute(
            "INSERT INTO ingest_state (source_file, last_offset, last_mtime, last_size, rows, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source_file) DO UPDATE SET "
            "last_offset = excluded.last_offset, last_mtime = excluded.last_mtime, "
            "last_size = excluded.last_size, rows = ingest_state.rows + excluded.rows, "
            "updated_at = excluded.updated_at",
            [source_file, new_offset, stat.st_mtime, stat.st_size, after - before, now],
        )
    return after - before


def ingest_all(
    db: Database, pricing: Pricing, adapters: Iterable[Adapter], roots: dict[str, Path]
) -> dict:
    """Run every adapter over its root. Serialized so DuckDB has a single writer."""
    summary: dict[str, dict] = {}
    with _INGEST_LOCK:
        for adapter in adapters:
            root = roots.get(adapter.name)
            if root is None:
                continue
            files = adapter.discover(root)
            inserted = 0
            changed = 0
            for path in files:
                try:
                    n = ingest_one(db, pricing, adapter, path)
                except Exception as exc:  # one bad file shouldn't abort the pass
                    print(f"[ingest] {adapter.name}: failed on {path}: {exc}")
                    continue
                if n:
                    changed += 1
                    inserted += n
            summary[adapter.name] = {
                "files_scanned": len(files),
                "files_with_new_rows": changed,
                "events_inserted": inserted,
            }
    return summary
