"""Telegram Bot — bidirectional communication hub (V2).

Message model: every non-command message becomes a Task automatically.

Flow:
  Message received → Task created (received) → claim_for_triage (inline)
                   → TriageAgent classifies:
                       note    → reply confirmation → closed
                       question → Claude answer     → closed
                       action  → plan card with [批准执行][打回重做][取消] buttons
                                  → user clicks → approved → worker executes

Command table:
  /start        — welcome + help
  /scout        — trigger immediate discovery round
  /report       — recent discoveries + proposals summary
  /status       — daemon status, budget, task board
  /list         — active action tasks (awaiting decision + executing)
  /done         — completed tasks today
  /targets      — list managed projects
  /approve <id> — approve a TASK (task.id)
  /approve_imp <id> — approve an IMPROVEMENT (improvements.id, backward compat)
  /reject <id>  — reject a task
  /retry <id>   — retry a failed/dead-letter task
  /dismiss <id> — dismiss a dead-letter task
  /cancel <id>  — cancel a task
  /budget       — today's token usage
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Awaitable

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import aiosqlite

from vibefairy.config.loader import DaemonConfig
from vibefairy.config.secrets import Secrets
from vibefairy.memory import repo
from vibefairy.memory.models import Approval, Improvement, Task

if TYPE_CHECKING:
    from vibefairy.engine.policy import PolicyEngine
    from vibefairy.engine.worker import Worker
    from vibefairy.agents.triage import TriageAgent

logger = logging.getLogger(__name__)

ScoutTrigger = Callable[[], Awaitable[None]]
TriageFn = Callable[[int], Awaitable[None]]


class TelegramBot:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
        policy: "PolicyEngine",
        worker: "Worker",
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db
        self._policy = policy
        self._worker = worker
        self._scout_trigger: ScoutTrigger | None = None
        self._triage_fn: TriageFn | None = None
        self._app: Application | None = None
        self._start_time = datetime.now(tz=timezone.utc)

    def set_scout_trigger(self, fn: ScoutTrigger) -> None:
        self._scout_trigger = fn

    def set_triage_fn(self, fn: TriageFn) -> None:
        """Wire in TriageAgent.process_task so bot can trigger inline triage."""
        self._triage_fn = fn

    async def start(self) -> None:
        self._app = Application.builder().token(self._secrets.telegram_bot_token).build()

        handlers = [
            CommandHandler("start",       self._cmd_start),
            CommandHandler("scout",       self._cmd_scout),
            CommandHandler("report",      self._cmd_report),
            CommandHandler("status",      self._cmd_status),
            CommandHandler("list",        self._cmd_list),
            CommandHandler("done",        self._cmd_done),
            CommandHandler("targets",     self._cmd_targets),
            CommandHandler("approve",     self._cmd_approve),
            CommandHandler("approve_imp", self._cmd_approve_imp),
            CommandHandler("reject",      self._cmd_reject),
            CommandHandler("retry",       self._cmd_retry),
            CommandHandler("dismiss",     self._cmd_dismiss),
            CommandHandler("cancel",      self._cmd_cancel),
            CommandHandler("budget",      self._cmd_budget),
            CallbackQueryHandler(self._handle_callback),
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message),
        ]
        for h in handlers:
            self._app.add_handler(h)

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (V2 task mode)")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_message(self, chat_id: str, text: str, parse_mode: str = "HTML") -> None:
        if self._app is None:
            return
        for attempt in range(3):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                )
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    logger.warning("send_message failed after 3 attempts to %s: %s", chat_id, e)

    async def _safe_reply(self, message, text: str, **kwargs) -> None:
        """带重试的 reply_text（关键通知路径，用户无法主动重试）。"""
        for attempt in range(3):
            try:
                await message.reply_text(text, **kwargs)
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    logger.warning("reply_text failed after 3 attempts: %s", e)

    async def _safe_edit(self, query, text: str, **kwargs) -> None:
        """带重试的 edit_message_text（关键通知路径，用户无法主动重试）。"""
        for attempt in range(3):
            try:
                await query.edit_message_text(text, **kwargs)
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    logger.warning("edit_message_text failed after 3 attempts: %s", e)

    async def broadcast(self, text: str, parse_mode: str = "HTML") -> None:
        for cid in self._secrets.allowed_chat_ids:
            await self.send_message(cid, text, parse_mode)

    # ---------------------------------------------------------------------- #
    # Auth guard
    # ---------------------------------------------------------------------- #

    def _is_allowed(self, update: Update) -> bool:
        if update.effective_chat is None:
            return False
        return str(update.effective_chat.id) in self._secrets.allowed_chat_ids

    async def _auth_check(self, update: Update) -> bool:
        if not self._is_allowed(update):
            logger.warning(
                "Unauthorized access from chat_id=%s",
                update.effective_chat.id if update.effective_chat else "unknown",
            )
            if update.message:
                await update.message.reply_text("Unauthorized.")
            return False
        return True

    def _chat_id(self, update: Update) -> str:
        return str(update.effective_chat.id) if update.effective_chat else ""

    def _user_id(self, update: Update) -> str:
        return str(update.effective_user.id) if update.effective_user else "unknown"

    # ---------------------------------------------------------------------- #
    # Core: _handle_message — message → task → inline triage
    # ---------------------------------------------------------------------- #

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Every non-command text message becomes a Task automatically."""
        if not await self._auth_check(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return

        chat_id = self._chat_id(update)
        user_id = self._user_id(update)
        source_message_id = update.message.message_id

        # 1. Create task in DB
        task = Task(
            id=None,
            raw_message=text,
            chat_id=chat_id,
            user_id=user_id,
            source_message_id=source_message_id,
        )
        task_id = await repo.create_task(self._db, task)

        # 2. Immediate receipt
        await update.message.reply_text(
            f"收到! Task #{task_id} 已创建，正在分析..."
        )

        # 3. Inline triage (background)
        if self._triage_fn is not None:
            asyncio.create_task(
                self._triage_and_notify(task_id, update)
            )

    async def _triage_and_notify(self, task_id: int, update: Update) -> None:
        """Run triage and send result notification to user."""
        try:
            await self._triage_fn(task_id)
        except Exception as e:
            logger.exception("Inline triage failed for task #%d", task_id)
            await self._safe_reply(
                update.message,
                f"Task #{task_id} 分析失败: {e}\n系统将自动重试。",
            )
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            return

        if task.status == "noted":
            await self._safe_reply(
                update.message,
                f"Task #{task_id} — 已记录备忘\n{task.summary or ''}",
            )

        elif task.status == "answered":
            answer = task.answer or "(无回答)"
            await self._safe_reply(
                update.message,
                f"Task #{task_id} — 回答\n\n{answer[:3800]}",
            )

        elif task.status == "awaiting_user_decision":
            await self._send_decision_card(update.message.reply_text, task)

        else:
            # Triage failed or unexpected status
            await self._safe_reply(
                update.message,
                f"Task #{task_id} 分析结果: {task.status}",
            )

    async def _send_decision_card(self, reply_fn, task: Task) -> None:
        """Send an InlineKeyboard decision card for an action task."""
        priority_str = task.priority or "?"
        effort_str = task.effort or "?"
        summary = task.summary or task.raw_message[:80]
        plan = task.plan or "(方案生成中...)"

        # Truncate plan for Telegram limit
        plan_preview = plan[:1500] if len(plan) > 1500 else plan

        card_text = (
            f"<b>Task #{task.id}</b> — {summary}\n"
            f"Priority: {priority_str} | Effort: {effort_str}\n"
            f"Target: {task.target or '未指定'}\n\n"
            f"<b>方案:</b>\n{plan_preview}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("批准执行", callback_data=f"approve:{task.id}"),
                InlineKeyboardButton("打回重做", callback_data=f"rework:{task.id}"),
            ],
            [
                InlineKeyboardButton("取消", callback_data=f"cancel:{task.id}"),
            ],
        ])

        await reply_fn(card_text, parse_mode="HTML", reply_markup=keyboard)

    # ---------------------------------------------------------------------- #
    # Callback query handler (inline buttons)
    # ---------------------------------------------------------------------- #

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if not self._is_allowed(update):
            await query.edit_message_text("Unauthorized.")
            return

        data = query.data or ""
        if ":" not in data:
            return

        action, _, id_str = data.partition(":")
        try:
            task_id = int(id_str)
        except ValueError:
            await query.edit_message_text("Invalid task ID.")
            return

        if action == "approve":
            await self._callback_approve_task(query, task_id, update)
        elif action == "rework":
            await self._callback_rework_task(query, task_id)
        elif action == "cancel":
            await self._callback_cancel_task(query, task_id)
        else:
            await query.edit_message_text(f"Unknown action: {action}")

    async def _callback_approve_task(self, query, task_id: int, update: Update) -> None:
        task = await repo.get_task(self._db, task_id)
        if task is None:
            await query.edit_message_text(f"Task #{task_id} 不存在。")
            return
        if task.status != "awaiting_user_decision":
            await query.edit_message_text(
                f"Task #{task_id} 状态为 '{task.status}'，无法批准。"
            )
            return

        # Create improvement + approval + execute
        imp = Improvement(
            id=None,
            target=task.target or self._primary_target_name() or "unknown",
            summary=task.summary or task.raw_message[:200],
            detail=task.plan or task.raw_message,
            effort=task.effort,
            priority=task.priority,
            status="proposed",
        )
        imp_id = await repo.create_improvement(self._db, imp)

        ttl = self._cfg.approval_default_ttl_minutes
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl)
        approval = Approval(
            id=None,
            improvement_id=imp_id,
            approved_by=self._user_id(update),
            chat_id=self._chat_id(update),
            prompt_snapshot=task.plan or task.raw_message,
            execution_mode="write",
            ttl_minutes=ttl,
            expires_at=expires,
        )
        approval_id = await repo.create_approval(self._db, approval)
        await repo.update_improvement_status(self._db, imp_id, "approved")

        # Link task to improvement + approval, update status
        await repo.update_task(
            self._db, task_id,
            status="approved",
            decision_needed=False,
            improvement_id=imp_id,
            approval_id=approval_id,
        )

        await self._safe_edit(
            query,
            f"Task #{task_id} 已批准，开始执行...\n"
            f"Improvement #{imp_id} | Approval #{approval_id}",
        )

        # Execute in background
        asyncio.create_task(
            self._execute_task(query, task_id, imp, approval_id)
        )

    async def _execute_task(self, query, task_id: int, imp: Improvement, approval_id: int) -> None:
        from vibefairy.engine.policy import ExecutionMode
        from vibefairy.engine.worker import WorkerTask

        await repo.update_task(self._db, task_id, status="executing")

        worker_task = WorkerTask(
            improvement=imp,
            prompt=imp.detail or imp.summary,
            approval_id=approval_id,
            requested_mode=ExecutionMode.WRITE,
        )
        try:
            result = await self._worker.execute(worker_task)
            status = "done" if result.success else "failed"
            summary = result.output[:500] if result.output else "(no output)"
            await repo.update_task(
                self._db, task_id,
                status=status,
                run_id=result.run_id,
                execute_retries=0,
            )
            await self._safe_reply(
                query.message,
                f"<b>Task #{task_id} 执行{'完成' if result.success else '失败'}</b>\n"
                f"状态: {status} | Tokens: {result.token_count:,} | 耗时: {result.duration_secs:.1f}s\n\n"
                f"<code>{summary}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Execution failed for task #%d", task_id)
            await repo.update_task(
                self._db, task_id,
                status="failed",
                last_error=str(e),
            )
            await self._safe_reply(
                query.message,
                f"Task #{task_id} 执行出错: {e}",
            )

    async def _callback_rework_task(self, query, task_id: int) -> None:
        task = await repo.get_task(self._db, task_id)
        if task is None:
            await query.edit_message_text(f"Task #{task_id} 不存在。")
            return
        # Reset to received for re-triage
        await repo.update_task(
            self._db, task_id,
            status="received",
            decision_needed=False,
            plan=None,
            triage_retries=0,
        )
        await query.edit_message_text(
            f"Task #{task_id} 已打回，重新分析...\n"
            "如需指导分析方向，请直接回复具体要求。"
        )
        # Re-triage
        if self._triage_fn is not None:
            asyncio.create_task(self._triage_fn(task_id))

    async def _callback_cancel_task(self, query, task_id: int) -> None:
        task = await repo.get_task(self._db, task_id)
        if task is None:
            await query.edit_message_text(f"Task #{task_id} 不存在。")
            return
        await repo.update_task(self._db, task_id, status="cancelled", decision_needed=False)
        await query.edit_message_text(f"Task #{task_id} 已取消。")

    # ---------------------------------------------------------------------- #
    # Commands
    # ---------------------------------------------------------------------- #

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        text = (
            "<b>VibeFairy</b> — 消息即任务\n\n"
            "直接发送任何消息 → 自动创建任务并分析\n\n"
            "<b>任务命令:</b>\n"
            "/list — 活跃任务看板\n"
            "/done — 今日完成\n"
            "/approve &lt;id&gt; — 批准任务\n"
            "/reject &lt;id&gt; — 拒绝任务\n"
            "/cancel &lt;id&gt; — 取消任务\n"
            "/retry &lt;id&gt; — 重试失败任务\n\n"
            "<b>系统命令:</b>\n"
            "/scout — 立即触发发现\n"
            "/report — 发现 + 建议摘要\n"
            "/status — 系统状态\n"
            "/budget — 今日 token 用量\n"
            "/targets — 管理的项目\n"
            "/approve_imp &lt;id&gt; — 批准改进建议（Scout 流程）\n"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show active action tasks (excluding note/closed/cancelled)."""
        if not await self._auth_check(update):
            return

        active_statuses = ["received", "triaging", "planned", "awaiting_user_decision", "approved", "executing"]
        tasks = await repo.list_tasks_by_statuses(self._db, active_statuses, limit=20)
        # Only show action tasks (or unknown)
        action_tasks = [t for t in tasks if t.kind in ("action", "unknown")]

        counts = await repo.count_tasks_by_status(self._db)

        pending_decision = [t for t in action_tasks if t.status == "awaiting_user_decision"]
        in_progress = [t for t in action_tasks if t.status in ("approved", "executing")]
        queued = [t for t in action_tasks if t.status in ("received", "triaging", "planned")]

        lines = ["<b>任务看板</b>"]

        if pending_decision:
            lines.append(f"\n<b>待确认 ({len(pending_decision)})</b>")
            for t in pending_decision[:5]:
                pri = t.priority or "?"
                eff = t.effort or "?"
                lines.append(f"  #{t.id} [{pri}/{eff}] {(t.summary or t.raw_message)[:60]}")

        if in_progress:
            lines.append(f"\n<b>执行中 ({len(in_progress)})</b>")
            for t in in_progress[:5]:
                lines.append(f"  #{t.id} [{t.status}] {(t.summary or t.raw_message)[:60]}")

        if queued:
            lines.append(f"\n<b>队列中 ({len(queued)})</b>")
            for t in queued[:5]:
                lines.append(f"  #{t.id} [{t.status}] {(t.summary or t.raw_message)[:60]}")

        if not action_tasks:
            lines.append("\n暂无活跃任务。直接发消息即可创建任务。")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_done(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show tasks completed today."""
        if not await self._auth_check(update):
            return
        tasks = await repo.list_tasks_by_statuses(self._db, ["done"], limit=20)
        today = datetime.now().date()
        done_today = [t for t in tasks if t.created_at and t.created_at.date() == today]

        if not done_today:
            await update.message.reply_text("今日暂无完成的任务。")
            return

        lines = [f"<b>今日完成 ({len(done_today)})</b>"]
        for t in done_today:
            pri = t.priority or "?"
            lines.append(f"  #{t.id} [{pri}] {(t.summary or t.raw_message)[:70]}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_approve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve a TASK by task_id."""
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /approve <task_id>")
            return
        try:
            task_id = int(args[0])
        except ValueError:
            await update.message.reply_text("无效 ID。用法: /approve <数字>")
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            await update.message.reply_text(f"Task #{task_id} 不存在。")
            return
        if task.status != "awaiting_user_decision":
            await update.message.reply_text(
                f"Task #{task_id} 当前状态为 '{task.status}'，不能批准。\n"
                "只有 awaiting_user_decision 状态的任务可以批准。"
            )
            return

        # Reuse callback logic via a stub query object
        await update.message.reply_text(f"Task #{task_id} 批准中...")
        await self._approve_task_by_id(task_id, update)

    async def _approve_task_by_id(self, task_id: int, update: Update) -> None:
        """Core approval logic, shared between button and text command."""
        task = await repo.get_task(self._db, task_id)
        if task is None:
            return

        imp = Improvement(
            id=None,
            target=task.target or self._primary_target_name() or "unknown",
            summary=task.summary or task.raw_message[:200],
            detail=task.plan or task.raw_message,
            effort=task.effort,
            priority=task.priority,
            status="proposed",
        )
        imp_id = await repo.create_improvement(self._db, imp)

        ttl = self._cfg.approval_default_ttl_minutes
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl)
        approval = Approval(
            id=None,
            improvement_id=imp_id,
            approved_by=self._user_id(update),
            chat_id=self._chat_id(update),
            prompt_snapshot=task.plan or task.raw_message,
            execution_mode="write",
            ttl_minutes=ttl,
            expires_at=expires,
        )
        approval_id = await repo.create_approval(self._db, approval)
        await repo.update_improvement_status(self._db, imp_id, "approved")
        await repo.update_task(
            self._db, task_id,
            status="approved",
            decision_needed=False,
            improvement_id=imp_id,
            approval_id=approval_id,
        )

        asyncio.create_task(
            self._execute_task_text(task_id, task, imp, approval_id, update)
        )

    async def _execute_task_text(
        self, task_id: int, task: Task, imp: Improvement, approval_id: int, update: Update
    ) -> None:
        from vibefairy.engine.policy import ExecutionMode
        from vibefairy.engine.worker import WorkerTask

        await repo.update_task(self._db, task_id, status="executing")
        worker_task = WorkerTask(
            improvement=imp,
            prompt=imp.detail or imp.summary,
            approval_id=approval_id,
            requested_mode=ExecutionMode.WRITE,
        )
        try:
            result = await self._worker.execute(worker_task)
            status = "done" if result.success else "failed"
            summary = result.output[:500] if result.output else "(no output)"
            await repo.update_task(self._db, task_id, status=status, run_id=result.run_id)
            await self._safe_reply(
                update.message,
                f"<b>Task #{task_id} 执行{'完成' if result.success else '失败'}</b>\n"
                f"状态: {status} | Tokens: {result.token_count:,} | 耗时: {result.duration_secs:.1f}s\n\n"
                f"<code>{summary}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Text-command execution failed for task #%d", task_id)
            await repo.update_task(self._db, task_id, status="failed", last_error=str(e))
            await self._safe_reply(update.message, f"Task #{task_id} 执行出错: {e}")

    async def _cmd_approve_imp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve an IMPROVEMENT by improvement_id (Scout pipeline, backward compat)."""
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /approve_imp <improvement_id>")
            return
        try:
            imp_id = int(args[0])
        except ValueError:
            await update.message.reply_text("无效 ID。")
            return

        imp = await repo.get_improvement(self._db, imp_id)
        if imp is None:
            await update.message.reply_text(f"Improvement #{imp_id} 不存在。")
            return
        if imp.status not in ("proposed", "analyzed"):
            await update.message.reply_text(
                f"Improvement #{imp_id} 状态为 '{imp.status}'，无法批准。"
            )
            return

        ttl = self._cfg.approval_default_ttl_minutes
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl)
        approval = Approval(
            id=None,
            improvement_id=imp_id,
            approved_by=self._user_id(update),
            chat_id=self._chat_id(update),
            prompt_snapshot=imp.detail or imp.summary,
            execution_mode="write",
            ttl_minutes=ttl,
            expires_at=expires,
        )
        approval_id = await repo.create_approval(self._db, approval)
        await repo.update_improvement_status(self._db, imp_id, "approved")

        await update.message.reply_text(
            f"Improvement #{imp_id} 已批准 (approval #{approval_id})，执行中..."
        )
        asyncio.create_task(self._execute_improvement(update, imp, approval_id))

    async def _execute_improvement(self, update: Update, imp: Improvement, approval_id: int) -> None:
        from vibefairy.engine.policy import ExecutionMode
        from vibefairy.engine.worker import WorkerTask

        task = WorkerTask(
            improvement=imp,
            prompt=imp.detail or imp.summary,
            approval_id=approval_id,
            requested_mode=ExecutionMode.WRITE,
        )
        try:
            result = await self._worker.execute(task)
            status = "applied" if result.success else "failed"
            summary = result.output[:500] if result.output else "(no output)"
            await update.message.reply_text(
                f"Improvement #{imp.id} {status} (run #{result.run_id})\n"
                f"Tokens: {result.token_count:,} | Duration: {result.duration_secs:.1f}s\n\n"
                f"<code>{summary}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Improvement execution failed for imp #%d", imp.id)
            await update.message.reply_text(f"Improvement #{imp.id} 执行出错: {e}")

    async def _cmd_reject(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /reject <task_id>")
            return
        try:
            task_id = int(args[0])
        except ValueError:
            await update.message.reply_text("无效 ID。")
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            # Fallback: maybe it's an improvement_id from old flow
            await update.message.reply_text(
                f"Task #{task_id} 不存在。"
                "\n如需拒绝改进建议，请使用 /dismiss <improvement_id>"
            )
            return
        await repo.update_task(self._db, task_id, status="cancelled", decision_needed=False)
        await repo.log_event(
            self._db, "human_action", source="telegram",
            detail=f"rejected task #{task_id} by {self._user_id(update)}"
        )
        await update.message.reply_text(f"Task #{task_id} 已拒绝。")

    async def _cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /cancel <task_id>")
            return
        try:
            task_id = int(args[0])
        except ValueError:
            await update.message.reply_text("无效 ID。")
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            await update.message.reply_text(f"Task #{task_id} 不存在。")
            return
        if task.status in ("done", "failed", "dead_letter", "closed", "cancelled"):
            await update.message.reply_text(f"Task #{task_id} 已是终态 ({task.status})，无法取消。")
            return
        await repo.update_task(self._db, task_id, status="cancelled", decision_needed=False)
        await update.message.reply_text(f"Task #{task_id} 已取消。")

    async def _cmd_retry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /retry <task_id>")
            return
        try:
            task_id = int(args[0])
        except ValueError:
            await update.message.reply_text("无效 ID。")
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            await update.message.reply_text(f"Task #{task_id} 不存在。")
            return
        if task.status not in ("failed", "dead_letter"):
            await update.message.reply_text(
                f"Task #{task_id} 状态为 '{task.status}'，只有 failed/dead_letter 可以重试。"
            )
            return
        await repo.update_task(
            self._db, task_id,
            status="received",
            last_error=None,
            triage_retries=0,
            execute_retries=0,
        )
        await update.message.reply_text(
            f"Task #{task_id} 已重置为 received，将自动重新分析。"
        )

    async def _cmd_dismiss(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Dismiss an improvement from the Scout pipeline (dead-letter)."""
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /dismiss <improvement_id>")
            return
        try:
            imp_id = int(args[0])
        except ValueError:
            await update.message.reply_text("无效 ID。")
            return

        imp = await repo.get_improvement(self._db, imp_id)
        if imp is None:
            await update.message.reply_text(f"Improvement #{imp_id} 不存在。")
            return
        await repo.update_improvement_status(self._db, imp_id, "dismissed")
        await repo.log_event(
            self._db, "human_action", source="telegram",
            detail=f"dismissed improvement #{imp_id} by {self._user_id(update)}"
        )
        await update.message.reply_text(f"Improvement #{imp_id} 已清除。")

    async def _cmd_scout(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._scout_trigger is None:
            await update.message.reply_text("Scout 未配置。")
            return
        await update.message.reply_text("触发发现轮次...")
        asyncio.create_task(self._run_scout_and_notify(update))

    async def _run_scout_and_notify(self, update: Update) -> None:
        try:
            await self._scout_trigger()
            await update.message.reply_text("Scout 完成。使用 /report 查看结果。")
        except Exception as e:
            await update.message.reply_text(f"Scout 失败: {e}")

    async def _cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        discoveries = await repo.list_discoveries(self._db, limit=10)
        improvements = await repo.list_improvements(self._db, status="proposed", limit=10)

        lines = [f"<b>最近发现 ({len(discoveries)})</b>"]
        for d in discoveries[:5]:
            score = f"{d.relevance_score:.1f}" if d.relevance_score else "?"
            lines.append(f"  [{d.status}] {d.title or d.url} (score={score})")

        lines.append(f"\n<b>待确认改进建议 ({len(improvements)})</b>")
        for imp in improvements[:5]:
            lines.append(f"  #{imp.id} [{imp.priority or '?'}] {imp.summary[:80]}")
            lines.append(f"    Target: {imp.target} | Effort: {imp.effort or '?'}")
            lines.append(f"    /approve_imp {imp.id} — 批准执行")

        if not discoveries and not improvements:
            lines.append("暂无数据。运行 /scout 开始发现。")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        uptime = datetime.now(tz=timezone.utc) - self._start_time
        uptime_str = str(uptime).split(".")[0]

        today_tokens = await repo.get_today_token_count(self._db)
        daily_limit = self._cfg.budget.daily_token_limit
        budget_pct = (today_tokens / daily_limit * 100) if daily_limit > 0 else 0

        counts = await repo.count_tasks_by_status(self._db)
        awaiting = counts.get("awaiting_user_decision", 0)
        executing = counts.get("executing", 0)
        received = counts.get("received", 0) + counts.get("triaging", 0)

        dead_letter_imps = await repo.list_improvements(self._db, status="dead_letter", limit=3)

        lines = [
            "<b>VibeFairy 状态</b>",
            f"运行时间: {uptime_str}",
            f"预算: {today_tokens:,}/{daily_limit:,} tokens ({budget_pct:.1f}%)",
            f"预算模式: {self._cfg.budget.over_budget_mode}",
            f"主模型: {self._cfg.models.main.provider} / {self._cfg.models.main.model or 'default'}",
            (
                f"Review 模型: {self._cfg.models.review.provider} / "
                f"{self._cfg.models.review.model or 'default'}"
                if self._cfg.models.review.enabled
                else "Review 模型: disabled"
            ),
            "",
            "<b>任务看板</b>",
            f"  待分拣: {received}",
            f"  待确认: {awaiting}",
            f"  执行中: {executing}",
            f"  改进建议死信: {len(dead_letter_imps)}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_targets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if not self._cfg.targets:
            await update.message.reply_text("未配置目标项目。")
            return
        lines = ["<b>管理的项目</b>"]
        for t in self._cfg.targets:
            lines.append(
                f"  <b>{t.name}</b> {'(主要)' if t.primary else ''}\n"
                f"    路径: {t.path}\n"
                f"    允许写操作: {t.allow_write}\n"
                f"    {t.description}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_budget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        today_tokens = await repo.get_today_token_count(self._db)
        daily_limit = self._cfg.budget.daily_token_limit
        task_limit = self._cfg.budget.per_task_token_limit
        pct = (today_tokens / daily_limit * 100) if daily_limit > 0 else 0
        over = self._policy.is_over_budget

        icon = "🔴" if over else ("🟡" if pct > 75 else "🟢")
        await update.message.reply_text(
            f"{icon} <b>今日 Token 预算</b>\n"
            f"已用: {today_tokens:,}\n"
            f"每日上限: {daily_limit:,} ({pct:.1f}%)\n"
            f"单任务上限: {task_limit:,}\n"
            f"状态: {'超限' if over else '正常'}",
            parse_mode="HTML",
        )

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    def _primary_target(self):
        for t in self._cfg.targets:
            if t.primary:
                return t
        return self._cfg.targets[0] if self._cfg.targets else None

    def _primary_target_name(self) -> str | None:
        t = self._primary_target()
        return t.name if t else None

