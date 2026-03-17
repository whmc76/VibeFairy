"""Runner agent — general-purpose task execution.

Provides high-level task coordination:
- Find target project
- Build a scoped prompt with project context
- Route to worker with appropriate mode
- Return result summary
"""

from __future__ import annotations

import logging

import aiosqlite

from vibefairy.config.loader import DaemonConfig
from vibefairy.config.secrets import Secrets
from vibefairy.engine.policy import ExecutionMode
from vibefairy.engine.worker import Worker, WorkerResult, WorkerTask
from vibefairy.memory import repo
from vibefairy.memory.models import Improvement

logger = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
        worker: Worker,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db
        self._worker = worker

    async def run_task(
        self,
        prompt: str,
        target_name: str | None = None,
        mode: ExecutionMode = ExecutionMode.READONLY,
        approval_id: int | None = None,
    ) -> WorkerResult:
        """Run an ad-hoc task against a target project."""
        target = self._find_target(target_name)
        if not target:
            names = [t.name for t in self._cfg.targets]
            raise ValueError(f"Target '{target_name}' not found. Available: {names}")

        if mode == ExecutionMode.WRITE and not target.allow_write:
            raise ValueError(f"Target '{target.name}' does not allow write operations")

        imp = Improvement(
            id=None,
            target=target.name,
            summary=prompt[:200],
            detail=prompt,
            effort="S",
            priority="P2",
            status="proposed",
        )
        imp_id = await repo.create_improvement(self._db, imp)
        imp.id = imp_id

        task = WorkerTask(
            improvement=imp,
            prompt=prompt,
            approval_id=approval_id,
            requested_mode=mode,
        )
        return await self._worker.execute(task)

    async def run_readonly_query(self, prompt: str, target_name: str | None = None) -> str:
        """Convenience method for read-only Claude queries. Returns output text."""
        result = await self.run_task(prompt, target_name, mode=ExecutionMode.READONLY)
        return result.output

    def _find_target(self, name: str | None):
        if name is None:
            # Use primary or first
            for t in self._cfg.targets:
                if t.primary:
                    return t
            return self._cfg.targets[0] if self._cfg.targets else None
        for t in self._cfg.targets:
            if t.name == name:
                return t
        return None
