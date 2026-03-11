"""Claude Code SDK session wrapper.

Auth: Uses the locally logged-in `claude` CLI session (via `claude login`).
No ANTHROPIC_API_KEY needed — the SDK spawns the `claude` subprocess which
carries its own stored credentials.

Read-only enforcement: uses `allowed_tools` to restrict Claude to safe
read-only tools. Write mode lifts this restriction after approval gate.

Failure contract:
- Success → returns SessionResult (exit_code=0)
- Failure → raises ClaudeTransientError / ClaudePermanentError / ClaudeTimeoutError
  Callers must NOT inspect exit_code for error detection; catch exceptions instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

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

# Sentinel for "use global semaphore" (distinct from None = "no semaphore")
_UNSET = object()


class ClaudeTransientError(Exception):
    """可重试的瞬态错误（rate limit / 5xx / 连接中断）。内层重试耗尽后 raise。"""


class ClaudePermanentError(Exception):
    """不可重试的永久错误（认证失败、无效请求等）。"""


class ClaudeTimeoutError(Exception):
    """超时——总时间预算耗尽。"""


class _NullSemaphore:
    """No-op async context manager，供调用方传 semaphore=None 时跳过并发控制。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@dataclass
class SessionResult:
    output: str
    token_count: int
    duration_secs: float
    exit_code: int
    prompt_hash: str


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
        retry_max: int = 3,
        retry_base: float = 5.0,
        retry_cap: float = 60.0,
        semaphore=_UNSET,
    ):
        self._working_dir = Path(working_dir)
        self._model = model
        self._api_key = anthropic_api_key
        self._retry_max = retry_max
        self._retry_base = retry_base
        self._retry_cap = retry_cap

        # _UNSET → 使用全局信号量；None → 不用信号量（调用方已持有）
        if semaphore is _UNSET:
            from vibefairy.engine.resilience import get_claude_semaphore
            self._semaphore = get_claude_semaphore()
        elif semaphore is None:
            self._semaphore = _NullSemaphore()
        else:
            self._semaphore = semaphore

    async def run_readonly(self, prompt: str, timeout_secs: int = 120) -> SessionResult:
        """Run in read-only mode: only file-read and web-read tools allowed."""
        return await self._run(prompt=prompt, allow_write=False, timeout_secs=timeout_secs)

    async def run_write(self, prompt: str, timeout_secs: int = 300) -> SessionResult:
        """Run with write permissions. Caller must have verified approval gate."""
        return await self._run(prompt=prompt, allow_write=True, timeout_secs=timeout_secs)

    async def _run(
        self,
        prompt: str,
        allow_write: bool,
        timeout_secs: int,
    ) -> SessionResult:
        """Execute a Claude session.

        Returns SessionResult on success.
        Raises ClaudeTransientError / ClaudePermanentError / ClaudeTimeoutError on failure.
        """
        try:
            from claude_code_sdk import query, ClaudeCodeOptions
        except ImportError:
            logger.warning("claude-code-sdk not installed — using stub")
            return await self._stub_run(prompt, allow_write)

        from vibefairy.engine.resilience import is_transient_error

        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        start = time.monotonic()
        deadline = start + timeout_secs

        options = ClaudeCodeOptions(
            cwd=str(self._working_dir),
            allowed_tools=None if allow_write else _READONLY_TOOLS,
            # Never pass --dangerously-skip-permissions automatically.
        )

        for attempt in range(self._retry_max + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClaudeTimeoutError(f"total deadline exceeded ({timeout_secs}s)")

            output_parts: list[str] = []
            total_tokens = 0

            try:
                async with self._semaphore:
                    async with asyncio.timeout(remaining):
                        async for message in query(prompt=prompt, options=options):
                            msg_type = type(message).__name__
                            if msg_type == "AssistantMessage":
                                for block in message.content:
                                    if type(block).__name__ == "TextBlock":
                                        output_parts.append(block.text)
                            elif msg_type == "ResultMessage":
                                if hasattr(message, "usage") and message.usage:
                                    total_tokens = getattr(message.usage, "total_tokens", 0)

                return SessionResult(
                    output="\n".join(output_parts),
                    token_count=total_tokens,
                    duration_secs=time.monotonic() - start,
                    exit_code=0,
                    prompt_hash=prompt_hash,
                )

            except asyncio.TimeoutError:
                raise ClaudeTimeoutError(f"timeout after {timeout_secs}s")

            except Exception as e:
                if is_transient_error(str(e)) and attempt < self._retry_max:
                    backoff = min(self._retry_base * (2 ** attempt), self._retry_cap)
                    logger.warning(
                        "Claude transient error (attempt %d/%d), retry in %.0fs: %s",
                        attempt + 1, self._retry_max, backoff, e,
                    )
                    await asyncio.sleep(backoff)
                    continue
                if is_transient_error(str(e)):
                    raise ClaudeTransientError(str(e)) from e
                raise ClaudePermanentError(str(e)) from e

        # Should not reach here (loop always returns or raises)
        raise ClaudeTransientError("retry loop exhausted unexpectedly")

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

