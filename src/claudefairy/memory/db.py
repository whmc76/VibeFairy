"""SQLite connection + migration.

Schema version is tracked in user_version PRAGMA.
Migrations run forward-only, never destructive.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_VERSION = 2

# Full schema DDL (all 6 tables)
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    title TEXT,
    description TEXT,
    relevance_score REAL,
    tags TEXT,
    target TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discovery_id INTEGER REFERENCES discoveries(id),
    target TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT,
    effort TEXT,
    priority TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    improvement_id INTEGER REFERENCES improvements(id),
    approved_by TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    prompt_snapshot TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    ttl_minutes INTEGER NOT NULL DEFAULT 30,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    improvement_id INTEGER REFERENCES improvements(id),
    approval_id INTEGER REFERENCES approvals(id),
    target TEXT NOT NULL,
    prompt_hash TEXT,
    output_summary TEXT,
    token_count INTEGER,
    duration_secs REAL,
    exit_code INTEGER,
    execution_mode TEXT NOT NULL DEFAULT 'readonly',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    source TEXT,
    detail TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS locks (
    target TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ttl_minutes INTEGER NOT NULL DEFAULT 60
);

CREATE INDEX IF NOT EXISTS idx_discoveries_status ON discoveries(status);
CREATE INDEX IF NOT EXISTS idx_discoveries_url ON discoveries(url);
CREATE INDEX IF NOT EXISTS idx_improvements_status ON improvements(status);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
"""

# Migration V2: tasks table
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source_message_id INTEGER,

    kind TEXT NOT NULL DEFAULT 'unknown',
    summary TEXT,
    plan TEXT,
    answer TEXT,
    target TEXT,
    priority TEXT,
    effort TEXT,
    decision_needed BOOLEAN NOT NULL DEFAULT FALSE,

    improvement_id INTEGER REFERENCES improvements(id),
    approval_id INTEGER REFERENCES approvals(id),
    run_id INTEGER REFERENCES runs(id),

    triage_retries INTEGER NOT NULL DEFAULT 0,
    execute_retries INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at TIMESTAMP,

    status TEXT NOT NULL DEFAULT 'received',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_id ON tasks(chat_id);
CREATE INDEX IF NOT EXISTS idx_tasks_decision ON tasks(decision_needed) WHERE decision_needed = TRUE;
"""


async def open_db(db_path: str | Path) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and run migrations."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row

    # Enable WAL mode for better concurrent reads
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")

    version = await _get_version(db)
    if version < CURRENT_VERSION:
        await _migrate(db, version)

    return db


async def _get_version(db: aiosqlite.Connection) -> int:
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
        return row[0] if row else 0


async def _migrate(db: aiosqlite.Connection, from_version: int) -> None:
    logger.info("Migrating DB from version %d to %d", from_version, CURRENT_VERSION)
    if from_version < 1:
        await db.executescript(_SCHEMA_V1)
        await db.execute("PRAGMA user_version = 1")
        await db.commit()
        logger.info("Migration to version 1 complete")
        from_version = 1
    if from_version < 2:
        await db.executescript(_SCHEMA_V2)
        await db.execute("PRAGMA user_version = 2")
        await db.commit()
        logger.info("Migration to version 2 complete")
