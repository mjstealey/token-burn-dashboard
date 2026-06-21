"""Command-line entry point: `token-dashboard serve` | `token-dashboard ingest`."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from .config import load_config


def _is_duckdb_lock_error(exc: Exception) -> bool:
    return "Could not set lock" in str(exc)


def _print_lock_error(action: str) -> None:
    print(
        "Could not open the DuckDB file for writing. A dashboard server may "
        f"already be running; {action} instead.",
        file=sys.stderr,
    )


def _post_server(port: int, path: str) -> dict | None:
    """POST to a running server's API; return parsed JSON, or None if unreachable."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ):
        return None


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .main import create_app

    cfg = load_config()
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port or cfg.port, log_level="info")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .main import build_state

    cfg = load_config()
    if not args.direct:
        server_result = _post_server(cfg.port, "/api/ingest")
        if server_result is not None:
            print(json.dumps(server_result, indent=2, default=str))
            return 0

    try:
        state = build_state(cfg)
    except Exception as exc:
        if _is_duckdb_lock_error(exc):
            _print_lock_error("use the refresh button or POST /api/ingest")
            return 2
        raise

    try:
        summary = state.ingest()
        print(
            json.dumps(
                {"summary": summary, "pricing": state.last_pricing_sync},
                indent=2,
                default=str,
            )
        )
    finally:
        state.db.close()
    return 0


def _cmd_reprice(args: argparse.Namespace) -> int:
    from .main import build_state

    cfg = load_config()
    if not args.direct:
        path = "/api/reprice?force=true" if args.force else "/api/reprice"
        server_result = _post_server(cfg.port, path)
        if server_result is not None:
            print(json.dumps(server_result, indent=2, default=str))
            return 0

    try:
        state = build_state(cfg)
    except Exception as exc:
        if _is_duckdb_lock_error(exc):
            _print_lock_error("use POST /api/reprice")
            return 2
        raise

    try:
        pricing = state.sync_pricing(force=args.force)
        print(json.dumps({"pricing": pricing}, indent=2, default=str))
    finally:
        state.db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="token-dashboard")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the web dashboard")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_ingest = sub.add_parser("ingest", help="Run one ingest pass and exit")
    p_ingest.add_argument(
        "--direct",
        action="store_true",
        help="Open the DuckDB file directly instead of calling a running server",
    )
    p_ingest.set_defaults(func=_cmd_ingest)

    p_reprice = sub.add_parser(
        "reprice", help="Recompute stored costs from the current pricing table"
    )
    p_reprice.add_argument(
        "--force",
        action="store_true",
        help="Reprice all rows even if pricing.yaml has not changed",
    )
    p_reprice.add_argument(
        "--direct",
        action="store_true",
        help="Open the DuckDB file directly instead of calling a running server",
    )
    p_reprice.set_defaults(func=_cmd_reprice)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Default to serve.
        args = parser.parse_args(["serve", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
