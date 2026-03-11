"""Provider-agnostic model session factory.

Main model and review model share one config surface:
- `claude_code` uses the native Claude Code SDK wrapper
- `codex` uses a built-in CLI adapter
- `gemini` / `kimi` / `minimax` are supported through configurable CLI templates
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping
from pathlib import Path

from vibefairy.config.loader import ModelEndpointConfig, RetryConfig
from vibefairy.engine.claude_session import (
    ClaudePermanentError,
    ClaudeSession,
    ClaudeTimeoutError,
    ClaudeTransientError,
    SessionResult,
)

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"claude_code", "codex", "gemini", "kimi", "minimax"}
_UNSET = object()


def normalize_provider(provider: str) -> str:
    value = (provider or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "claude": "claude_code",
        "claudecode": "claude_code",
        "claude_code_sdk": "claude_code",
    }
    return aliases.get(value, value)


class _NullSemaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class CliModelSession:
    """Runs provider CLIs with stdin prompts and a ClaudeSession-like contract."""

    def __init__(
        self,
        cfg: ModelEndpointConfig,
        working_dir: str | Path,
        retry_cfg: RetryConfig,
        semaphore,
    ) -> None:
        self._cfg = cfg
        self._provider = normalize_provider(cfg.provider)
        self._working_dir = Path(working_dir)
        self._retry_max = retry_cfg.claude_inner_retries
        self._retry_base = retry_cfg.claude_inner_backoff_base
        self._retry_cap = retry_cfg.claude_inner_backoff_max
        if semaphore is _UNSET:
            from vibefairy.engine.resilience import get_claude_semaphore
            self._semaphore = get_claude_semaphore()
        elif semaphore is None:
            self._semaphore = _NullSemaphore()
        else:
            self._semaphore = semaphore

    async def run_readonly(self, prompt: str, timeout_secs: int | None = None) -> SessionResult:
        return await self._run(prompt=prompt, allow_write=False, timeout_secs=timeout_secs)

    async def run_write(self, prompt: str, timeout_secs: int | None = None) -> SessionResult:
        return await self._run(prompt=prompt, allow_write=True, timeout_secs=timeout_secs)

    async def _run(
        self,
        *,
        prompt: str,
        allow_write: bool,
        timeout_secs: int | None,
    ) -> SessionResult:
        from vibefairy.engine.resilience import is_transient_error

        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        timeout = timeout_secs or self._cfg.timeout_secs
        start = time.monotonic()
        deadline = start + timeout

        for attempt in range(self._retry_max + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClaudeTimeoutError(f"timeout after {timeout}s")

            cmd = self._build_command(allow_write=allow_write)
            logger.info(
                "Running %s provider command in %s",
                self._provider,
                self._working_dir,
            )

            try:
                async with self._semaphore:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(self._working_dir),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(input=prompt.encode("utf-8")),
                        timeout=remaining,
                    )
            except asyncio.TimeoutError as exc:
                raise ClaudeTimeoutError(f"timeout after {timeout}s") from exc
            except FileNotFoundError as exc:
                raise ClaudePermanentError(
                    f"{self._provider} command not found: {cmd[0]}"
                ) from exc
            except Exception as exc:
                text = str(exc)
                if is_transient_error(text) and attempt < self._retry_max:
                    await asyncio.sleep(min(self._retry_base * (2 ** attempt), self._retry_cap))
                    continue
                if is_transient_error(text):
                    raise ClaudeTransientError(text) from exc
                raise ClaudePermanentError(text) from exc

            output = stdout.decode(errors="replace").strip()
            error_text = stderr.decode(errors="replace").strip()
            if proc.returncode == 0:
                return SessionResult(
                    output=output,
                    token_count=0,
                    duration_secs=time.monotonic() - start,
                    exit_code=0,
                    prompt_hash=prompt_hash,
                )

            detail = error_text or output or f"{self._provider} exited {proc.returncode}"
            if is_transient_error(detail) and attempt < self._retry_max:
                await asyncio.sleep(min(self._retry_base * (2 ** attempt), self._retry_cap))
                continue
            if is_transient_error(detail):
                raise ClaudeTransientError(detail)
            raise ClaudePermanentError(f"{self._provider} exit {proc.returncode}: {detail[:400]}")

        raise ClaudeTransientError("retry loop exhausted unexpectedly")

    def _build_command(self, *, allow_write: bool) -> list[str]:
        template = self._cfg.write_command if allow_write else self._cfg.readonly_command
        if template:
            return [self._format_piece(piece) for piece in template]

        if self._provider == "codex":
            command = self._cfg.command or "codex"
            cmd = [
                command,
                "exec",
                "-C",
                str(self._working_dir),
                "-s",
                "workspace-write" if allow_write else "read-only",
                "--color",
                "never",
            ]
            if allow_write:
                cmd.append("--full-auto")
            if self._cfg.model is not None:
                cmd += ["-m", self._cfg.model]
            cmd.append("-")
            return cmd

        command = self._cfg.command or self._provider
        raise ClaudePermanentError(
            f"provider '{self._provider}' requires "
            f"{'write_command' if allow_write else 'readonly_command'} in config "
            f"(command={command})"
        )

    def _format_piece(self, piece: str) -> str:
        values: Mapping[str, str] = {
            "command": self._cfg.command or self._provider,
            "model": self._cfg.model or "",
            "provider": self._provider,
            "working_dir": str(self._working_dir),
        }
        return piece.format(**values)


def build_model_session(
    endpoint: ModelEndpointConfig,
    *,
    working_dir: str | Path,
    retry_cfg: RetryConfig,
    semaphore=_UNSET,
    model_override: str | None = None,
):
    provider = normalize_provider(endpoint.provider)
    if provider not in SUPPORTED_PROVIDERS:
        raise ClaudePermanentError(
            f"unsupported provider '{endpoint.provider}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )

    if provider == "claude_code":
        return ClaudeSession(
            working_dir=working_dir,
            model=model_override or endpoint.model or "claude-sonnet-4-6",
            retry_max=retry_cfg.claude_inner_retries,
            retry_base=retry_cfg.claude_inner_backoff_base,
            retry_cap=retry_cfg.claude_inner_backoff_max,
            semaphore=semaphore,
        )

    session_cfg = ModelEndpointConfig(
        enabled=endpoint.enabled,
        provider=provider,
        model=model_override or endpoint.model,
        timeout_secs=endpoint.timeout_secs,
        command=endpoint.command,
        readonly_command=list(endpoint.readonly_command),
        write_command=list(endpoint.write_command),
    )
    return CliModelSession(
        cfg=session_cfg,
        working_dir=working_dir,
        retry_cfg=retry_cfg,
        semaphore=semaphore,
    )

