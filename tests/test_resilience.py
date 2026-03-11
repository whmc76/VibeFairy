"""Tests for engine/resilience.py — unified transient detection + global semaphore."""

import asyncio
import pytest

from vibefairy.engine.resilience import (
    get_claude_semaphore,
    init_claude_semaphore,
    is_transient_error,
)


class TestIsTransientError:
    def test_builtin_rate_limit_patterns(self):
        assert is_transient_error("rate_limit exceeded")
        assert is_transient_error("rate limit hit")
        assert is_transient_error("ratelimit")
        assert is_transient_error("HTTP 429 Too Many Requests")
        assert is_transient_error("too many requests")

    def test_builtin_overload_patterns(self):
        assert is_transient_error("Service overloaded")
        assert is_transient_error("overload detected")

    def test_builtin_timeout_patterns(self):
        assert is_transient_error("connection timeout")
        assert is_transient_error("request timed out")

    def test_builtin_connection_patterns(self):
        assert is_transient_error("connection_error")
        assert is_transient_error("connection error occurred")
        assert is_transient_error("connection reset by peer")
        assert is_transient_error("connection refused")

    def test_builtin_http_5xx_patterns(self):
        assert is_transient_error("503 Service Unavailable")
        assert is_transient_error("502 Bad Gateway")
        assert is_transient_error("temporarily unavailable")

    def test_builtin_grpc_patterns(self):
        assert is_transient_error("ResourceExhausted: quota exceeded")
        assert is_transient_error("capacity exceeded")

    def test_case_insensitive(self):
        assert is_transient_error("RATE_LIMIT")
        assert is_transient_error("Rate Limit")
        assert is_transient_error("Connection Reset")  # "connection reset" pattern matches

    def test_extra_patterns(self):
        assert is_transient_error("custom_transient", extra_patterns=["custom_transient"])
        assert is_transient_error("my service error", extra_patterns=["my service"])

    def test_extra_patterns_case_insensitive(self):
        assert is_transient_error("MY_ERROR", extra_patterns=["my_error"])

    def test_negative_authentication_error(self):
        assert not is_transient_error("authentication failed")
        assert not is_transient_error("invalid API key")
        assert not is_transient_error("permission denied")
        assert not is_transient_error("model not found")

    def test_negative_extra_patterns_not_present(self):
        assert not is_transient_error("permanent failure", extra_patterns=["transient_keyword"])

    def test_empty_string(self):
        assert not is_transient_error("")


class TestClaudeSemaphore:
    def setup_method(self):
        """Reset global semaphore state before each test."""
        import vibefairy.engine.resilience as r
        self._saved = r._claude_semaphore
        r._claude_semaphore = None

    def teardown_method(self):
        """Restore global semaphore state after each test."""
        import vibefairy.engine.resilience as r
        r._claude_semaphore = self._saved

    def test_init_returns_semaphore(self):
        sem = init_claude_semaphore(max_concurrent=3)
        assert isinstance(sem, asyncio.Semaphore)

    def test_get_after_init_returns_same_instance(self):
        sem = init_claude_semaphore(max_concurrent=2)
        got = get_claude_semaphore()
        assert got is sem

    def test_get_without_init_returns_fallback(self):
        # No init — should return a new fallback semaphore
        sem = get_claude_semaphore()
        assert isinstance(sem, asyncio.Semaphore)

    def test_init_idempotent_last_wins(self):
        sem1 = init_claude_semaphore(max_concurrent=1)
        sem2 = init_claude_semaphore(max_concurrent=4)
        assert get_claude_semaphore() is sem2
        assert sem1 is not sem2
