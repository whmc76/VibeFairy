"""Tests for engine/worker.py — retry logic with typed exceptions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibefairy.engine.claude_session import (
    ClaudePermanentError,
    ClaudeTransientError,
    ClaudeTimeoutError,
)
from vibefairy.engine.resilience import is_transient_error


# ---------------------------------------------------------------------------
# Tests for _is_transient (Worker's outer retry guard)
# ---------------------------------------------------------------------------

class TestWorkerIsTransient:
    """Test that Worker._is_transient() correctly classifies error strings."""

    def _make_worker(self, extra_patterns=None):
        """Create a minimal Worker with a mock config."""
        from vibefairy.config.loader import DaemonConfig, RetryConfig
        from vibefairy.engine.worker import Worker

        cfg = DaemonConfig()
        cfg.retry = RetryConfig(
            transient_errors=extra_patterns or ["rate_limit", "timeout", "connection_error"]
        )

        worker = Worker.__new__(Worker)
        worker._cfg = cfg
        return worker

    def test_transient_rate_limit(self):
        worker = self._make_worker()
        assert worker._is_transient("rate_limit exceeded")

    def test_transient_builtin_429(self):
        worker = self._make_worker()
        assert worker._is_transient("429 too many requests")

    def test_transient_user_configured(self):
        worker = self._make_worker(extra_patterns=["my_custom_error"])
        assert worker._is_transient("my_custom_error occurred")

    def test_not_transient_permanent(self):
        worker = self._make_worker()
        assert not worker._is_transient("authentication failed")
        assert not worker._is_transient("invalid request body")

    def test_not_transient_prefix(self):
        """'permanent: ...' prefix should NOT be transient."""
        worker = self._make_worker()
        assert not worker._is_transient("permanent: authentication failed")

    def test_transient_claude_exception_str(self):
        """ClaudeTransientError str representation should be detected as transient."""
        worker = self._make_worker()
        err = ClaudeTransientError("rate_limit exceeded after 3 attempts")
        assert worker._is_transient(str(err))

    def test_not_transient_permanent_exception_prefix(self):
        """Worker wraps permanent errors with 'permanent:' prefix."""
        worker = self._make_worker()
        err = ClaudePermanentError("authentication failed")
        error_str = f"permanent: {err}"
        assert not worker._is_transient(error_str)


# ---------------------------------------------------------------------------
# Tests for resilience.is_transient_error with extra_patterns
# ---------------------------------------------------------------------------

class TestIsTransientWithExtraPatterns:
    def test_combines_builtin_and_extra(self):
        assert is_transient_error("rate_limit", extra_patterns=["custom"])
        assert is_transient_error("custom error", extra_patterns=["custom"])

    def test_extra_none_uses_builtin_only(self):
        assert is_transient_error("connection_error", extra_patterns=None)
        assert not is_transient_error("permanent failure", extra_patterns=None)


# ---------------------------------------------------------------------------
# Tests for exception error string propagation
# ---------------------------------------------------------------------------

class TestExceptionErrorStrings:
    """Verify that exception strings contain keywords for transient detection."""

    def test_transient_error_contains_rate_limit(self):
        e = ClaudeTransientError("rate_limit: quota exceeded")
        assert "rate_limit" in str(e)
        assert is_transient_error(str(e))

    def test_transient_timeout_error(self):
        e = ClaudeTimeoutError("timeout after 120s")
        assert "timeout" in str(e)
        assert is_transient_error(str(e))

    def test_permanent_error_not_transient(self):
        e = ClaudePermanentError("authentication failed: invalid key")
        assert not is_transient_error(str(e))

    def test_worker_permanent_prefix_not_transient(self):
        """Worker sets error='permanent: <msg>' for ClaudePermanentError."""
        e = ClaudePermanentError("invalid model")
        error_field = f"permanent: {e}"
        assert not is_transient_error(error_field)
