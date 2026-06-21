#!/usr/bin/env python
"""Generate light + dark dashboard screenshots for the README.

It seeds a synthetic demo DuckDB (fabricated providers/models/dates — so the
committed images contain no real project paths or spend), serves the app
in-process, drives headless Chromium via Playwright, and writes
``docs/screenshot-light.png`` and ``docs/screenshot-dark.png``.

Usage:
    uv run python scripts/screenshot.py            # regenerate both images
    uv run playwright install chromium             # one-time, installs the browser

Notes:
    - The page loads ECharts from a CDN, so this needs network access to render
      the charts. For an offline run, vendor echarts.min.js (see README).
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from token_dashboard.config import Config  # noqa: E402
from token_dashboard.ingest.base import _INSERT, UsageEvent, _row  # noqa: E402
from token_dashboard.main import build_state, create_app  # noqa: E402

CLAUDE_MODELS = [
    "claude-opus-4-8",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]
CLAUDE_PROJECTS = ["~/code/api", "~/code/web", "~/code/infra", "~/notes", "~/code/api"]
CODEX_PROJECTS = ["~/code/api", "~/code/cli", "~/scratch"]


def seed(db, pricing, days: int = 365, rng_seed: int = 42) -> int:
    """Insert synthetic usage so the heat map and tables look populated."""
    rnd = random.Random(rng_seed)
    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    events: list[UsageEvent] = []
    eid = 0

    for d in range(days):
        day = end - dt.timedelta(days=d)

        # Claude on ~68% of days, with a varying daily intensity for color spread.
        if rnd.random() < 0.68:
            mag = rnd.choice([0.2, 0.4, 0.6, 1.0, 1.0, 1.6, 2.6])
            session = f"demo-claude-{d}-{rnd.randint(0, 3)}"
            project = rnd.choice(CLAUDE_PROJECTS)
            for _ in range(rnd.randint(1, 6)):
                eid += 1
                ts = day - dt.timedelta(
                    hours=rnd.randint(0, 12), minutes=rnd.randint(0, 59)
                )
                cwrite = int(rnd.randint(1500, 9000) * mag)
                events.append(
                    UsageEvent(
                        event_id=f"demo:claude:{eid}",
                        provider="claude",
                        tool="claude-code",
                        ts=ts,
                        request_id=f"req-{eid}",
                        session_id=session,
                        model=rnd.choice(CLAUDE_MODELS),
                        input_tokens=rnd.randint(40, 600),
                        output_tokens=int(rnd.randint(300, 3000) * mag),
                        cache_creation_tokens=cwrite,
                        cache_read_tokens=int(rnd.randint(40000, 280000) * mag),
                        cache_create_5m=cwrite,
                        project=project,
                        git_branch="main",
                    )
                )

        # Codex on ~40% of days.
        if rnd.random() < 0.40:
            mag = rnd.choice([0.3, 0.5, 1.0, 1.0, 1.8])
            session = f"demo-codex-{d}"
            project = rnd.choice(CODEX_PROJECTS)
            for _ in range(rnd.randint(1, 4)):
                eid += 1
                ts = day - dt.timedelta(
                    hours=rnd.randint(0, 12), minutes=rnd.randint(0, 59)
                )
                events.append(
                    UsageEvent(
                        event_id=f"demo:codex:{eid}",
                        provider="openai",
                        tool="codex-cli",
                        ts=ts,
                        session_id=session,
                        model="gpt-5.3-codex",
                        input_tokens=int(rnd.randint(2000, 20000) * mag),
                        output_tokens=int(rnd.randint(200, 2500) * mag),
                        cache_read_tokens=int(rnd.randint(5000, 60000) * mag),
                        reasoning_tokens=rnd.randint(0, 800),
                        project=project,
                    )
                )

    rows = [_row(ev, pricing, "demo") for ev in events]
    with db.lock:
        db.con.executemany(_INSERT, rows)
    return len(rows)


def start_server(app, port: int):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.config.install_signal_handlers = lambda: None  # we run it in a thread
    threading.Thread(target=server.run, daemon=True).start()
    return server


def wait_until_ready(port: int, timeout: float = 30.0) -> None:
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    raise RuntimeError(f"server did not become ready on :{port}")


def capture(base_url: str, out_dir: Path, width: int) -> None:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme in ("light", "dark"):
            ctx = browser.new_context(
                viewport={"width": width, "height": 900}, device_scale_factor=2
            )
            # Set the theme before the page's own scripts run.
            ctx.add_init_script(f"localStorage.setItem('td.theme', {theme!r})")
            page = ctx.new_page()
            page.goto(base_url, wait_until="networkidle")
            page.wait_for_selector("#hm-combined canvas", timeout=20000)
            page.wait_for_timeout(1500)  # let ECharts finish drawing
            path = out_dir / f"screenshot-{theme}.png"
            # Crop to the hero: top of the page through the end of the "Daily burn"
            # card (KPIs + the per-provider heat maps), so the README stays tidy.
            # Grow the viewport to the hero height first so the whole card is in
            # frame (a non-full-page clip is otherwise bound to the viewport).
            card = page.query_selector("section.card")
            box = card.bounding_box() if card else None
            if box:
                hero_h = int(box["y"] + box["height"] + 16)
                page.set_viewport_size({"width": width, "height": hero_h})
                page.wait_for_timeout(700)  # let ECharts re-layout to the new size
                page.screenshot(
                    path=str(path),
                    clip={"x": 0, "y": 0, "width": width, "height": hero_h},
                )
            else:
                page.screenshot(path=str(path), full_page=True)
            print(f"wrote {path.relative_to(REPO)}")
            ctx.close()
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render README screenshots (light + dark)."
    )
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--width", type=int, default=1160)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default=str(REPO / "docs"))
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="td-shot-"))
    cfg = Config(
        db_path=str(tmp / "demo.duckdb"),
        scan_roots={
            "claude": str(tmp / "none"),
            "codex": str(tmp / "none"),
        },  # no ingest
        pricing_path=str(REPO / "pricing.yaml"),
        timezone="America/New_York",
        ingest_interval_min=100000,
        port=args.port,
    )
    state = build_state(cfg)
    print(f"seeded {seed(state.db, state.pricing, days=args.days)} synthetic events")

    server = start_server(create_app(state), args.port)
    try:
        wait_until_ready(args.port)
        capture(f"http://127.0.0.1:{args.port}/", Path(args.out), args.width)
    finally:
        server.should_exit = True
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
