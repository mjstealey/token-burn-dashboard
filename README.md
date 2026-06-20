# Token Burn Dashboard

A self-hosted dashboard that turns the usage logs your AI coding tools already write
to disk into a **Tufte-style calendar heat map of daily token burn** — plus per-model,
per-project, per-session, and burn-rate views. It tracks **Claude Code** and **Codex**
out of the box (with a pluggable adapter interface for other tools), persists to a
lightweight local **DuckDB**, deploys via **docker compose**, and re-ingests
incrementally on a schedule and on every run.

The heat map renders as **per-provider panels plus a combined total** (e.g. Claude,
Codex, and Combined), and each panel can be **toggled on/off** — the selection is
remembered in your browser. Panels share a common date window and each is color-scaled
to its own range so a quiet provider's pattern stays visible next to a busy one.

The framing follows Nate B Jones's "token burn" idea: tokens are a proxy for
*delegated intelligence*, and this is a learning loop — not a leaderboard.

> **Privacy:** everything runs locally. Logs are read **read-only**; nothing is sent
> anywhere. Dollar figures are *notional* (token counts × published prices from
> `pricing.yaml`), not a bill.

---

## Quick start (docker compose)

```bash
cp .env.example .env          # optional — adjust TZ, interval, limits
docker compose up --build
# open http://localhost:8080
```

Compose mounts `~/.claude` and `~/.codex` **read-only** and stores the DuckDB file in
`./data`. Set the timezone so the heat map buckets by *your* day, not UTC:

```bash
TZ=America/Chicago docker compose up --build
```

## Quick start (local, no Docker)

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv run token-dashboard serve        # ingests on boot, serves on :8080
# or run a one-off ingest:
uv run token-dashboard ingest
uv run pytest                        # run the test suite
```

---

## What it reads

| Tool | Path (default) | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | ground truth; per-request token usage incl. cache tiers |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | per-turn usage; model joined from `turn_context` |

Other tools (Gemini, Cursor, Copilot, …) aren't present on most machines; adding one
is a single new file under `src/token_dashboard/ingest/` plus an entry in
`registry.py`. The canonical event shape is in `ingest/base.py`.

### Correctness notes (why the numbers are trustworthy)

- **Claude dedup:** one API turn is split across several JSONL lines that each repeat
  the *identical* usage payload. We collapse to one row per `requestId`, so totals
  aren't multiplied 2–10×.
- **Codex deltas:** `total_token_usage` is a running cumulative total — summing it
  squares the count. We sum the per-turn `last_token_usage` deltas instead.
- **Cache pricing:** cache reads (~0.1×) and cache writes (1.25×/2×) are billed at
  their own rates, never at the base input rate — on coding logs cache tokens
  dominate, so this is the difference between a believable burn number and one off by
  an order of magnitude.
- **Local timezone:** logs are UTC; the heat map buckets by the configured `TZ`.

---

## Configuration

Set via `.env` / environment (overrides), or an optional YAML file (`TD_CONFIG=./config.yaml`).
See `.env.example` and `config.example.yaml`.

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `America/New_York` | local day for heat-map bucketing |
| `TD_INGEST_INTERVAL_MIN` | `15` | scheduled re-ingest interval |
| `TD_DB_PATH` | `./data/token.duckdb` | DuckDB file |
| `TD_CLAUDE_ROOT` / `TD_CODEX_ROOT` | home dirs | scan roots |
| `TD_PRICING_PATH` | `./pricing.yaml` | editable rate table |
| `TD_LIMIT_5H_TOKENS` / `TD_LIMIT_WEEK_TOKENS` | unset | enable plan-utilization gauges |
| `HOST_PORT` (compose) | `8080` | host port |

### Pricing

`pricing.yaml` holds USD-per-million-token rates per provider/model, with `input`,
`cache_write_5m`, `cache_write_1h`, `cache_read`, and `output` tiers. **Re-verify
against the vendor pricing pages periodically** — model lineups change. Editing the
file and hitting **↻ refresh** (or restarting) recomputes new ingests; to recompute
historical rows, delete `./data/token.duckdb` and re-ingest.

---

## How updates happen

The server ingests once on startup (so the dashboard is current the moment you open
it) and then re-ingests every `TD_INGEST_INTERVAL_MIN` minutes via an in-process
scheduler — one process owns all DuckDB writes (its single-writer model). Ingestion
is incremental (byte-offset watermark for Claude; change-detected re-parse for Codex)
and idempotent (`ON CONFLICT` on a stable event id), so re-runs never double-count.
The **↻ refresh** button triggers an immediate ingest; the page also polls every 60s.

## API

`GET /` · `/api/summary` · `/api/heatmap?days=&metric=` · `/api/models` ·
`/api/projects` · `/api/sessions` · `/api/turns` · `/api/burn` · `/api/health` ·
`POST /api/ingest`.

`/api/heatmap` returns `{ metric, days, combined: [...], providers: { <provider>: [...] } }`
(plus `series` as a back-compat alias for `combined`); each daily entry carries
`cost`, `tokens`, and the `input`/`output`/`cache_read`/`cache_creation` breakdown.

---

## Offline use

The frontend loads ECharts from a CDN. For a fully offline deploy, download
`echarts.min.js` into `src/token_dashboard/static/` and point the `<script>` in
`templates/base.html` at `/static/echarts.min.js`.

## Layout

```
src/token_dashboard/
  config.py  db.py  pricing.py  metrics.py  scheduler.py  main.py  cli.py
  ingest/{base,registry,claude,codex}.py
  templates/{base,dashboard}.html   static/{app.js,styles.css}
pricing.yaml  docker-compose.yml  Dockerfile  entrypoint.sh  tests/
```
