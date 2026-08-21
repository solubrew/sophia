# sophia/cli.py
"""Command-line entrypoint for Sophia."""
from __future__ import annotations

import argparse

import uvicorn

from sophia.config import load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sophia", description="OpenAI-compatible gateway over ladyol.")
    parser.add_argument("--host", help="override bind host")
    parser.add_argument("--port", type=int, help="override bind port")
    parser.add_argument("--log-level", help="uvicorn log level (debug, info, warning, error)")
    parser.add_argument("--config", help="path to sophia.yaml (overrides default)")
    args = parser.parse_args(argv)

    cfg = load_config(path=args.config) if args.config else load_config()
    host = args.host or cfg.host
    port = args.port or cfg.port
    log_level = args.log_level or cfg.log_level.lower()

    uvicorn.run(
        "sophia.server:app",
        host=host,
        port=port,
        log_level=log_level,
        factory=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())