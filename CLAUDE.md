# token-burn-dashboard

## What this is
Self-hosted dashboard that turns Claude Code / Codex usage logs into a Tufte-style
calendar heat map of daily token burn. Read-only, local-only. See README.md for the full picture.

## Stack
Python 3.11+ · FastAPI + uvicorn · DuckDB · APScheduler · Jinja2 templates + ECharts (CDN).
Package `token_dashboard` under `src/`; `uv` for envs/installs/running.

## Commands
- `uv run token-dashboard serve` — ingest on boot, serve on :8080
- `uv run token-dashboard ingest` — one ingest pass, exit (routes to a running server if up)
- `uv run token-dashboard reprice [--force]` — recompute stored costs from current `pricing.yaml`
- `uv run pytest` — test suite
- `docker compose up --build` — mounts `~/.claude` + `~/.codex` read-only, DB in `./data`

## Conventions
- Add a tool = one `Adapter` subclass in `src/token_dashboard/ingest/` + an entry in
  `ingest/registry.py`. Canonical event shape is `UsageEvent` in `ingest/base.py`;
  watermarking, dedup, cost, and insertion are shared there — adapters only `parse()`.
- Adapter `incremental = True` tails via stored byte offset; `False` re-parses the whole
  file on any change (use when a record depends on earlier context in the file — Codex).
- Config: `TD_*` env vars override optional YAML (`TD_CONFIG`). Don't hardcode paths/ports.
- Cost is materialized into `cost_usd` at ingest. `pricing.yaml`'s content hash lives in
  `app_metadata`; ingest/refresh reprices existing rows only when that hash changes.

## Gotchas
- Single-writer DB: one process owns all DuckDB writes, serialized by `_INGEST_LOCK` +
  `db.lock`. Don't open a second writer or move writes off the scheduler; the CLI
  `ingest`/`reprice` POST to a running server rather than opening a second connection.
- Claude repeats the same usage payload across sibling JSONL lines — collapse to one row
  per `requestId` (or stable `msg.id`; never line-level `uuid`), or totals inflate 2–10×.
- Codex `total_token_usage` is cumulative — sum per-turn `last_token_usage` deltas instead.
- Cache reads/writes bill at their own tiers, never the base input rate (`pricing.py`).
- Logs are UTC; the heat map buckets by the configured `TZ` (validated at startup, IANA
  name) — set it or days are wrong.
- Editing `pricing.yaml` reprices on the next ingest/refresh, gated by its content hash;
  use `reprice --force` to recompute when the file is unchanged.

## Don't
Send anything off-box, or treat dollar figures as a bill — they're notional (tokens × `pricing.yaml`).
