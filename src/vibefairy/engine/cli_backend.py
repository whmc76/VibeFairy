"""CLI backend selection — Claude Code CLI vs OpenAI Codex CLI.

Current backend is persisted to data/cli_backend (plain text) so it survives
daemon restarts without requiring a config file edit.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

_STATE_FILE = Path("data/cli_backend")


class CLIBackend(str, Enum):
    CLAUDE = "claude"
    CODEX  = "codex"

    @property
    def display_name(self) -> str:
        return {"claude": "Claude Code", "codex": "OpenAI Codex"}[self.value]


def load_backend() -> CLIBackend:
    """Load persisted backend choice; defaults to Claude if missing/invalid."""
    try:
        val = _STATE_FILE.read_text(encoding="utf-8").strip().lower()
        return CLIBackend(val)
    except Exception:
        return CLIBackend.CLAUDE


def save_backend(backend: CLIBackend) -> None:
    """Persist backend choice to disk."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(backend.value, encoding="utf-8")
