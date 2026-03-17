"""Session manager — multi-session lifecycle and message routing.

Each LiveSession wraps a backend (Claude / Codex) for a specific working
directory. The SessionManager tracks which session is active for each chat_id
and routes messages accordingly.

Thread/concurrency model:
- One asyncio.Lock per LiveSession prevents interleaved concurrent messages.
- If a session is busy, the caller receives an immediate "still thinking" reply.
- SessionManager state is in-memory; DB is the source of truth for persistence.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

import aiosqlite

from vibefairy.engine.claude_session import SessionResult
from vibefairy.memory import repo
from vibefairy.memory.models import Session, SessionMessage

if TYPE_CHECKING:
    from vibefairy.config.secrets import Secrets

logger = logging.getLogger(__name__)


class LiveSession:
    """In-memory wrapper for an active AI backend session.

    Holds the working state (backend, model, working_dir, claude_session_id)
    and serialises concurrent messages via an asyncio.Lock.
    """

    def __init__(
        self,
        db_id: int,
        name: str,
        chat_id: str,
        working_dir: str,
        backend: str = "claude",
        model: str | None = None,
        claude_session_id: str | None = None,
    ):
        self.db_id = db_id
        self.name = name
        self.chat_id = chat_id
        self.working_dir = working_dir
        self.backend = backend
        self.model = model
        self.claude_session_id = claude_session_id   # for Claude context continuation
        self._lock = asyncio.Lock()

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    async def run(
        self,
        prompt: str,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        secrets: "Secrets | None" = None,
    ) -> SessionResult:
        """Send a message and stream the response.

        Acquires the per-session lock so concurrent callers are blocked
        (not queued — callers should check is_busy first).
        """
        async with self._lock:
            if self.backend == "codex":
                from vibefairy.engine.codex_session import CodexSession
                sess = CodexSession(
                    working_dir=self.working_dir,
                    model=self.model or "codex-mini-latest",
                    openai_api_key=secrets.openai_api_key if secrets else None,
                )
                result = await sess.run_streaming(
                    prompt=prompt,
                    on_chunk=on_chunk,
                    allow_write=True,
                )
            else:
                from vibefairy.engine.claude_session import ClaudeSession
                sess = ClaudeSession(
                    working_dir=self.working_dir,
                    model=self.model or "claude-sonnet-4-6",
                )
                result = await sess.run_streaming(
                    prompt=prompt,
                    on_chunk=on_chunk,
                    session_id=self.claude_session_id,
                    allow_write=True,
                )
                if result.new_session_id:
                    self.claude_session_id = result.new_session_id

            return result


class SessionManager:
    """Manages multiple LiveSession instances across chat_ids.

    Lifecycle:
        manager = SessionManager(db, secrets)
        await manager.initialize()   # loads active sessions from DB
        ...
        session = await manager.create_session(...)
        result  = await manager.send_message(session.name, prompt, on_chunk)
    """

    def __init__(self, db: aiosqlite.Connection, secrets: "Secrets | None" = None):
        self._db = db
        self._secrets = secrets
        self._sessions: dict[str, LiveSession] = {}  # name -> LiveSession
        self._active: dict[str, str] = {}            # chat_id -> session_name

    async def initialize(self) -> None:
        """Load active sessions from DB and rebuild in-memory state."""
        sessions = await repo.list_all_active_sessions(self._db)
        for s in sessions:
            live = LiveSession(
                db_id=s.id,
                name=s.name,
                chat_id=s.chat_id,
                working_dir=s.working_dir,
                backend=s.backend,
                model=s.model,
                claude_session_id=s.session_id,
            )
            self._sessions[s.name] = live

        # Set the most recently-updated session as active per chat_id
        chat_latest: dict[str, Session] = {}
        for s in sessions:
            key = s.chat_id
            if key not in chat_latest:
                chat_latest[key] = s
            else:
                prev = chat_latest[key]
                if s.updated_at and (prev.updated_at is None or s.updated_at > prev.updated_at):
                    chat_latest[key] = s

        for chat_id, s in chat_latest.items():
            self._active[chat_id] = s.name

        logger.info(
            "SessionManager initialized: %d sessions, %d active",
            len(self._sessions), len(self._active),
        )

    async def create_session(
        self,
        name: str,
        chat_id: str,
        working_dir: str,
        backend: str = "claude",
        model: str | None = None,
    ) -> LiveSession:
        """Create a new session, persist to DB, and set it as active."""
        if not Path(working_dir).is_dir():
            raise ValueError(f"Working directory does not exist: {working_dir}")

        db_session = Session(
            id=None,
            name=name,
            chat_id=chat_id,
            working_dir=working_dir,
            backend=backend,
            model=model,
            status="active",
        )
        db_id = await repo.create_session(self._db, db_session)

        live = LiveSession(
            db_id=db_id,
            name=name,
            chat_id=chat_id,
            working_dir=working_dir,
            backend=backend,
            model=model,
        )
        self._sessions[name] = live
        self._active[chat_id] = name
        logger.info("Created session '%s' for chat %s (backend=%s)", name, chat_id, backend)
        return live

    def get_active(self, chat_id: str) -> LiveSession | None:
        """Return the active session for a chat, or None if none."""
        name = self._active.get(chat_id)
        return self._sessions.get(name) if name else None

    def get_session(self, name: str) -> LiveSession | None:
        return self._sessions.get(name)

    def list_sessions(self, chat_id: str) -> list[LiveSession]:
        return [s for s in self._sessions.values() if s.chat_id == chat_id]

    async def switch_active(self, chat_id: str, session_name: str) -> bool:
        """Switch the active session for a chat. Returns False if session not found."""
        session = self._sessions.get(session_name)
        if session is None or session.chat_id != chat_id:
            return False
        self._active[chat_id] = session_name
        # Touch updated_at so next initialize() picks this as the most recent
        await repo.update_session(self._db, session.db_id)
        return True

    async def close_session(self, session_name: str) -> bool:
        """Close a session: mark as closed in DB and remove from memory."""
        session = self._sessions.get(session_name)
        if session is None:
            return False
        await repo.close_session(self._db, session.db_id)
        del self._sessions[session_name]
        # Remove from active tracking
        for chat_id, name in list(self._active.items()):
            if name == session_name:
                del self._active[chat_id]
        logger.info("Closed session '%s'", session_name)
        return True

    async def send_message(
        self,
        session_name: str,
        prompt: str,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> SessionResult:
        """Send a message to a named session and stream the response."""
        session = self._sessions.get(session_name)
        if session is None:
            raise ValueError(f"Session '{session_name}' not found")

        result = await session.run(
            prompt=prompt,
            on_chunk=on_chunk,
            secrets=self._secrets,
        )

        # Persist updated session_id for context continuation
        if session.claude_session_id:
            await repo.update_session(self._db, session.db_id, session_id=session.claude_session_id)

        return result
