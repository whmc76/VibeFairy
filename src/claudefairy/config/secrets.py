"""Secrets management — env vars ONLY, never persisted to disk.

All secrets are read from environment variables (loaded via python-dotenv
before this module is imported). If a required secret is missing, load_secrets()
raises SecretsError and the daemon refuses to start.

Auth strategy:
- Claude Code SDK uses the locally logged-in `claude` CLI session.
  ANTHROPIC_API_KEY is NOT required — `claude login` handles authentication.
- Only Telegram bot token + allowed chat IDs are mandatory.
- GitHub token is optional (increases API rate limits for Scout).
"""

import os
from dataclasses import dataclass


class SecretsError(RuntimeError):
    """Raised when a required secret is missing or invalid."""


@dataclass(frozen=True)
class Secrets:
    telegram_bot_token: str
    allowed_chat_ids: frozenset[str]
    # Optional: only needed if bypassing the local `claude` CLI auth
    anthropic_api_key: str | None
    github_token: str | None


def load_secrets() -> Secrets:
    """Load secrets from environment variables.

    Required:
      TELEGRAM_BOT_TOKEN       — from @BotFather
      TELEGRAM_ALLOWED_CHAT_IDS — comma-separated chat IDs

    Optional (Claude Code SDK uses local `claude login` by default):
      ANTHROPIC_API_KEY        — only needed to override CLI auth
      GITHUB_TOKEN             — increases GitHub API rate limits for Scout

    Raises SecretsError if required secrets are missing.
    """
    missing: list[str] = []

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        missing.append("TELEGRAM_BOT_TOKEN")

    raw_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw_ids:
        missing.append("TELEGRAM_ALLOWED_CHAT_IDS")
    allowed_ids: frozenset[str] = frozenset(
        cid.strip() for cid in raw_ids.split(",") if cid.strip()
    )

    if missing:
        raise SecretsError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the real values."
        )

    # Optional secrets — warn but don't block startup
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    if anthropic_key and anthropic_key.startswith("sk-ant-your"):
        anthropic_key = None  # ignore placeholder values

    github_token = os.environ.get("GITHUB_TOKEN", "").strip() or None

    return Secrets(
        telegram_bot_token=token,
        allowed_chat_ids=allowed_ids,
        anthropic_api_key=anthropic_key,
        github_token=github_token,
    )
