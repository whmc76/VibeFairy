"""CRUD repository — all DB access goes through here.

Uses aiosqlite directly (no ORM). All methods are async.
Callers should not write SQL outside this module.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from .models import Approval, Discovery, Event, Improvement, Lock, Run, Task

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dt(val: str | None) -> datetime | None:
    if val is None:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Discoveries
# --------------------------------------------------------------------------- #

async def upsert_discovery(db: aiosqlite.Connection, d: Discovery) -> int:
    """Insert or ignore (by URL). Returns the row id."""
    await db.execute(
        """
        INSERT INTO discoveries (source, url, title, description, relevance_score, tags, target, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            relevance_score = excluded.relevance_score,
            tags = excluded.tags,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            d.source, d.url, d.title, d.description,
            d.relevance_score, json.dumps(d.tags), d.target, d.status,
        ),
    )
    await db.commit()
    async with db.execute("SELECT id FROM discoveries WHERE url = ?", (d.url,)) as cur:
        row = await cur.fetchone()
        return row["id"]


async def update_discovery_status(db: aiosqlite.Connection, discovery_id: int, status: str) -> None:
    await db.execute(
        "UPDATE discoveries SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, discovery_id),
    )
    await db.commit()


async def get_discovery(db: aiosqlite.Connection, discovery_id: int) -> Discovery | None:
    async with db.execute("SELECT * FROM discoveries WHERE id = ?", (discovery_id,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_discovery(row)


async def list_discoveries(
    db: aiosqlite.Connection,
    status: str | None = None,
    limit: int = 50,
) -> list[Discovery]:
    if status:
        sql = "SELECT * FROM discoveries WHERE status = ? ORDER BY created_at DESC LIMIT ?"
        params = (status, limit)
    else:
        sql = "SELECT * FROM discoveries ORDER BY created_at DESC LIMIT ?"
        params = (limit,)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
        return [_row_to_discovery(r) for r in rows]


async def discovery_url_exists(db: aiosqlite.Connection, url: str) -> bool:
    async with db.execute("SELECT 1 FROM discoveries WHERE url = ?", (url,)) as cur:
        return await cur.fetchone() is not None


def _row_to_discovery(row: aiosqlite.Row) -> Discovery:
    tags_raw = row["tags"]
    try:
        tags = json.loads(tags_raw) if tags_raw else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    return Discovery(
        id=row["id"],
        source=row["source"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        relevance_score=row["relevance_score"],
        tags=tags,
        target=row["target"],
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


# --------------------------------------------------------------------------- #
# Improvements
# --------------------------------------------------------------------------- #

async def create_improvement(db: aiosqlite.Connection, imp: Improvement) -> int:
    async with db.execute(
        """
        INSERT INTO improvements (discovery_id, target, summary, detail, effort, priority, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (imp.discovery_id, imp.target, imp.summary, imp.detail, imp.effort, imp.priority, imp.status),
    ) as cur:
        row_id = cur.lastrowid
    await db.commit()
    return row_id


async def update_improvement_status(db: aiosqlite.Connection, imp_id: int, status: str) -> None:
    await db.execute("UPDATE improvements SET status = ? WHERE id = ?", (status, imp_id))
    await db.commit()


async def get_improvement(db: aiosqlite.Connection, imp_id: int) -> Improvement | None:
    async with db.execute("SELECT * FROM improvements WHERE id = ?", (imp_id,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_improvement(row)


async def list_improvements(
    db: aiosqlite.Connection,
    status: str | None = None,
    target: str | None = None,
    limit: int = 50,
) -> list[Improvement]:
    conditions: list[str] = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if target:
        conditions.append("target = ?")
        params.append(target)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    async with db.execute(
        f"SELECT * FROM improvements {where} ORDER BY created_at DESC LIMIT ?", params
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_improvement(r) for r in rows]


def _row_to_improvement(row: aiosqlite.Row) -> Improvement:
    return Improvement(
        id=row["id"],
        discovery_id=row["discovery_id"],
        target=row["target"],
        summary=row["summary"],
        detail=row["detail"],
        effort=row["effort"],
        priority=row["priority"],
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
    )


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #

async def create_approval(db: aiosqlite.Connection, appr: Approval) -> int:
    async with db.execute(
        """
        INSERT INTO approvals (improvement_id, approved_by, chat_id, prompt_snapshot,
                               execution_mode, ttl_minutes, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            appr.improvement_id,
            appr.approved_by,
            appr.chat_id,
            appr.prompt_snapshot,
            appr.execution_mode,
            appr.ttl_minutes,
            appr.expires_at.isoformat(),
        ),
    ) as cur:
        row_id = cur.lastrowid
    await db.commit()
    return row_id


async def get_approval(db: aiosqlite.Connection, approval_id: int) -> Approval | None:
    async with db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_approval(row)


async def is_approval_valid(
    db: aiosqlite.Connection,
    approval_id: int,
    target: str,
    improvement_id: int,
) -> tuple[bool, str]:
    """Returns (is_valid, reason)."""
    appr = await get_approval(db, approval_id)
    if appr is None:
        return False, "approval not found"
    now = _now()
    expires = appr.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        return False, "approval expired"
    if appr.improvement_id != improvement_id:
        return False, "approval belongs to different improvement"
    return True, "ok"


def _row_to_approval(row: aiosqlite.Row) -> Approval:
    expires = _parse_dt(row["expires_at"]) or datetime.now(tz=timezone.utc)
    return Approval(
        id=row["id"],
        improvement_id=row["improvement_id"],
        approved_by=row["approved_by"],
        chat_id=row["chat_id"],
        prompt_snapshot=row["prompt_snapshot"],
        execution_mode=row["execution_mode"],
        ttl_minutes=row["ttl_minutes"],
        expires_at=expires,
        created_at=_parse_dt(row["created_at"]),
    )


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

async def create_run(db: aiosqlite.Connection, run: Run) -> int:
    async with db.execute(
        """
        INSERT INTO runs (improvement_id, approval_id, target, prompt_hash,
                          execution_mode, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run.improvement_id, run.approval_id, run.target,
            run.prompt_hash, run.execution_mode, run.status,
        ),
    ) as cur:
        row_id = cur.lastrowid
    await db.commit()
    return row_id


async def update_run(db: aiosqlite.Connection, run_id: int, **kwargs) -> None:
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [run_id]
    await db.execute(f"UPDATE runs SET {set_clause} WHERE id = ?", values)
    await db.commit()


async def get_run(db: aiosqlite.Connection, run_id: int) -> Run | None:
    async with db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_run(row)


async def list_stuck_runs(db: aiosqlite.Connection) -> list[Run]:
    """Runs in 'executing' state — likely crashed."""
    async with db.execute(
        "SELECT * FROM runs WHERE status = 'executing' ORDER BY created_at",
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]


def _row_to_run(row: aiosqlite.Row) -> Run:
    return Run(
        id=row["id"],
        improvement_id=row["improvement_id"],
        approval_id=row["approval_id"],
        target=row["target"],
        prompt_hash=row["prompt_hash"],
        output_summary=row["output_summary"],
        token_count=row["token_count"],
        duration_secs=row["duration_secs"],
        exit_code=row["exit_code"],
        execution_mode=row["execution_mode"],
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
    )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

async def log_event(
    db: aiosqlite.Connection,
    event_type: str,
    source: str | None = None,
    detail: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO events (event_type, source, detail) VALUES (?, ?, ?)",
        (event_type, source, detail),
    )
    await db.commit()


async def list_events(
    db: aiosqlite.Connection,
    event_type: str | None = None,
    limit: int = 100,
) -> list[Event]:
    if event_type:
        sql = "SELECT * FROM events WHERE event_type = ? ORDER BY created_at DESC LIMIT ?"
        params = (event_type, limit)
    else:
        sql = "SELECT * FROM events ORDER BY created_at DESC LIMIT ?"
        params = (limit,)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
        return [
            Event(
                id=r["id"],
                event_type=r["event_type"],
                source=r["source"],
                detail=r["detail"],
                created_at=_parse_dt(r["created_at"]),
            )
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Locks
# --------------------------------------------------------------------------- #

async def acquire_lock(
    db: aiosqlite.Connection,
    target: str,
    holder: str,
    ttl_minutes: int = 60,
) -> bool:
    """Try to acquire a lock. Returns True on success, False if already locked."""
    # Clean expired locks first
    await db.execute(
        """
        DELETE FROM locks
        WHERE target = ? AND datetime(acquired_at, '+' || ttl_minutes || ' minutes') < CURRENT_TIMESTAMP
        """,
        (target,),
    )
    await db.commit()

    try:
        await db.execute(
            "INSERT INTO locks (target, holder, ttl_minutes) VALUES (?, ?, ?)",
            (target, holder, ttl_minutes),
        )
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # lock already held


async def release_lock(db: aiosqlite.Connection, target: str, holder: str) -> None:
    await db.execute(
        "DELETE FROM locks WHERE target = ? AND holder = ?",
        (target, holder),
    )
    await db.commit()


async def cleanup_expired_locks(db: aiosqlite.Connection) -> int:
    """Remove all expired locks. Returns count of removed locks."""
    async with db.execute(
        """
        DELETE FROM locks
        WHERE datetime(acquired_at, '+' || ttl_minutes || ' minutes') < CURRENT_TIMESTAMP
        """
    ) as cur:
        count = cur.rowcount
    await db.commit()
    return count


# --------------------------------------------------------------------------- #
# Daily budget tracking (in-memory via events table)
# --------------------------------------------------------------------------- #

async def get_today_token_count(db: aiosqlite.Connection) -> int:
    """Sum token_count from runs created today."""
    async with db.execute(
        "SELECT COALESCE(SUM(token_count), 0) FROM runs WHERE date(created_at) = date('now')"
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row else 0


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

async def create_task(db: aiosqlite.Connection, task: Task) -> int:
    async with db.execute(
        """
        INSERT INTO tasks (raw_message, chat_id, user_id, source_message_id,
                           kind, summary, plan, answer, target, priority, effort,
                           decision_needed, improvement_id, approval_id, run_id,
                           triage_retries, execute_retries, last_error, next_retry_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.raw_message, task.chat_id, task.user_id, task.source_message_id,
            task.kind, task.summary, task.plan, task.answer, task.target,
            task.priority, task.effort, task.decision_needed,
            task.improvement_id, task.approval_id, task.run_id,
            task.triage_retries, task.execute_retries, task.last_error,
            task.next_retry_at.isoformat() if task.next_retry_at else None,
            task.status,
        ),
    ) as cur:
        row_id = cur.lastrowid
    await db.commit()
    return row_id


async def get_task(db: aiosqlite.Connection, task_id: int) -> Task | None:
    async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_task(row)


async def update_task(db: aiosqlite.Connection, task_id: int, **kwargs) -> None:
    """Update arbitrary task fields. Automatically sets updated_at."""
    if not kwargs:
        return
    kwargs["updated_at"] = _now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    await db.commit()


async def claim_for_triage(db: aiosqlite.Connection, task_id: int) -> bool:
    """Atomic CAS: transition status from 'received' to 'triaging'.

    Returns True only if the task was in 'received' state and this call
    successfully claimed it. Prevents both _handle_message and the scheduler
    from triaging the same task twice.
    """
    async with db.execute(
        "UPDATE tasks SET status='triaging', updated_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND status='received'",
        (task_id,),
    ) as cur:
        claimed = cur.rowcount > 0
    await db.commit()
    return claimed


async def list_tasks(
    db: aiosqlite.Connection,
    status: str | None = None,
    kind: str | None = None,
    chat_id: str | None = None,
    decision_needed: bool | None = None,
    limit: int = 50,
) -> list[Task]:
    conditions: list[str] = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if chat_id:
        conditions.append("chat_id = ?")
        params.append(chat_id)
    if decision_needed is not None:
        conditions.append("decision_needed = ?")
        params.append(decision_needed)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    async with db.execute(
        f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?", params
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_tasks_by_statuses(
    db: aiosqlite.Connection,
    statuses: list[str],
    limit: int = 50,
) -> list[Task]:
    placeholders = ",".join("?" * len(statuses))
    async with db.execute(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
        (*statuses, limit),
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def count_tasks_by_status(db: aiosqlite.Connection) -> dict[str, int]:
    """Return a mapping of status → count for dashboard display."""
    async with db.execute(
        "SELECT status, COUNT(*) FROM tasks GROUP BY status"
    ) as cur:
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}


async def list_triaging_tasks(db: aiosqlite.Connection) -> list[Task]:
    """Tasks stuck in 'triaging' — likely crashed, need recovery."""
    async with db.execute(
        "SELECT * FROM tasks WHERE status = 'triaging' ORDER BY created_at"
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_received_tasks(db: aiosqlite.Connection) -> list[Task]:
    """Tasks not yet claimed for triage."""
    async with db.execute(
        "SELECT * FROM tasks WHERE status = 'received' ORDER BY created_at"
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        raw_message=row["raw_message"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        source_message_id=row["source_message_id"],
        kind=row["kind"],
        summary=row["summary"],
        plan=row["plan"],
        answer=row["answer"],
        target=row["target"],
        priority=row["priority"],
        effort=row["effort"],
        decision_needed=bool(row["decision_needed"]),
        improvement_id=row["improvement_id"],
        approval_id=row["approval_id"],
        run_id=row["run_id"],
        triage_retries=row["triage_retries"],
        execute_retries=row["execute_retries"],
        last_error=row["last_error"],
        next_retry_at=_parse_dt(row["next_retry_at"]),
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )
