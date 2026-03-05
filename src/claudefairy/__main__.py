"""Entry point: python -m claudefairy or claudefairy CLI."""

import asyncio
import argparse
import sys
import logging
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claudefairy",
        description="ClaudeFairy V2 — Secure Autonomous AI Assistant Daemon",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to claudefairy.toml (default: ./claudefairy.toml)",
    )
    p.add_argument(
        "--env",
        type=Path,
        default=None,
        help="Path to .env file (default: ./.env)",
    )
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default=None,
        help="Override log level from config",
    )
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="Start daemon (default)")
    sub.add_parser("check", help="Validate config and secrets, then exit")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Default command is "run"
    if args.command is None or args.command == "run":
        from claudefairy.daemon import run_daemon
        asyncio.run(run_daemon(config_path=args.config, env_path=args.env, log_level=args.log_level))
    elif args.command == "check":
        from claudefairy.config.loader import load_config
        from claudefairy.config.secrets import load_secrets
        try:
            env_path = args.env or Path(".env")
            cfg = load_config(config_path=args.config, env_path=env_path)
            secrets = load_secrets()
            print("[OK] Config and secrets loaded successfully.")
            print(f"  Targets: {[t.name for t in cfg.targets]}")
            print(f"  Telegram bot: configured")
            print(f"  Anthropic key: configured")
        except Exception as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
