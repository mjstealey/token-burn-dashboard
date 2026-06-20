"""Command-line entry point: `token-dashboard serve` | `token-dashboard ingest`."""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .main import create_app

    cfg = load_config()
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port or cfg.port, log_level="info")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .main import build_state

    state = build_state()
    summary = state.ingest()
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="token-dashboard")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the web dashboard")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_ingest = sub.add_parser("ingest", help="Run one ingest pass and exit")
    p_ingest.set_defaults(func=_cmd_ingest)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Default to serve.
        args = parser.parse_args(["serve", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
