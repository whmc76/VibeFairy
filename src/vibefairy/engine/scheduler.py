"""Task scheduler — cron + interval + event-based.

Uses asyncio tasks under the hood; no external deps required.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Coro = Callable[[], Awaitable[None]]


@dataclass
class ScheduledJob:
    name: str
    coro_factory: Coro
    interval_secs: float | None = None      # run every N seconds
    daily_time: dt_time | None = None       # run daily at HH:MM (local time)
    run_immediately: bool = False
    _task: asyncio.Task | None = field(default=None, repr=False, compare=False)


class Scheduler:
    def __init__(self):
        self._jobs: list[ScheduledJob] = []
        self._running = False

    def add_interval(
        self,
        name: str,
        coro_factory: Coro,
        interval_secs: float,
        run_immediately: bool = False,
    ) -> None:
        self._jobs.append(
            ScheduledJob(
                name=name,
                coro_factory=coro_factory,
                interval_secs=interval_secs,
                run_immediately=run_immediately,
            )
        )

    def add_daily(
        self,
        name: str,
        coro_factory: Coro,
        daily_time: str,
    ) -> None:
        """daily_time: 'HH:MM' 24-hour local time."""
        h, m = daily_time.split(":")
        self._jobs.append(
            ScheduledJob(
                name=name,
                coro_factory=coro_factory,
                daily_time=dt_time(int(h), int(m)),
            )
        )

    def start(self) -> None:
        self._running = True
        for job in self._jobs:
            if job.interval_secs is not None:
                job._task = asyncio.create_task(
                    self._interval_loop(job), name=f"sched:{job.name}"
                )
            elif job.daily_time is not None:
                job._task = asyncio.create_task(
                    self._daily_loop(job), name=f"sched:{job.name}"
                )

    async def stop(self) -> None:
        self._running = False
        for job in self._jobs:
            if job._task and not job._task.done():
                job._task.cancel()
                try:
                    await job._task
                except asyncio.CancelledError:
                    pass

    async def _interval_loop(self, job: ScheduledJob) -> None:
        if job.run_immediately:
            await self._safe_run(job)
        while self._running:
            await asyncio.sleep(job.interval_secs)
            if not self._running:
                break
            await self._safe_run(job)

    async def _daily_loop(self, job: ScheduledJob) -> None:
        while self._running:
            wait_secs = self._secs_until(job.daily_time)
            logger.debug("Job %s next run in %.0fs", job.name, wait_secs)
            await asyncio.sleep(wait_secs)
            if not self._running:
                break
            await self._safe_run(job)
            # Sleep 60s to avoid running twice in the same minute
            await asyncio.sleep(60)

    async def _safe_run(self, job: ScheduledJob) -> None:
        logger.info("Scheduler: running job '%s'", job.name)
        try:
            await job.coro_factory()
        except Exception:
            logger.exception("Job '%s' raised an unhandled exception", job.name)

    @staticmethod
    def _secs_until(target: dt_time) -> float:
        """Seconds until the next occurrence of target local time."""
        now = datetime.now()
        today_target = datetime.combine(now.date(), target)
        diff = (today_target - now).total_seconds()
        if diff < 0:
            # Already past today — schedule for tomorrow
            diff += 86400
        return diff
