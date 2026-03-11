"""ClaudeSDKClient wrapper for plan mode multi-turn sessions.

Provides generate_plan() → submit_revision() → close() lifecycle,
keeping the same session_id across turns so Codex feedback can be
submitted as an option-4 equivalent without starting a new session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PlanResult:
    plan_text: str
    session_id: str
    messages: list = field(default_factory=list)


class ClaudePlanMode:
    """Manages a Claude Code SDK plan-mode multi-turn session."""

    def __init__(self, working_dir: str, model: str | None = None) -> None:
        self._working_dir = working_dir
        self._model = model
        self._client = None
        self._session_id: str | None = None

    async def generate_plan(self, prompt: str) -> PlanResult:
        """Start a plan-mode session and return the generated plan."""
        try:
            from claude_code_sdk import ClaudeCodeOptions
            from claude_code_sdk._internal.client import ClaudeSDKClient
        except ImportError:
            logger.warning("claude-code-sdk not installed — returning stub plan")
            self._session_id = "stub-session"
            return PlanResult(
                plan_text="[STUB] claude-code-sdk not installed.",
                session_id=self._session_id,
            )

        options = ClaudeCodeOptions(
            cwd=self._working_dir,
            permission_mode="plan",
            **({"model": self._model} if self._model else {}),
        )

        self._client = ClaudeSDKClient(options)
        await self._client.connect(prompt=prompt)

        messages = await self._collect_messages()
        self._session_id = self._extract_session_id(messages)

        plan_text = self._find_plan_file() or self._extract_text(messages)
        return PlanResult(plan_text=plan_text, session_id=self._session_id, messages=messages)

    async def submit_revision(self, feedback: str) -> PlanResult:
        """Submit Codex feedback to the same session (option-4 equivalent)."""
        if self._client is None:
            raise RuntimeError("No active plan session — call generate_plan() first")

        await self._client.query(
            prompt=feedback,
            session_id=self._session_id or "default",
        )

        messages = await self._collect_messages()
        plan_text = self._find_plan_file() or self._extract_text(messages)
        return PlanResult(plan_text=plan_text, session_id=self._session_id or "", messages=messages)

    async def close(self) -> None:
        """Disconnect from the plan session."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _collect_messages(self) -> list:
        messages = []
        async for msg in self._client.receive_response():
            messages.append(msg)
        return messages

    def _extract_session_id(self, messages: list) -> str:
        for msg in reversed(messages):
            sid = getattr(msg, "session_id", None)
            if sid:
                return sid
        return "unknown"

    def _extract_text(self, messages: list) -> str:
        parts = []
        for msg in messages:
            if type(msg).__name__ == "AssistantMessage":
                for block in msg.content:
                    if type(block).__name__ == "TextBlock":
                        parts.append(block.text)
        return "\n".join(parts)

    def _find_plan_file(self) -> str | None:
        """Return contents of the most recently modified .md in .claude/plans/, or None."""
        plans_dir = Path(self._working_dir) / ".claude" / "plans"
        if not plans_dir.exists():
            return None
        md_files = sorted(
            plans_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if md_files:
            return md_files[0].read_text(encoding="utf-8")
        return None
