"""Tests for engine/claude_session.py — failure contract + inner retry."""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers: mock message types that match type(obj).__name__ checks in _run()
# ---------------------------------------------------------------------------

class TextBlock:
    def __init__(self, text: str):
        self.text = text


class AssistantMessage:
    def __init__(self, text: str):
        self.content = [TextBlock(text)]


class ResultMessage:
    def __init__(self, tokens: int = 100):
        self.usage = type("Usage", (), {"total_tokens": tokens})()


def make_mock_query(*messages):
    """Return an async generator factory that yields the given messages."""
    async def query(prompt, options):
        for msg in messages:
            yield msg
    return query


def make_failing_query(exc: Exception):
    """Return an async generator factory that raises the given exception."""
    async def query(prompt, options):
        raise exc
        yield  # make it an async generator
    return query


# ---------------------------------------------------------------------------
# Fixture: inject mock claude_code_sdk module
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_sdk(monkeypatch):
    """Inject a mock claude_code_sdk into sys.modules."""
    mock_module = MagicMock()
    mock_module.ClaudeCodeOptions = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "claude_code_sdk", mock_module)
    return mock_module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClaudeSessionSuccess:
    @pytest.mark.asyncio
    async def test_run_success_returns_session_result(self, mock_sdk):
        from vibefairy.engine.claude_session import ClaudeSession, SessionResult

        mock_sdk.query = make_mock_query(
            AssistantMessage("Hello world"),
            ResultMessage(tokens=42),
        )

        session = ClaudeSession(working_dir=".", semaphore=None)
        result = await session.run_readonly("test prompt")

        assert isinstance(result, SessionResult)
        assert result.exit_code == 0
        assert "Hello world" in result.output
        assert result.token_count == 42

    @pytest.mark.asyncio
    async def test_run_readonly_uses_allowed_tools(self, mock_sdk):
        from vibefairy.engine.claude_session import ClaudeSession

        captured_options = []

        async def capturing_query(prompt, options):
            captured_options.append(options)
            yield AssistantMessage("ok")

        mock_sdk.query = capturing_query
        session = ClaudeSession(working_dir=".", semaphore=None)
        await session.run_readonly("test")

        assert len(captured_options) == 1

    @pytest.mark.asyncio
    async def test_run_multiple_text_blocks_joined(self, mock_sdk):
        from vibefairy.engine.claude_session import ClaudeSession

        msg = AssistantMessage("first")
        msg.content.append(TextBlock("second"))
        mock_sdk.query = make_mock_query(msg)

        session = ClaudeSession(working_dir=".", semaphore=None)
        result = await session.run_readonly("test")

        assert "first" in result.output
        assert "second" in result.output


class TestClaudeSessionErrors:
    @pytest.mark.asyncio
    async def test_permanent_error_raises_immediately(self, mock_sdk):
        from vibefairy.engine.claude_session import ClaudeSession, ClaudePermanentError

        mock_sdk.query = make_failing_query(ValueError("invalid request"))

        session = ClaudeSession(working_dir=".", semaphore=None, retry_max=3)
        with pytest.raises(ClaudePermanentError):
            await session.run_readonly("test")

    @pytest.mark.asyncio
    async def test_permanent_error_no_retry(self, mock_sdk):
        """Permanent errors must NOT trigger retries."""
        from vibefairy.engine.claude_session import ClaudeSession, ClaudePermanentError

        call_count = 0

        async def counting_query(prompt, options):
            nonlocal call_count
            call_count += 1
            raise ValueError("authentication failed")
            yield

        mock_sdk.query = counting_query

        session = ClaudeSession(working_dir=".", semaphore=None, retry_max=3)
        with pytest.raises(ClaudePermanentError):
            await session.run_readonly("test")

        assert call_count == 1  # no retries for permanent errors

    @pytest.mark.asyncio
    async def test_transient_error_raises_after_retry_exhausted(self, mock_sdk):
        from vibefairy.engine.claude_session import ClaudeSession, ClaudeTransientError

        mock_sdk.query = make_failing_query(RuntimeError("rate_limit exceeded"))

        session = ClaudeSession(
            working_dir=".", semaphore=None,
            retry_max=2, retry_base=0.01, retry_cap=0.01,
        )
        with pytest.raises(ClaudeTransientError):
            await session.run_readonly("test")

    @pytest.mark.asyncio
    async def test_transient_error_retried_n_times(self, mock_sdk):
        """Transient error: should be attempted retry_max+1 times total."""
        from vibefairy.engine.claude_session import ClaudeSession, ClaudeTransientError

        call_count = 0

        async def counting_query(prompt, options):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("rate_limit")
            yield

        mock_sdk.query = counting_query

        session = ClaudeSession(
            working_dir=".", semaphore=None,
            retry_max=2, retry_base=0.01, retry_cap=0.01,
        )
        with pytest.raises(ClaudeTransientError):
            await session.run_readonly("test")

        assert call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_transient_retry_then_succeed(self, mock_sdk):
        """Transient failures followed by success should return SessionResult."""
        from vibefairy.engine.claude_session import ClaudeSession, SessionResult

        call_count = 0

        async def query_with_eventual_success(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("rate_limit")
            yield AssistantMessage("success after retries")

        mock_sdk.query = query_with_eventual_success

        session = ClaudeSession(
            working_dir=".", semaphore=None,
            retry_max=3, retry_base=0.01, retry_cap=0.01,
        )
        result = await session.run_readonly("test")

        assert isinstance(result, SessionResult)
        assert result.exit_code == 0
        assert "success after retries" in result.output
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_raises_claude_timeout_error(self, mock_sdk):
        from vibefairy.engine.claude_session import ClaudeSession, ClaudeTimeoutError

        async def slow_query(prompt, options):
            await asyncio.sleep(10)
            yield AssistantMessage("never reached")

        mock_sdk.query = slow_query

        session = ClaudeSession(working_dir=".", semaphore=None, retry_max=0)
        with pytest.raises(ClaudeTimeoutError):
            await session.run_readonly("test", timeout_secs=0.05)


class TestClaudeSessionStub:
    @pytest.mark.asyncio
    async def test_stub_used_when_sdk_missing(self, monkeypatch):
        """When claude_code_sdk has no 'query' attribute, stub returns a SessionResult."""
        import types
        from vibefairy.engine.claude_session import ClaudeSession, SessionResult

        # Replace the module with an empty one so `from claude_code_sdk import query` raises ImportError
        empty_module = types.ModuleType("claude_code_sdk")
        monkeypatch.setitem(sys.modules, "claude_code_sdk", empty_module)

        session = ClaudeSession(working_dir=".", semaphore=None)
        result = await session.run_readonly("test prompt")

        assert isinstance(result, SessionResult)
        assert result.exit_code == 0
        assert "STUB" in result.output
