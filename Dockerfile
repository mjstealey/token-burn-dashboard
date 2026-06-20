FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --no-dev

# Runtime assets.
COPY pricing.yaml ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV TD_DB_PATH=/app/data/token.duckdb \
    TD_PORT=8080
EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
