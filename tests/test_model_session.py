from __future__ import annotations

from pathlib import Path

from vibefairy.config.loader import ModelEndpointConfig, RetryConfig
from vibefairy.engine.claude_session import ClaudeSession
from vibefairy.engine.model_session import CliModelSession, build_model_session, normalize_provider


def test_normalize_provider_aliases():
    assert normalize_provider("Claude Code") == "claude_code"
    assert normalize_provider("claude-code") == "claude_code"
    assert normalize_provider("codex") == "codex"


def test_build_model_session_returns_claude_session_for_claude_code():
    session = build_model_session(
        ModelEndpointConfig(provider="claude code", model="claude-sonnet-4-6"),
        working_dir=".",
        retry_cfg=RetryConfig(),
        semaphore=None,
    )

    assert isinstance(session, ClaudeSession)


def test_codex_default_command_builder():
    working_dir = str(Path("C:/repo"))
    session = CliModelSession(
        cfg=ModelEndpointConfig(provider="codex", model="o4-mini", command="codex"),
        working_dir=working_dir,
        retry_cfg=RetryConfig(),
        semaphore=None,
    )

    readonly_cmd = session._build_command(allow_write=False)
    write_cmd = session._build_command(allow_write=True)

    assert readonly_cmd == [
        "codex",
        "exec",
        "-C",
        working_dir,
        "-s",
        "read-only",
        "--color",
        "never",
        "-m",
        "o4-mini",
        "-",
    ]
    assert "--full-auto" in write_cmd
    assert "workspace-write" in write_cmd


def test_custom_cli_template_substitutes_placeholders():
    working_dir = str(Path("D:/project"))
    session = CliModelSession(
        cfg=ModelEndpointConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            command="gemini-wrapper",
            readonly_command=["{command}", "{working_dir}", "{model}", "{provider}", "readonly"],
        ),
        working_dir=working_dir,
        retry_cfg=RetryConfig(),
        semaphore=None,
    )

    assert session._build_command(allow_write=False) == [
        "gemini-wrapper",
        working_dir,
        "gemini-2.5-pro",
        "gemini",
        "readonly",
    ]
