"""Claude Code SDK session wrapper.

Auth: Uses the locally logged-in `claude` CLI session (via `claude login`).
No ANTHROPIC_API_KEY needed — the SDK spawns the `claude` subprocess which
carries its own stored credentials.

Read-only enforcement: uses `allowed_tools` to restrict Claude to safe
read-only tools. Write mode lifts this restriction after approval gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Tools allowed in read-only mode — no filesystem writes, no shell execution
_READONLY_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "WebSearch",
    "TodoRead",
]


@dataclass
class SessionResult:
    output: str
    token_count: int
    duration_secs: float
    exit_code: int
    prompt_hash: str
    new_session_id: str | None = None  # Claude Code SDK session_id for context continuation


class ClaudeSession:
    """Manages a Claude Code SDK session for a given working directory.

    Uses the local `claude` CLI authentication — no API key required.
    """

    def __init__(
        self,
        working_dir: str | Path,
        model: str = "claude-sonnet-4-6",
        # anthropic_api_key kept for backward compat but ignored by default
        anthropic_api_key: str | None = None,
    ):
        self._working_dir = Path(working_dir)
        self._model = model
        # API key is only used if explicitly set AND claude CLI is unavailable
        self._api_key = anthropic_api_key

    async def run_readonly(self, prompt: str, timeout_secs: int = 120) -> SessionResult:
        """Run in read-only mode: only file-read and web-read tools allowed."""
        return await self.run_streaming(prompt=prompt, allow_write=False, timeout_secs=timeout_secs)

    async def run_write(self, prompt: str, timeout_secs: int = 300) -> SessionResult:
        """Run with write permissions. Caller must have verified approval gate."""
        return await self.run_streaming(prompt=prompt, allow_write=True, timeout_secs=timeout_secs)

    async def run_streaming(
        self,
        prompt: str,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        session_id: str | None = None,
        allow_write: bool = True,
        timeout_secs: int = 300,
    ) -> SessionResult:
        """Run with optional streaming output callback.

        Args:
            prompt: The prompt to send to Claude.
            on_chunk: Async callback invoked for each text chunk as it arrives.
            session_id: Claude Code SDK session ID to resume a prior conversation.
            allow_write: If False, restricts tools to read-only set.
            timeout_secs: Timeout for the entire query.

        Returns:
            SessionResult with output, token_count, and new_session_id for
            subsequent context-continuation calls.
        """
        try:
            from claude_code_sdk import query, ClaudeCodeOptions
        except ImportError:
            logger.warning("claude-code-sdk not installed — using stub")
            return await self._stub_run(prompt, allow_write)

        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        start = time.monotonic()
        output_parts: list[str] = []
        total_tokens = 0
        new_session_id: str | None = None

        # Build ClaudeCodeOptions kwargs, probing for optional fields
        options_kwargs: dict = {
            "cwd": str(self._working_dir),
            "allowed_tools": None if allow_write else _READONLY_TOOLS,
            # Never pass --dangerously-skip-permissions automatically.
        }

        try:
            sdk_fields = {f.name for f in dataclasses.fields(ClaudeCodeOptions)}
        except Exception:
            sdk_fields = set()

        # Model selection (supported in newer SDK versions)
        if self._model and "model" in sdk_fields:
            options_kwargs["model"] = self._model

        # Session continuation — try several candidate field names
        if session_id:
            for candidate in ("resume", "session_id", "continue_session_id"):
                if candidate in sdk_fields:
                    options_kwargs[candidate] = session_id
                    break

        try:
            options = ClaudeCodeOptions(**options_kwargs)
        except TypeError:
            # Fall back to minimal options if unknown kwargs cause an error
            options = ClaudeCodeOptions(
                cwd=str(self._working_dir),
                allowed_tools=None if allow_write else _READONLY_TOOLS,
            )

        try:
            async with asyncio.timeout(timeout_secs):
                async for message in query(prompt=prompt, options=options):
                    msg_type = type(message).__name__
                    if msg_type == "AssistantMessage":
                        for block in message.content:
                            if type(block).__name__ == "TextBlock":
                                chunk = block.text
                                output_parts.append(chunk)
                                if on_chunk is not None:
                                    await on_chunk(chunk)
                    elif msg_type == "ResultMessage":
                        if hasattr(message, "usage") and message.usage:
                            total_tokens = getattr(message.usage, "total_tokens", 0)
                        if hasattr(message, "session_id") and message.session_id:
                            new_session_id = message.session_id

        except asyncio.TimeoutError:
            logger.warning("Claude session timed out after %ds", timeout_secs)
            return SessionResult(
                output="[TIMEOUT] Session exceeded time limit.",
                token_count=total_tokens,
                duration_secs=time.monotonic() - start,
                exit_code=124,
                prompt_hash=prompt_hash,
            )
        except Exception as e:
            logger.error("Claude session error: %s", e)
            return SessionResult(
                output=f"[ERROR] {e}",
                token_count=0,
                duration_secs=time.monotonic() - start,
                exit_code=1,
                prompt_hash=prompt_hash,
            )

        return SessionResult(
            output="\n".join(output_parts),
            token_count=total_tokens,
            duration_secs=time.monotonic() - start,
            exit_code=0,
            prompt_hash=prompt_hash,
            new_session_id=new_session_id,
        )

    @staticmethod
    async def check_auth_available() -> tuple[bool, str]:
        """Quick pre-flight: check if claude CLI is authenticated.

        Returns (available, reason). Called by Worker before creating a session.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="replace").lower()
            if proc.returncode == 0 and (
                "logged in" in output or "authenticated" in output or "@" in output
            ):
                return True, "ok"
            return False, "Claude 未登录。请使用 /login claude 进行认证。"
        except FileNotFoundError:
            return False, "claude CLI 未找到。请先安装 Claude Code。"
        except asyncio.TimeoutError:
            # Don't block execution on timeout — assume available
            return True, "ok (auth check timed out, assuming available)"
        except Exception as e:
            return True, f"ok (auth check error: {e}, assuming available)"

    async def _stub_run(self, prompt: str, allow_write: bool) -> SessionResult:
        """Fallback when SDK not installed."""
        await asyncio.sleep(0.1)
        mode = "WRITE" if allow_write else "READONLY"
        return SessionResult(
            output=(
                f"[STUB {mode}] claude-code-sdk not found.\n"
                f"Install with: pip install claude-code-sdk\n"
                f"Working dir: {self._working_dir}\n"
                f"Prompt preview: {prompt[:200]}"
            ),
            token_count=0,
            duration_secs=0.1,
            exit_code=0,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:16],
        )
