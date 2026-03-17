"""Auth manager — check and trigger OAuth login for Claude and Codex backends.

Responsibilities:
- Check auth status for each backend (with TTL cache)
- Trigger OAuth / device-code login flows
- Yield progress URLs/codes as async generator
- Prevent concurrent logins with asyncio.Lock
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator

from vibefairy.engine.cli_backend import CLIBackend

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")


@dataclass
class AuthStatus:
    backend: CLIBackend
    logged_in: bool
    method: str | None    # "claude.ai", "api_key", "ChatGPT", etc.
    email: str | None
    details: str          # human-readable summary


class AuthManager:
    """Encapsulates auth checks and OAuth triggers for Claude and Codex backends."""

    def __init__(self, login_timeout_secs: int = 300, status_cache_secs: int = 60):
        self._login_timeout = login_timeout_secs
        self._cache_ttl = status_cache_secs
        self._locks: dict[CLIBackend, asyncio.Lock] = {
            CLIBackend.CLAUDE: asyncio.Lock(),
            CLIBackend.CODEX: asyncio.Lock(),
        }
        # Cache: backend -> (status, expiry_ts)
        self._cache: dict[CLIBackend, tuple[AuthStatus, float]] = {}

    async def check_auth(self, backend: CLIBackend, force: bool = False) -> AuthStatus:
        """Check auth status for a backend. Results are cached for status_cache_secs."""
        if not force:
            cached = self._cache.get(backend)
            if cached and time.monotonic() < cached[1]:
                return cached[0]

        if backend == CLIBackend.CLAUDE:
            status = await self._check_claude()
        else:
            status = await self._check_codex()

        self._cache[backend] = (status, time.monotonic() + self._cache_ttl)
        return status

    def invalidate_cache(self, backend: CLIBackend) -> None:
        self._cache.pop(backend, None)

    async def start_login(self, backend: CLIBackend) -> AsyncGenerator[str, None]:
        """Trigger OAuth/device-code login. Yields progress messages including URLs.

        Yields strings — either informational messages or URLs the user should visit.
        """
        lock = self._locks[backend]
        if lock.locked():
            yield "登录流程已在进行中，请稍候..."
            return

        async with lock:
            if backend == CLIBackend.CLAUDE:
                async for msg in self._login_claude():
                    yield msg
            else:
                async for msg in self._login_codex():
                    yield msg

        # Invalidate cache after login attempt
        self.invalidate_cache(backend)

    # ---------------------------------------------------------------------- #
    # Claude
    # ---------------------------------------------------------------------- #

    async def _check_claude(self) -> AuthStatus:
        """Run `claude auth status` and parse output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode("utf-8", errors="replace").strip()
            exit_code = proc.returncode or 0
        except FileNotFoundError:
            return AuthStatus(
                backend=CLIBackend.CLAUDE,
                logged_in=False,
                method=None,
                email=None,
                details="claude CLI not found. Install Claude Code first.",
            )
        except asyncio.TimeoutError:
            return AuthStatus(
                backend=CLIBackend.CLAUDE,
                logged_in=False,
                method=None,
                email=None,
                details="Auth check timed out.",
            )
        except Exception as e:
            return AuthStatus(
                backend=CLIBackend.CLAUDE,
                logged_in=False,
                method=None,
                email=None,
                details=f"Auth check error: {e}",
            )

        # Parse output for login state
        logged_in = exit_code == 0 and (
            "logged in" in output.lower()
            or "authenticated" in output.lower()
            or "@" in output  # email present
        )

        # Try to extract email
        email = None
        for line in output.splitlines():
            if "@" in line and "." in line:
                parts = line.split()
                for part in parts:
                    if "@" in part and "." in part:
                        email = part.strip(".,;()")
                        break
            if email:
                break

        # Try to detect method
        method = None
        lower = output.lower()
        if "claude.ai" in lower or "web" in lower:
            method = "claude.ai"
        elif "api" in lower or "api_key" in lower:
            method = "api_key"

        if logged_in:
            details = f"已登录 Claude Code"
            if email:
                details += f" ({email})"
            if method:
                details += f" via {method}"
        else:
            details = f"未登录。使用 /login claude 进行认证。\n原始输出: {output[:200]}"

        return AuthStatus(
            backend=CLIBackend.CLAUDE,
            logged_in=logged_in,
            method=method,
            email=email,
            details=details,
        )

    async def _login_claude(self) -> AsyncGenerator[str, None]:
        """Trigger `claude auth login` with BROWSER=echo to capture OAuth URL."""
        yield "正在启动 Claude 登录流程..."

        import os
        env = os.environ.copy()
        # BROWSER=echo makes the CLI print the URL instead of opening a browser
        env["BROWSER"] = "echo"
        # Ensure non-interactive / headless
        env["CI"] = "1"

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "auth", "login",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError:
            yield "错误: claude CLI 未找到。请先安装 Claude Code。"
            return

        url_sent = False
        deadline = time.monotonic() + self._login_timeout

        try:
            while time.monotonic() < deadline:
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue

                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                logger.debug("claude login: %s", line)

                # Detect and yield URL
                urls = _URL_RE.findall(line)
                if urls and not url_sent:
                    yield f"请在浏览器中打开以下链接完成登录:\n{urls[0]}"
                    url_sent = True
                elif "success" in line.lower() or "logged in" in line.lower():
                    yield "Claude 登录成功！"
                elif "error" in line.lower() or "failed" in line.lower():
                    yield f"登录错误: {line}"
                elif line and not url_sent:
                    yield line

        except Exception as e:
            yield f"登录流程出错: {e}"
        finally:
            try:
                proc.kill()
            except Exception:
                pass

        if not url_sent:
            yield (
                "未能自动获取登录 URL。请手动运行: claude auth login\n"
                "或检查 claude CLI 版本是否支持 BROWSER=echo 模式。"
            )

    # ---------------------------------------------------------------------- #
    # Codex
    # ---------------------------------------------------------------------- #

    async def _check_codex(self) -> AuthStatus:
        """Check Codex auth status."""
        import os

        # First check if OPENAI_API_KEY is set
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key and api_key.startswith("sk-"):
            return AuthStatus(
                backend=CLIBackend.CODEX,
                logged_in=True,
                method="api_key",
                email=None,
                details=f"已通过 OPENAI_API_KEY 认证 (key: {api_key[:8]}...)",
            )

        # Try codex login status command
        try:
            proc = await asyncio.create_subprocess_exec(
                "codex", "login", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode("utf-8", errors="replace").strip()
            exit_code = proc.returncode or 0
        except FileNotFoundError:
            return AuthStatus(
                backend=CLIBackend.CODEX,
                logged_in=False,
                method=None,
                email=None,
                details="codex CLI not found. Install with: npm install -g @openai/codex\n或在 .env 中设置 OPENAI_API_KEY。",
            )
        except asyncio.TimeoutError:
            return AuthStatus(
                backend=CLIBackend.CODEX,
                logged_in=False,
                method=None,
                email=None,
                details="Auth check timed out.",
            )
        except Exception as e:
            return AuthStatus(
                backend=CLIBackend.CODEX,
                logged_in=False,
                method=None,
                email=None,
                details=f"Auth check error: {e}",
            )

        logged_in = exit_code == 0 and (
            "logged in" in output.lower()
            or "authenticated" in output.lower()
        )

        email = None
        for line in output.splitlines():
            if "@" in line:
                parts = line.split()
                for part in parts:
                    if "@" in part:
                        email = part.strip(".,;()")
                        break
            if email:
                break

        if logged_in:
            details = "已登录 Codex"
            if email:
                details += f" ({email})"
        else:
            details = f"未登录。使用 /login codex 进行认证，或在 .env 中设置 OPENAI_API_KEY。\n原始输出: {output[:200]}"

        return AuthStatus(
            backend=CLIBackend.CODEX,
            logged_in=logged_in,
            method="oauth" if logged_in else None,
            email=email,
            details=details,
        )

    async def _login_codex(self) -> AsyncGenerator[str, None]:
        """Trigger `codex login --device-auth` device code flow."""
        yield "正在启动 Codex 设备码登录流程..."

        try:
            proc = await asyncio.create_subprocess_exec(
                "codex", "login", "--device-auth",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            yield (
                "错误: codex CLI 未找到。\n"
                "安装命令: npm install -g @openai/codex\n"
                "或直接在 .env 中设置 OPENAI_API_KEY。"
            )
            return

        url_sent = False
        code_sent = False
        deadline = time.monotonic() + self._login_timeout

        try:
            while time.monotonic() < deadline:
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue

                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                logger.debug("codex login: %s", line)

                urls = _URL_RE.findall(line)
                if urls and not url_sent:
                    yield f"请在浏览器中打开以下链接:\n{urls[0]}"
                    url_sent = True

                # Device code is typically a short alphanumeric string
                # Look for patterns like "XXXX-XXXX" or "Enter code: XXXX"
                code_match = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", line)
                if code_match and not code_sent:
                    yield f"输入验证码: <code>{code_match.group(1)}</code>"
                    code_sent = True
                elif "success" in line.lower() or "logged in" in line.lower():
                    yield "Codex 登录成功！"
                elif "error" in line.lower() or "failed" in line.lower():
                    yield f"登录错误: {line}"
                elif line and not url_sent:
                    yield line

        except Exception as e:
            yield f"登录流程出错: {e}"
        finally:
            try:
                proc.kill()
            except Exception:
                pass

        if not url_sent:
            yield (
                "未能自动获取设备验证 URL。\n"
                "请手动运行: codex login --device-auth\n"
                "或在 .env 中直接设置 OPENAI_API_KEY。"
            )
