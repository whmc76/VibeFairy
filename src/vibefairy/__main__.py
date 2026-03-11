"""Entry point: python -m vibefairy or vibefairy CLI."""

import asyncio
import argparse
import sys
import logging
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vibefairy",
        description="VibeFairy — Secure Autonomous AI Assistant Daemon",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to vibefairy.toml (default: ./vibefairy.toml)",
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

    review_parser = sub.add_parser(
        "plan-review",
        help="Run dual plan with configurable main/review models",
    )
    review_parser.add_argument(
        "prompt",
        nargs="?",
        help="User request (omit to read from stdin)",
    )
    review_parser.add_argument(
        "--project", "-p",
        help="Target project name (from config targets)",
    )
    review_parser.add_argument(
        "--dir", "-d",
        help="Working directory (overrides --project)",
    )

    return p


def _resolve_project_dir(cfg: object, project_name: str | None) -> str:
    """Return the working directory for the named project, or cwd as fallback."""
    import os
    if project_name:
        for t in cfg.targets:
            if t.name == project_name:
                return t.path
        print(f"[WARN] Project '{project_name}' not found in config — using cwd", file=sys.stderr)
    return os.getcwd()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Default command is "run"
    if args.command is None or args.command == "run":
        from vibefairy.daemon import run_daemon
        asyncio.run(run_daemon(config_path=args.config, env_path=args.env, log_level=args.log_level))

    elif args.command == "check":
        from vibefairy.config.loader import load_config
        from vibefairy.config.secrets import load_secrets
        try:
            env_path = args.env or Path(".env")
            cfg = load_config(config_path=args.config, env_path=env_path)
            load_secrets()
            print("[OK] Config and secrets loaded successfully.")
            print(f"  Targets: {[t.name for t in cfg.targets]}")
            print(f"  Telegram bot: configured")
            print(
                f"  Main model: provider={cfg.models.main.provider}, "
                f"model={cfg.models.main.model or '(provider default)'}"
            )
            if cfg.models.review.enabled:
                print(
                    f"  Review model: provider={cfg.models.review.provider}, "
                    f"model={cfg.models.review.model or '(provider default)'}"
                )
            else:
                print("  Review model: disabled")
        except Exception as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "plan-review":
        from vibefairy.config.loader import load_config
        from vibefairy.engine.dual_plan_orchestrator import DualPlanOrchestrator

        prompt = args.prompt or sys.stdin.read().strip()
        if not prompt:
            print("Error: no prompt provided (pass as argument or via stdin)", file=sys.stderr)
            sys.exit(1)

        cfg = load_config(config_path=args.config, env_path=args.env)
        working_dir = args.dir or _resolve_project_dir(cfg, getattr(args, "project", None))

        log_level = getattr(args, "log_level", None) or cfg.log_level
        logging.basicConfig(
            level=getattr(logging, log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        )

        orchestrator = DualPlanOrchestrator(cfg=cfg)
        result = asyncio.run(orchestrator.run(user_request=prompt, working_dir=working_dir))

        print("\n" + "=" * 60)
        print("Dual Plan Orchestration Complete")
        print("=" * 60)
        print(f"Session ID     : {result.session_id}")
        print(f"Review used    : {result.review_used}")
        print(f"Success        : {result.success}")
        print(f"Tokens used    : {result.execution_tokens}")
        print(f"Main provider  : {cfg.models.main.provider}")
        print(
            "Review provider: "
            f"{cfg.models.review.provider if cfg.models.review.enabled else '(disabled)'}"
        )
        if result.revised_plan != result.initial_plan:
            print("\n[Plan was revised by review feedback]")
        print("\n--- Execution Output ---")
        print(result.execution_output or "(no output)")

        sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()

