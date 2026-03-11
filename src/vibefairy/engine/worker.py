"""Execution plane worker.

Responsibilities:
- Receive an Improvement + optional Approval
- Verify policy (via PolicyEngine)
- Acquire target lock
- Run Claude session
- Record run in DB
- Release lock on completion/failure
- Retry with exponential backoff
- Mark dead-letter after max retries
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiosqlite

from vibefairy.config.loader import DaemonConfig
from vibefairy.config.secrets import Secrets
from vibefairy.engine.claude_session import (
    ClaudePermanentError,
    ClaudeTransientError,
    ClaudeTimeoutError,
)
from vibefairy.engine.model_session import build_model_session
from vibefairy.engine.policy import ExecutionMode, PolicyEngine
from vibefairy.engine.resilience import get_claude_semaphore, is_transient_error
from vibefairy.memory import repo
from vibefairy.memory.models import Improvement, Run

logger = logging.getLogger(__name__)


@dataclass
class WorkerTask:
    improvement: Improvement
    prompt: str
    approval_id: int | None = None
    requested_mode: ExecutionMode = ExecutionMode.READONLY


@dataclass
class WorkerResult:
    run_id: int
    success: bool
    output: str
    token_count: int
    duration_secs: float
    exit_code: int
    mode: ExecutionMode
    error: str | None = None


class Worker:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
        policy: PolicyEngine,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db
        self._policy = policy

    async def execute(self, task: WorkerTask) -> WorkerResult:
        """Execute a task with retry + dead-letter logic."""
        imp = task.improvement
        target_project = self._find_target(imp.target)

        if not target_project:
            return WorkerResult(
                run_id=-1,
                success=False,
                output="",
                token_count=0,
                duration_secs=0,
                exit_code=1,
                mode=task.requested_mode,
                error=f"Target '{imp.target}' not found in config",
            )

        # Policy check
        policy_result = await self._policy.evaluate(
            prompt=task.prompt,
            requested_mode=task.requested_mode,
            approval_id=task.approval_id,
            improvement_id=imp.id,
            target=imp.target,
        )
        if not policy_result.allowed:
            logger.warning("Policy denied execution: %s", policy_result.reason)
            await repo.log_event(
                self._db,
                "policy_denied",
                source="worker",
                detail=f"imp={imp.id} reason={policy_result.reason}",
            )
            return WorkerResult(
                run_id=-1,
                success=False,
                output=policy_result.reason,
                token_count=0,
                duration_secs=0,
                exit_code=1,
                mode=task.requested_mode,
                error=policy_result.reason,
            )

        # Create run record
        run = Run(
            id=None,
            improvement_id=imp.id,
            approval_id=task.approval_id,
            target=imp.target,
            prompt_hash=policy_result.prompt_hash,
            execution_mode=task.requested_mode.value,
            status="pending",
        )
        run_id = await repo.create_run(self._db, run)

        # Retry loop
        retry_cfg = self._cfg.retry
        attempt = 0
        last_error: str | None = None

        while attempt <= retry_cfg.max_retries:
            if attempt > 0:
                backoff = min(
                    retry_cfg.backoff_base_secs * (2 ** (attempt - 1)),
                    retry_cfg.backoff_max_secs,
                )
                logger.info("Retry %d/%d in %ds for run %d", attempt, retry_cfg.max_retries, backoff, run_id)
                await asyncio.sleep(backoff)

            result = await self._execute_once(
                run_id=run_id,
                task=task,
                target_path=target_project["path"],
            )

            if result.success:
                return result

            last_error = result.error or "unknown error"
            # Only retry transient errors
            if not self._is_transient(last_error):
                break
            attempt += 1

        # Dead-letter
        logger.error("Run %d dead-lettered after %d attempts: %s", run_id, attempt, last_error)
        await repo.update_run(self._db, run_id, status="dead_letter", output_summary=last_error)
        await repo.update_improvement_status(self._db, imp.id, "dead_letter")
        await repo.log_event(
            self._db,
            "dead_letter",
            source="worker",
            detail=f"run={run_id} imp={imp.id} error={last_error}",
        )
        return WorkerResult(
            run_id=run_id,
            success=False,
            output=last_error or "",
            token_count=0,
            duration_secs=0,
            exit_code=1,
            mode=task.requested_mode,
            error=last_error,
        )

    async def _execute_once(
        self,
        run_id: int,
        task: WorkerTask,
        target_path: str,
    ) -> WorkerResult:
        imp = task.improvement
        lock_holder = f"run_{run_id}"

        # 锁序：sem → lock → run（避免持锁等信号量导致死锁）
        sem = get_claude_semaphore()
        async with sem:
            # Acquire write lock after holding semaphore slot
            if task.requested_mode == ExecutionMode.WRITE:
                acquired = await repo.acquire_lock(
                    self._db, imp.target, lock_holder, self._cfg.lock_ttl_minutes
                )
                if not acquired:
                    return WorkerResult(
                        run_id=run_id,
                        success=False,
                        output="",
                        token_count=0,
                        duration_secs=0,
                        exit_code=1,
                        mode=task.requested_mode,
                        error="target_locked",
                    )

            await repo.update_run(self._db, run_id, status="executing")

            # Worker already holds the semaphore slot — pass semaphore=None to skip inner control
            session = build_model_session(
                self._cfg.models.main,
                working_dir=target_path,
                retry_cfg=self._cfg.retry,
                semaphore=None,
            )

            start = time.monotonic()
            try:
                if task.requested_mode == ExecutionMode.WRITE:
                    result = await session.run_write(task.prompt)
                else:
                    result = await session.run_readonly(task.prompt)

                # Success path
                await repo.update_run(
                    self._db,
                    run_id,
                    status="applied",
                    output_summary=result.output[:2000],
                    token_count=result.token_count,
                    duration_secs=result.duration_secs,
                    exit_code=result.exit_code,
                )
                if imp.id is not None:
                    await repo.update_improvement_status(self._db, imp.id, "applied")

                return WorkerResult(
                    run_id=run_id,
                    success=True,
                    output=result.output,
                    token_count=result.token_count,
                    duration_secs=result.duration_secs,
                    exit_code=result.exit_code,
                    mode=task.requested_mode,
                )

            except (ClaudeTransientError, ClaudeTimeoutError) as e:
                # Transient: outer retry loop will decide whether to re-attempt
                logger.warning("Transient/timeout error in run %d: %s", run_id, e)
                await repo.update_run(self._db, run_id, status="failed", output_summary=str(e)[:500])
                return WorkerResult(
                    run_id=run_id,
                    success=False,
                    output="",
                    token_count=0,
                    duration_secs=time.monotonic() - start,
                    exit_code=1,
                    mode=task.requested_mode,
                    error=str(e),
                )

            except ClaudePermanentError as e:
                # Permanent: no retry
                logger.error("Permanent error in run %d: %s", run_id, e)
                await repo.update_run(self._db, run_id, status="failed", output_summary=str(e)[:500])
                return WorkerResult(
                    run_id=run_id,
                    success=False,
                    output="",
                    token_count=0,
                    duration_secs=time.monotonic() - start,
                    exit_code=1,
                    mode=task.requested_mode,
                    error=f"permanent: {e}",
                )

            except Exception as e:
                logger.exception("Unexpected error in run %d", run_id)
                await repo.update_run(self._db, run_id, status="failed", output_summary=str(e)[:500])
                return WorkerResult(
                    run_id=run_id,
                    success=False,
                    output="",
                    token_count=0,
                    duration_secs=time.monotonic() - start,
                    exit_code=1,
                    mode=task.requested_mode,
                    error=str(e),
                )

            finally:
                if task.requested_mode == ExecutionMode.WRITE:
                    await repo.release_lock(self._db, imp.target, lock_holder)

    def _find_target(self, name: str) -> dict | None:
        for t in self._cfg.targets:
            if t.name == name:
                return {"name": t.name, "path": t.path, "allow_write": t.allow_write}
        return None

    def _is_transient(self, error: str) -> bool:
        return is_transient_error(error, extra_patterns=self._cfg.retry.transient_errors)

