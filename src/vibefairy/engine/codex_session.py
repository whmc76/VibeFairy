"""OpenAI Codex CLI session wrapper.

Auth: Reads OPENAI_API_KEY from environment (set via .env).
Requires the `codex` CLI: npm install -g @openai/codex

Approval modes:
  full-auto   — applies changes directly (used for write tasks)
  suggest     — proposes changes but does NOT apply them (used for read-only)

Token counts are not exposed by the Codex CLI, so token_count is always 0.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from vibefairy.engine.claude_session import SessionResult

logger = logging.getLogger(__name__)

_READONLY_PREFIX = (
    "[READ-ONLY MODE] Only read and analyze files. "
    "Do NOT create, modify, or delete any files. "
    "Provide your analysis as plain text output.\n\n"
)


class CodexSession:
    """Manages an OpenAI Codex CLI session for a given working directory."""

    @staticmethod
    async def check_auth_available() -> tuple[bool, str]:
        """Quick pre-flight: check if Codex is authenticated.

        Returns (available, reason). Called by Worker before creating a session.
        """
        import os

        # OPENAI_API_KEY in environment is sufficient
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key and api_key.startswith("sk-"):
            return True, "ok"

        # Try codex login status
        try:
            proc = await asyncio.create_subprocess_exec(
                "codex", "login", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="replace").lower()
            if proc.returncode == 0 and (
                "logged in" in output or "authenticated" in output
            ):
                return True, "ok"
            return False, "Codex 未登录。请使用 /login codex 进行认证，或在 .env 中设置 OPENAI_API_KEY。"
        except FileNotFoundError:
            return False, "codex CLI 未找到且 OPENAI_API_KEY 未设置。\n安装: npm install -g @openai/codex 或设置 OPENAI_API_KEY。"
        except asyncio.TimeoutError:
            return True, "ok (auth check timed out, assuming available)"
        except Exception as e:
            return True, f"ok (auth check error: {e}, assuming available)"

    def __init__(
        self,
        working_dir: str | Path,
        model: str = "codex-mini-latest",
        openai_api_key: str | None = None,
    ):
        self._working_dir = Path(working_dir)
        self._model = model
        self._api_key = openai_api_key

    async def run_readonly(self, prompt: str, timeout_secs: int = 120) -> SessionResult:
        """Run in read-only mode: suggest approval, prefixed read-only instruction."""
        return await self.run_streaming(
            prompt=_READONLY_PREFIX + prompt,
            allow_write=False,
            timeout_secs=timeout_secs,
        )

    async def run_write(self, prompt: str, timeout_secs: int = 300) -> SessionResult:
        """Run with write permissions (full-auto). Caller must have verified approval gate."""
        return await self.run_streaming(
            prompt=prompt,
            allow_write=True,
            timeout_secs=timeout_secs,
        )

    async def run_streaming(
        self,
        prompt: str,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        allow_write: bool = True,
        timeout_secs: int = 300,
    ) -> SessionResult:
        """Run with optional streaming output callback (line-by-line from stdout).

        Note: Codex CLI does not support session continuation — each call is independent.
        """
        approval_mode = "full-auto" if allow_write else "suggest"
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        start = time.monotonic()
        output_lines: list[str] = []
        exit_code = 0

        env = os.environ.copy()
        if self._api_key:
            env["OPENAI_API_KEY"] = self._api_key

        cmd = [
            "codex",
            "--model", self._model,
            "--approval-mode", approval_mode,
            "--quiet",
            prompt,
        ]

        try:
            async with asyncio.timeout(timeout_secs):
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(self._working_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                async for line in proc.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    output_lines.append(text)
                    if on_chunk is not None:
                        await on_chunk(text + "\n")
                await proc.wait()
                exit_code = proc.returncode or 0

        except asyncio.TimeoutError:
            logger.warning("Codex session timed out after %ds", timeout_secs)
            return SessionResult(
                output="[TIMEOUT] Codex session exceeded time limit.",
                token_count=0,
                duration_secs=time.monotonic() - start,
                exit_code=124,
                prompt_hash=prompt_hash,
            )
        except FileNotFoundError:
            logger.error("codex CLI not found — install with: npm install -g @openai/codex")
            return SessionResult(
                output="[ERROR] codex CLI not found.\n安装命令: npm install -g @openai/codex",
                token_count=0,
                duration_secs=time.monotonic() - start,
                exit_code=127,
                prompt_hash=prompt_hash,
            )
        except Exception as e:
            logger.error("Codex session error: %s", e)
            return SessionResult(
                output=f"[ERROR] {e}",
                token_count=0,
                duration_secs=time.monotonic() - start,
                exit_code=1,
                prompt_hash=prompt_hash,
            )

        return SessionResult(
            output="\n".join(output_lines),
            token_count=0,  # Codex CLI does not expose token counts
            duration_secs=time.monotonic() - start,
            exit_code=exit_code,
            prompt_hash=prompt_hash,
        )
