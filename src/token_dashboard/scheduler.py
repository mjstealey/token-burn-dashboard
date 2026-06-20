"""In-process periodic ingest via APScheduler.

Runs alongside the web server: one process owns all DuckDB writes, satisfying its
single-writer model. Combined with the entrypoint's ingest-on-boot, this keeps the
dashboard fresh both on startup and on a schedule.
"""

from __future__ import annotations

from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler


def start_scheduler(
    ingest_fn: Callable[[], None], interval_min: int
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        ingest_fn,
        trigger="interval",
        minutes=max(interval_min, 1),
        id="ingest",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
