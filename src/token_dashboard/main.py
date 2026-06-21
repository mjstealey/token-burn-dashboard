"""FastAPI app: serves the dashboard, exposes JSON metrics, runs scheduled ingest."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import metrics
from .config import Config, load_config, validate_timezone
from .db import Database
from .ingest import ingest_all
from .ingest.base import reprice_if_needed
from .ingest.registry import adapters
from .pricing import Pricing
from .scheduler import start_scheduler

_PKG = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(_PKG / "templates"))


class AppState:
    def __init__(self, cfg: Config) -> None:
        validate_timezone(cfg.timezone)
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.pricing = Pricing.load(cfg.pricing_path)
        self.adapters = adapters()
        self.roots = {a.name: cfg.root_for(a.name) for a in self.adapters}
        self.roots = {k: v for k, v in self.roots.items() if v is not None}
        self.last_ingest: dict | None = None
        self.last_ingest_at: dt.datetime | None = None
        self.last_pricing_sync: dict | None = None

    def ingest(self) -> dict:
        self.sync_pricing()
        summary = ingest_all(self.db, self.pricing, self.adapters, self.roots)
        self.last_ingest = summary
        self.last_ingest_at = dt.datetime.now(dt.timezone.utc)
        return summary

    def reload_pricing(self) -> None:
        self.pricing = Pricing.load(self.cfg.pricing_path)

    def sync_pricing(self, force: bool = False) -> dict:
        self.reload_pricing()
        self.last_pricing_sync = reprice_if_needed(self.db, self.pricing, force=force)
        return self.last_pricing_sync


def build_state(cfg: Config | None = None) -> AppState:
    return AppState(cfg or load_config())


def create_app(state: AppState | None = None) -> FastAPI:
    state = state or build_state()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fresh on boot, then on a schedule.
        try:
            state.ingest()
        except Exception as exc:  # don't block serving if a log is malformed
            print(f"[startup] initial ingest failed: {exc}")
        scheduler = start_scheduler(state.ingest, state.cfg.ingest_interval_min)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            state.db.close()

    app = FastAPI(title="Token Burn Dashboard", version="0.1.0", lifespan=lifespan)
    app.state.app_state = state
    app.mount("/static", StaticFiles(directory=str(_PKG / "static")), name="static")

    tz = state.cfg.timezone

    @app.get("/")
    def index(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "tz": tz,
                "has_limits": bool(
                    state.cfg.limit_block_5h_tokens or state.cfg.limit_week_tokens
                ),
            },
        )

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "db_path": state.cfg.db_path,
            "timezone": tz,
            "last_ingest_at": state.last_ingest_at,
            "last_ingest": state.last_ingest,
            "last_pricing_sync": state.last_pricing_sync,
        }

    @app.get("/api/summary")
    def api_summary():
        s = metrics.summary(state.db, tz)
        s["last_ingest_at"] = state.last_ingest_at
        s["last_ingest"] = state.last_ingest
        s["last_pricing_sync"] = state.last_pricing_sync
        s["timezone"] = tz
        return s

    @app.get("/api/heatmap")
    def api_heatmap(days: int = 365, metric: str = "cost"):
        return metrics.heatmap(state.db, tz, days=days, metric=metric)

    @app.get("/api/models")
    def api_models():
        return {"models": metrics.by_model(state.db)}

    @app.get("/api/projects")
    def api_projects(limit: int = 30):
        return {"projects": metrics.by_project(state.db, limit=limit)}

    @app.get("/api/sessions")
    def api_sessions(limit: int = 25):
        return {"sessions": metrics.top_sessions(state.db, limit=limit)}

    @app.get("/api/turns")
    def api_turns(limit: int = 25):
        return {"turns": metrics.top_turns(state.db, limit=limit)}

    @app.get("/api/burn")
    def api_burn():
        return metrics.burn(
            state.db,
            tz,
            state.cfg.limit_block_5h_tokens,
            state.cfg.limit_week_tokens,
        )

    @app.post("/api/ingest")
    def api_ingest():
        summary = state.ingest()
        return {
            "summary": summary,
            "pricing": state.last_pricing_sync,
            "at": state.last_ingest_at,
        }

    @app.post("/api/reprice")
    def api_reprice(force: bool = False):
        return {"pricing": state.sync_pricing(force=force)}

    return app


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
