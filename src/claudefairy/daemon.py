"""Main daemon — asyncio event loop + orchestration.

Startup sequence:
1. Load config + secrets (fail fast on missing secrets)
2. Setup logging
3. Open SQLite DB + run migrations
4. Crash recovery: mark stuck 'executing' runs as failed
5. Build subsystems: policy, worker, bot, scheduler
6. Register jobs: scout interval, daily report
7. Start Telegram bot
8. Run scheduler
9. On shutdown: stop bot, scheduler, close DB
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from claudefairy.config.loader import DaemonConfig, load_config
from claudefairy.config.secrets import SecretsError, load_secrets
from claudefairy.engine.policy import ExecutionMode, PolicyEngine
from claudefairy.engine.scheduler import Scheduler
from claudefairy.engine.worker import Worker
from claudefairy.memory import repo
from claudefairy.memory.db import open_db

logger = logging.getLogger(__name__)


def _setup_logging(cfg: DaemonConfig) -> None:
    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.log_level.upper(), logging.INFO)

    # JSON lines format for structured logs
    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return json.dumps(
                {
                    "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                },
                ensure_ascii=False,
            )

    # File handler (rotating)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "claudefairy.jsonl",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())

    # Console handler (human readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def _crash_recovery(db: aiosqlite.Connection, bot=None) -> None:
    """On restart: recover stuck runs and stuck triage tasks."""
    # 1. Recover stuck runs (executing → failed)
    stuck_runs = await repo.list_stuck_runs(db)
    if stuck_runs:
        logger.warning("Crash recovery: found %d stuck runs", len(stuck_runs))
        for run in stuck_runs:
            await repo.update_run(
                db,
                run.id,
                status="failed",
                output_summary="[crash_recovery] Daemon restarted while this run was executing",
            )
            await repo.log_event(
                db,
                "crash_recovery",
                source="daemon",
                detail=f"run={run.id} target={run.target}",
            )

    # 2. Recover stuck triage tasks (triaging → received)
    stuck_tasks = await repo.list_triaging_tasks(db)
    if stuck_tasks:
        logger.warning("Crash recovery: found %d stuck triaging tasks", len(stuck_tasks))
        for task in stuck_tasks:
            await repo.update_task(
                db, task.id,
                status="received",
                last_error="[crash_recovery] Daemon restarted during triage",
            )
            await repo.log_event(
                db,
                "crash_recovery",
                source="daemon",
                detail=f"task={task.id} reset triaging→received",
            )

    # 3. Clean expired locks
    count = await repo.cleanup_expired_locks(db)
    if count:
        logger.info("Crash recovery: cleaned %d expired locks", count)

    if bot and (stuck_runs or stuck_tasks):
        try:
            parts = []
            if stuck_runs:
                run_summary = ", ".join(f"run#{r.id}" for r in stuck_runs[:3])
                parts.append(f"{len(stuck_runs)} stuck run(s): {run_summary}")
            if stuck_tasks:
                parts.append(f"{len(stuck_tasks)} stuck triage task(s) reset to received")
            await bot.broadcast(
                f"Daemon restarted. Crash recovery: {'; '.join(parts)}"
            )
        except Exception:
            pass


async def _generate_daily_report(
    db: aiosqlite.Connection,
    cfg: DaemonConfig,
    bot,
) -> None:
    """Generate and send daily report. Pure SQL aggregation — zero token cost."""
    today_tokens = await repo.get_today_token_count(db)
    daily_limit = cfg.budget.daily_token_limit
    pct = (today_tokens / daily_limit * 100) if daily_limit > 0 else 0

    today = datetime.now().date()

    # Task board
    counts = await repo.count_tasks_by_status(db)
    awaiting_decision = counts.get("awaiting_user_decision", 0)
    executing = counts.get("executing", 0)
    received_queue = counts.get("received", 0) + counts.get("triaging", 0)

    # Tasks done today
    done_tasks = await repo.list_tasks_by_statuses(db, ["done"], limit=20)
    done_today = [t for t in done_tasks if t.created_at and t.created_at.date() == today]

    # Tasks awaiting decision (for detail)
    pending_tasks = await repo.list_tasks(db, status="awaiting_user_decision", limit=5)

    # Scout discoveries
    discoveries = await repo.list_discoveries(db, limit=20)
    new_discoveries = [
        d for d in discoveries
        if d.created_at and d.created_at.date() == today
    ]

    # Dead-letter improvements (Scout pipeline)
    dead_letter = await repo.list_improvements(db, status="dead_letter", limit=5)

    lines = [
        f"<b>ClaudeFairy 日报 — {today.strftime('%Y-%m-%d')}</b>",
        "",
        f"<b>任务看板:</b> 待分拣 {received_queue} | 待确认 {awaiting_decision} | 执行中 {executing} | 今日完成 {len(done_today)}",
        f"<b>预算:</b> {today_tokens:,}/{daily_limit:,} tokens ({pct:.1f}%)",
        f"<b>Scout 新发现:</b> {len(new_discoveries)}",
    ]

    if done_today:
        lines.append(f"\n<b>今日完成 ({len(done_today)}):</b>")
        for t in done_today[:5]:
            pri = t.priority or "?"
            lines.append(f"  #{t.id} [{pri}] {(t.summary or t.raw_message)[:70]}")

    if pending_tasks:
        lines.append(f"\n<b>待你确认 ({awaiting_decision}):</b>")
        for t in pending_tasks[:5]:
            pri = t.priority or "?"
            eff = t.effort or "?"
            lines.append(f"  #{t.id} [{pri}/{eff}] {(t.summary or t.raw_message)[:70]}")

    if dead_letter:
        lines.append(f"\n<b>改进建议死信 ({len(dead_letter)}):</b>")
        for imp in dead_letter[:3]:
            lines.append(f"  #{imp.id} {imp.summary[:60]} — /dismiss {imp.id}")

    await bot.broadcast("\n".join(lines))


async def run_daemon(
    config_path: Path | None = None,
    env_path: Path | None = None,
    log_level: str | None = None,
) -> None:
    """Main daemon entry point."""

    # 1. Load config
    try:
        cfg = load_config(config_path=config_path, env_path=env_path)
    except Exception as e:
        print(f"[FATAL] Config load failed: {e}", file=sys.stderr)
        sys.exit(1)

    if log_level:
        cfg.log_level = log_level

    # 2. Setup logging
    _setup_logging(cfg)
    logger.info("ClaudeFairy V2 starting up...")

    # 3. Load secrets (fail fast)
    try:
        secrets = load_secrets()
    except SecretsError as e:
        logger.critical("Secrets load failed: %s", e)
        sys.exit(1)

    logger.info("Secrets loaded OK")

    # 4. Open DB
    db = await open_db(cfg.db_path)
    logger.info("Database opened: %s", cfg.db_path)

    # 5. Build subsystems
    policy = PolicyEngine(cfg=cfg, db=db)
    worker = Worker(cfg=cfg, secrets=secrets, db=db, policy=policy)

    # Import bot here (avoids circular imports at module level)
    from claudefairy.comms.telegram_bot import TelegramBot
    bot = TelegramBot(cfg=cfg, secrets=secrets, db=db, policy=policy, worker=worker)

    # 6. Crash recovery (before bot.start so we can send notification)
    await _crash_recovery(db, bot=None)  # bot not started yet, skip notification

    # 7. Build agents
    from claudefairy.agents.scout import Scout
    from claudefairy.agents.analyst import Analyst
    from claudefairy.agents.advisor import Advisor
    from claudefairy.agents.runner import Runner
    from claudefairy.agents.triage import TriageAgent

    scout = Scout(cfg=cfg, secrets=secrets, db=db)
    analyst = Analyst(cfg=cfg, secrets=secrets, db=db)
    advisor = Advisor(cfg=cfg, secrets=secrets, db=db)
    runner = Runner(cfg=cfg, secrets=secrets, db=db, worker=worker)
    triage_agent = TriageAgent(cfg=cfg, secrets=secrets, db=db)

    # Wire scout notification → bot broadcast
    async def _scout_notify(text: str) -> None:
        await bot.broadcast(text)

    scout.add_notify_callback(_scout_notify)

    # Wire bot scout trigger → scout.run_round
    async def _trigger_scout() -> None:
        await scout.run_round()

    bot.set_scout_trigger(_trigger_scout)

    # Wire triage agent → bot (inline triage on message receipt)
    bot.set_triage_fn(triage_agent.process_task)

    # 8. Scheduler
    scheduler = Scheduler()

    scheduler.add_interval(
        name="scout",
        coro_factory=scout.run_round,
        interval_secs=cfg.scout_interval_secs,
        run_immediately=False,
    )

    scheduler.add_interval(
        name="cleanup_locks",
        coro_factory=lambda: repo.cleanup_expired_locks(db),
        interval_secs=300,  # every 5 minutes
    )

    # Periodic triage queue scan — claims tasks not yet handled inline
    scheduler.add_interval(
        name="triage_queue",
        coro_factory=triage_agent.process_pending_queue,
        interval_secs=cfg.triage.queue_scan_interval_secs,
        run_immediately=False,
    )

    scheduler.add_daily(
        name="daily_report",
        coro_factory=lambda: _generate_daily_report(db, cfg, bot),
        daily_time=cfg.daily_report_time,
    )

    # 9. Start everything
    await bot.start()
    logger.info("Telegram bot started")

    scheduler.start()
    logger.info("Scheduler started")

    # Log startup event
    await repo.log_event(db, "daemon_start", source="daemon", detail="ClaudeFairy V2 started")

    # Send startup notification to all chats
    target_names = [t.name for t in cfg.targets]
    await bot.broadcast(
        f"ClaudeFairy V2 online.\n"
        f"Targets: {', '.join(target_names) if target_names else 'none'}\n"
        f"Scout interval: {cfg.scout_interval_secs}s\n"
        f"Daily report: {cfg.daily_report_time}"
    )

    # 10. Run until signal
    stop_event = asyncio.Event()

    def _handle_signal(sig) -> None:
        logger.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))
        except (NotImplementedError, OSError):
            # Windows doesn't support add_signal_handler for all signals
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")

    # 11. Graceful shutdown
    logger.info("Shutting down...")
    await scheduler.stop()
    await bot.stop()
    await repo.log_event(db, "daemon_stop", source="daemon", detail="graceful shutdown")
    await db.close()
    logger.info("ClaudeFairy V2 stopped.")
