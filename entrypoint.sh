#!/usr/bin/env sh
set -e

# The server ingests once on startup (in its lifespan hook) so the dashboard is
# current the moment it's opened, then re-ingests on a schedule. exec so signals
# (SIGTERM from `docker compose down`) reach uvicorn directly.
exec uv run token-dashboard serve --host 0.0.0.0 --port "${TD_PORT:-8080}"
