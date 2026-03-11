"""韧性原语 — 统一瞬态检测 + 全局信号量。

供 ClaudeSession、Worker、以及各 agent 统一使用，避免各处自定义判定逻辑。
"""

from __future__ import annotations

import asyncio

# 内置默认匹配模式（always active，不依赖用户配置）
_BUILTIN_PATTERNS = [
    "rate_limit", "rate limit", "ratelimit",
    "429", "too many requests",
    "overloaded", "overload",
    "timeout", "timed out",
    "connection_error", "connection error",
    "connection reset", "connection refused",
    "temporarily unavailable",
    "503", "service unavailable",
    "502", "bad gateway",
    "resourceexhausted", "capacity",
]


def is_transient_error(error_text: str, extra_patterns: list[str] | None = None) -> bool:
    """判断错误文本是否属于可重试的瞬态错误。

    合并内置 patterns + 用户配置的 transient_errors 做判定。
    """
    lower = error_text.lower()
    patterns = _BUILTIN_PATTERNS + (extra_patterns or [])
    return any(p.lower() in lower for p in patterns)


# 全局 Claude API 信号量（限制同时活跃的 Claude session 数量）
_claude_semaphore: asyncio.Semaphore | None = None


def init_claude_semaphore(max_concurrent: int = 2) -> asyncio.Semaphore:
    """初始化全局信号量。应在 daemon 启动时调用一次。"""
    global _claude_semaphore
    _claude_semaphore = asyncio.Semaphore(max_concurrent)
    return _claude_semaphore


def get_claude_semaphore() -> asyncio.Semaphore:
    """获取全局信号量。未初始化时返回 fallback（每次新建，仅限单进程保护）。"""
    if _claude_semaphore is None:
        return asyncio.Semaphore(2)  # fallback：仅限未调用 init 的场景
    return _claude_semaphore
