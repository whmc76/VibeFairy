"""Telegram Bot — bidirectional communication hub (V3).

Default message flow (triage_auto = false):
  Message received → forwarded to active session's AI backend (streaming)
  No active session → auto-create one from the primary target project

Legacy triage flow (triage_auto = true):
  Message received → Task created → TriageAgent classifies →
    note     → reply confirmation → closed
    question → Claude answer     → closed
    action   → plan card with buttons

Session commands (new):
  /new [name] [path]          — create a new session
  /sessions                   — list all sessions for this chat
  /use <name>                 — switch active session
  /close <name>               — close a session
  /model <model>              — change model for active session
  /backend <claude|codex>     — change backend for active session
  /cd <path>                  — change working directory of active session
  /history [n]                — show last N messages from active session

Legacy task commands (still available):
  /triage <message>           — explicitly run triage on a message
  /list, /done, /approve, /reject, /retry, /dismiss, /cancel
  /scout, /report, /switch, /approve_imp

System commands:
  /start   — welcome + help
  /status  — daemon status, budget, session summary
  /budget  — today token usage
  /targets — list managed projects
  /reload  — hot-reload daemon config
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

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
from vibefairy.engine.cli_backend import CLIBackend
from vibefairy.memory import repo
from vibefairy.memory.models import Approval, Improvement, SessionMessage, Task

if TYPE_CHECKING:
    from vibefairy.engine.policy import PolicyEngine
    from vibefairy.engine.session_manager import SessionManager
    from vibefairy.engine.worker import Worker
    from vibefairy.agents.triage import TriageAgent
    from vibefairy.engine.auth_manager import AuthManager

logger = logging.getLogger(__name__)

ScoutTrigger = Callable[[], Awaitable[None]]
TriageFn = Callable[[int], Awaitable[None]]
SwitchFn = Callable[[CLIBackend], Awaitable[None]]
ReloadFn = Callable[[], Awaitable[None]]


class TelegramBot:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
        policy: "PolicyEngine",
        worker: "Worker",
        session_manager: "SessionManager | None" = None,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db
        self._policy = policy
        self._worker = worker
        self._session_mgr = session_manager
        self._scout_trigger: ScoutTrigger | None = None
        self._triage_fn: TriageFn | None = None
        self._switch_fn: SwitchFn | None = None
        self._reload_fn: ReloadFn | None = None
        self._auth_manager: "AuthManager | None" = None
        self._app: Application | None = None
        self._start_time = datetime.now(tz=timezone.utc)

    def set_scout_trigger(self, fn: ScoutTrigger) -> None:
        self._scout_trigger = fn

    def set_triage_fn(self, fn: TriageFn) -> None:
        self._triage_fn = fn

    def set_switch_fn(self, fn: SwitchFn) -> None:
        self._switch_fn = fn

    def set_reload_fn(self, fn: ReloadFn) -> None:
        self._reload_fn = fn

    def set_auth_manager(self, auth_manager: "AuthManager") -> None:
        self._auth_manager = auth_manager

    async def start(self) -> None:
        self._app = Application.builder().token(self._secrets.telegram_bot_token).build()

        handlers = [
            # Session commands
            CommandHandler("new",         self._cmd_new),
            CommandHandler("sessions",    self._cmd_sessions),
            CommandHandler("use",         self._cmd_use),
            CommandHandler("close",       self._cmd_close_session),
            CommandHandler("model",       self._cmd_model),
            CommandHandler("backend",     self._cmd_backend),
            CommandHandler("cd",          self._cmd_cd),
            CommandHandler("history",     self._cmd_history),
            # System commands
            CommandHandler("start",       self._cmd_start),
            CommandHandler("status",      self._cmd_status),
            CommandHandler("budget",      self._cmd_budget),
            CommandHandler("targets",     self._cmd_targets),
            CommandHandler("reload",      self._cmd_reload),
            # Legacy task commands
            CommandHandler("triage",      self._cmd_triage),
            CommandHandler("scout",       self._cmd_scout),
            CommandHandler("report",      self._cmd_report),
            CommandHandler("list",        self._cmd_list),
            CommandHandler("done",        self._cmd_done),
            CommandHandler("approve",     self._cmd_approve),
            CommandHandler("approve_imp", self._cmd_approve_imp),
            CommandHandler("reject",      self._cmd_reject),
            CommandHandler("retry",       self._cmd_retry),
            CommandHandler("dismiss",     self._cmd_dismiss),
            CommandHandler("cancel",      self._cmd_cancel),
            CommandHandler("switch",      self._cmd_switch),
            CommandHandler("auth",        self._cmd_auth),
            CommandHandler("login",       self._cmd_login),
            CallbackQueryHandler(self._handle_callback),
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message),
        ]
        for h in handlers:
            self._app.add_handler(h)

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (V3 session mode)")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_message(self, chat_id: str, text: str, parse_mode: str = "HTML") -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.warning("Failed to send Telegram message to %s: %s", chat_id, e)

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
    # Core: _handle_message
    # ---------------------------------------------------------------------- #

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return

        chat_id = self._chat_id(update)

        if not self._cfg.features.triage_auto and self._session_mgr is not None:
            await self._handle_message_session(update, chat_id, text)
            return

        await self._handle_message_triage(update, chat_id, text)

    async def _handle_message_session(self, update: Update, chat_id: str, text: str) -> None:
        """Route message to active session with streaming response."""
        session = self._session_mgr.get_active(chat_id)

        if session is None:
            primary = self._primary_target()
            if primary is None:
                await update.message.reply_text(
                    "没有活跃会话，也没有配置项目目录。\n"
                    "请先在 vibefairy.toml 中配置 [[targets.projects]]，\n"
                    "或使用 /new <名称> <路径> 创建会话。"
                )
                return
            try:
                session = await self._session_mgr.create_session(
                    name="default",
                    chat_id=chat_id,
                    working_dir=primary.path,
                    backend=self._worker.backend.value if self._worker else "claude",
                )
                await update.message.reply_text(
                    f"已自动创建会话 <b>default</b>\n工作目录: <code>{primary.path}</code>",
                    parse_mode="HTML",
                )
            except Exception as e:
                await update.message.reply_text(f"创建会话失败: {e}")
                return

        if session.is_busy:
            await update.message.reply_text(
                f"会话 <b>{session.name}</b> 正在处理中，请稍等再发消息。",
                parse_mode="HTML",
            )
            return

        reply = await update.message.reply_text("思考中...")

        output_parts: list[str] = []
        last_edit_time = [time.monotonic()]
        edit_interval = self._cfg.session.streaming_edit_interval_secs
        max_len = self._cfg.session.max_message_length

        async def on_chunk(chunk: str) -> None:
            output_parts.append(chunk)
            now = time.monotonic()
            if now - last_edit_time[0] >= edit_interval:
                full = "".join(output_parts)
                display = full[-max_len:] if len(full) > max_len else full
                try:
                    await reply.edit_text(display or "...")
                    last_edit_time[0] = now
                except Exception:
                    pass

        try:
            result = await self._session_mgr.send_message(
                session_name=session.name,
                prompt=text,
                on_chunk=on_chunk,
            )

            full_output = result.output or "".join(output_parts) or "(无回复)"

            if len(full_output) <= max_len:
                await reply.edit_text(full_output)
            else:
                await reply.edit_text(full_output[:max_len])
                remaining = full_output[max_len:]
                while remaining:
                    part = remaining[:max_len]
                    remaining = remaining[max_len:]
                    await update.message.reply_text(part)

            await repo.create_session_message(self._db, SessionMessage(
                id=None, session_id=session.db_id, role="user", content=text,
            ))
            await repo.create_session_message(self._db, SessionMessage(
                id=None, session_id=session.db_id, role="assistant",
                content=full_output, token_count=result.token_count,
            ))

        except Exception as e:
            logger.exception("Session message handling failed for session '%s'", session.name)
            try:
                await reply.edit_text(f"执行出错: {e}")
            except Exception:
                await update.message.reply_text(f"执行出错: {e}")

    # ---------------------------------------------------------------------- #
    # Legacy triage
    # ---------------------------------------------------------------------- #

    async def _handle_message_triage(self, update: Update, chat_id: str, text: str) -> None:
        user_id = self._user_id(update)
        task = Task(
            id=None,
            raw_message=text,
            chat_id=chat_id,
            user_id=user_id,
            source_message_id=update.message.message_id,
        )
        task_id = await repo.create_task(self._db, task)
        await update.message.reply_text(f"收到! Task #{task_id} 已创建，正在分析...")
        if self._triage_fn is not None:
            asyncio.create_task(self._triage_and_notify(task_id, update))

    async def _triage_and_notify(self, task_id: int, update: Update) -> None:
        try:
            await self._triage_fn(task_id)
        except Exception as e:
            logger.exception("Inline triage failed for task #%d", task_id)
            await update.message.reply_text(
                f"Task #{task_id} 分析失败: {e}\n系统将自动重试。"
            )
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            return
        if task.status == "noted":
            await update.message.reply_text(f"Task #{task_id} — 已记录备忘\n{task.summary or ''}")
        elif task.status == "answered":
            answer = task.answer or "(无回答)"
            await update.message.reply_text(f"Task #{task_id} — 回答\n\n{answer[:3800]}")
        elif task.status == "awaiting_user_decision":
            await self._send_decision_card(update.message.reply_text, task)
        else:
            await update.message.reply_text(f"Task #{task_id} 分析结果: {task.status}")

    async def _send_decision_card(self, reply_fn, task: Task) -> None:
        plan = task.plan or "(方案生成中...)"
        card_text = (
            f"<b>Task #{task.id}</b> — {task.summary or task.raw_message[:80]}\n"
            f"Priority: {task.priority or '?'} | Effort: {task.effort or '?'}\n"
            f"Target: {task.target or '未指定'}\n\n"
            f"<b>方案:</b>\n{plan[:1500]}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("批准执行", callback_data=f"approve:{task.id}"),
                InlineKeyboardButton("打回重做", callback_data=f"rework:{task.id}"),
            ],
            [InlineKeyboardButton("取消", callback_data=f"cancel:{task.id}")],
        ])
        await reply_fn(card_text, parse_mode="HTML", reply_markup=keyboard)

    # ---------------------------------------------------------------------- #
    # Callback handler
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
        if action == "switch":
            try:
                target = CLIBackend(id_str.lower())
            except ValueError:
                await query.edit_message_text(f"未知后端: {id_str}")
                return
            await self._do_switch(query, target)
            return
        if action == "login":
            try:
                target = CLIBackend(id_str.lower())
            except ValueError:
                await query.edit_message_text(f"未知后端: {id_str}")
                return
            await query.edit_message_text(f"正在启动 {target.display_name} 登录流程...")
            asyncio.create_task(self._run_login_and_notify(query.message, target))
            return
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
            await query.edit_message_text(f"Task #{task_id} 状态为 '{task.status}'，无法批准。")
            return
        imp = Improvement(
            id=None,
            target=task.target or self._primary_target_name() or "unknown",
            summary=task.summary or task.raw_message[:200],
            detail=task.plan or task.raw_message,
            effort=task.effort, priority=task.priority, status="proposed",
        )
        imp_id = await repo.create_improvement(self._db, imp)
        ttl = self._cfg.approval_default_ttl_minutes
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl)
        approval = Approval(
            id=None, improvement_id=imp_id, approved_by=self._user_id(update),
            chat_id=self._chat_id(update), prompt_snapshot=task.plan or task.raw_message,
            execution_mode="write", ttl_minutes=ttl, expires_at=expires,
        )
        approval_id = await repo.create_approval(self._db, approval)
        await repo.update_improvement_status(self._db, imp_id, "approved")
        await repo.update_task(self._db, task_id, status="approved", decision_needed=False,
                               improvement_id=imp_id, approval_id=approval_id)
        await query.edit_message_text(
            f"Task #{task_id} 已批准，开始执行...\nImprovement #{imp_id} | Approval #{approval_id}"
        )
        asyncio.create_task(self._execute_task(query, task_id, imp, approval_id))

    async def _execute_task(self, query, task_id: int, imp: Improvement, approval_id: int) -> None:
        from vibefairy.engine.policy import ExecutionMode
        from vibefairy.engine.worker import WorkerTask
        await repo.update_task(self._db, task_id, status="executing")
        worker_task = WorkerTask(
            improvement=imp, prompt=imp.detail or imp.summary,
            approval_id=approval_id, requested_mode=ExecutionMode.WRITE,
        )
        try:
            result = await self._worker.execute(worker_task)
            status = "done" if result.success else "failed"
            await repo.update_task(self._db, task_id, status=status, run_id=result.run_id)
            await query.message.reply_text(
                f"<b>Task #{task_id} 执行{'完成' if result.success else '失败'}</b>\n"
                f"状态: {status} | Tokens: {result.token_count:,} | 耗时: {result.duration_secs:.1f}s\n\n"
                f"<code>{(result.output or '')[:500]}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Execution failed for task #%d", task_id)
            await repo.update_task(self._db, task_id, status="failed", last_error=str(e))
            await query.message.reply_text(f"Task #{task_id} 执行出错: {e}")

    async def _callback_rework_task(self, query, task_id: int) -> None:
        task = await repo.get_task(self._db, task_id)
        if task is None:
            await query.edit_message_text(f"Task #{task_id} 不存在。")
            return
        await repo.update_task(self._db, task_id, status="received", decision_needed=False,
                               plan=None, triage_retries=0)
        await query.edit_message_text(f"Task #{task_id} 已打回，重新分析...")
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
    # Session commands
    # ---------------------------------------------------------------------- #

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        chat_id = self._chat_id(update)
        if len(args) == 0:
            primary = self._primary_target()
            if primary is None:
                await update.message.reply_text("用法: /new <名称> <路径>\n例: /new myproject /path/to/project")
                return
            name = f"session_{int(time.time())}"
            path = primary.path
        elif len(args) == 1:
            name = args[0]
            primary = self._primary_target()
            path = primary.path if primary else "."
        else:
            name = args[0]
            path = " ".join(args[1:])

        existing = self._session_mgr.get_session(name)
        if existing and existing.chat_id == chat_id:
            await update.message.reply_text(
                f"会话 '<b>{name}</b>' 已存在。使用 /use {name} 切换到它。",
                parse_mode="HTML",
            )
            return
        if not Path(path).is_dir():
            await update.message.reply_text(f"目录不存在: <code>{path}</code>", parse_mode="HTML")
            return
        backend = self._worker.backend.value if self._worker else "claude"
        try:
            await self._session_mgr.create_session(
                name=name, chat_id=chat_id, working_dir=path, backend=backend,
            )
        except Exception as e:
            await update.message.reply_text(f"创建会话失败: {e}")
            return
        await update.message.reply_text(
            f"会话 <b>{name}</b> 已创建并激活\n工作目录: <code>{path}</code>\n后端: {backend}",
            parse_mode="HTML",
        )

    async def _cmd_sessions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        chat_id = self._chat_id(update)
        sessions = self._session_mgr.list_sessions(chat_id)
        active_name = self._session_mgr._active.get(chat_id)
        if not sessions:
            await update.message.reply_text("暂无会话。使用 /new 创建新会话。")
            return
        lines = ["<b>会话列表</b>"]
        for s in sessions:
            marker = "●" if s.name == active_name else "○"
            model_str = f" | 模型: {s.model}" if s.model else ""
            lines.append(
                f"{marker} <b>{s.name}</b>\n"
                f"  目录: <code>{s.working_dir}</code>\n"
                f"  后端: {s.backend}{model_str}"
            )
        lines.append(f"\n活跃: {active_name or '(无)'}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_use(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("用法: /use <会话名称>")
            return
        name = args[0]
        chat_id = self._chat_id(update)
        ok = await self._session_mgr.switch_active(chat_id, name)
        if ok:
            session = self._session_mgr.get_session(name)
            await update.message.reply_text(
                f"已切换到会话 <b>{name}</b>\n工作目录: <code>{session.working_dir}</code>\n后端: {session.backend}",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(f"会话 '{name}' 不存在或不属于此聊天。\n使用 /sessions 查看可用会话。")

    async def _cmd_close_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("用法: /close <会话名称>")
            return
        name = args[0]
        ok = await self._session_mgr.close_session(name)
        if ok:
            await update.message.reply_text(f"会话 <b>{name}</b> 已关闭。", parse_mode="HTML")
        else:
            await update.message.reply_text(f"会话 '{name}' 不存在。")

    async def _cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("用法: /model <模型名>\n例: /model claude-sonnet-4-6")
            return
        model = args[0]
        chat_id = self._chat_id(update)
        session = self._session_mgr.get_active(chat_id)
        if session is None:
            await update.message.reply_text("没有活跃会话。使用 /new 创建。")
            return
        session.model = model
        await repo.update_session(self._db, session.db_id, model=model)
        await update.message.reply_text(
            f"会话 <b>{session.name}</b> 模型已设为: <code>{model}</code>", parse_mode="HTML"
        )

    async def _cmd_backend(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("用法: /backend <claude|codex>")
            return
        backend = args[0].lower()
        if backend not in ("claude", "codex"):
            await update.message.reply_text("无效后端，可选: claude, codex")
            return
        chat_id = self._chat_id(update)
        session = self._session_mgr.get_active(chat_id)
        if session is None:
            await update.message.reply_text("没有活跃会话。使用 /new 创建。")
            return
        session.backend = backend
        await repo.update_session(self._db, session.db_id, backend=backend)
        await update.message.reply_text(
            f"会话 <b>{session.name}</b> 后端已切换为: <b>{backend}</b>", parse_mode="HTML"
        )

    async def _cmd_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("用法: /cd <路径>")
            return
        path = " ".join(args)
        chat_id = self._chat_id(update)
        session = self._session_mgr.get_active(chat_id)
        if session is None:
            await update.message.reply_text("没有活跃会话。使用 /new 创建。")
            return
        if not Path(path).is_dir():
            await update.message.reply_text(f"目录不存在: <code>{path}</code>", parse_mode="HTML")
            return
        session.working_dir = path
        await repo.update_session(self._db, session.db_id, working_dir=path)
        await update.message.reply_text(
            f"会话 <b>{session.name}</b> 工作目录已更改为:\n<code>{path}</code>", parse_mode="HTML"
        )

    async def _cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._session_mgr is None:
            await update.message.reply_text("Session manager 未初始化。")
            return
        args = ctx.args or []
        n = 10
        if args:
            try:
                n = min(max(1, int(args[0])), 50)
            except ValueError:
                pass
        chat_id = self._chat_id(update)
        session = self._session_mgr.get_active(chat_id)
        if session is None:
            await update.message.reply_text("没有活跃会话。")
            return
        messages = await repo.list_session_messages(self._db, session.db_id, limit=n)
        if not messages:
            await update.message.reply_text("暂无消息历史。")
            return
        lines = [f"<b>会话 {session.name} — 最近 {n} 条消息</b>"]
        for msg in messages:
            role_display = "用户" if msg.role == "user" else "助手"
            content_preview = msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
            lines.append(f"\n<b>[{role_display}]</b>\n{content_preview}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # ---------------------------------------------------------------------- #
    # System commands
    # ---------------------------------------------------------------------- #

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        mode = "分拣模式（旧）" if self._cfg.features.triage_auto else "会话模式"
        text = (
            f"<b>VibeFairy V3</b> — 远程 AI 终端  [{mode}]\n\n"
            "直接发送消息 → 转发给活跃会话的 AI 后端\n\n"
            "<b>会话管理:</b>\n"
            "/new [name] [path] — 创建新会话\n"
            "/sessions — 查看所有会话\n"
            "/use &lt;name&gt; — 切换活跃会话\n"
            "/close &lt;name&gt; — 关闭会话\n"
            "/model &lt;model&gt; — 切换模型\n"
            "/backend &lt;claude|codex&gt; — 切换后端\n"
            "/cd &lt;path&gt; — 切换工作目录\n"
            "/history [n] — 查看消息历史\n\n"
            "<b>系统:</b>\n"
            "/status — 系统状态\n"
            "/budget — Token 用量\n"
            "/targets — 管理的项目\n"
            "/reload — 重新加载配置\n\n"
            "<b>传统任务流程:</b>\n"
            "/triage &lt;message&gt; — 显式分拣消息\n"
            "/list — 任务看板\n"
            "/approve &lt;id&gt; — 批准任务\n"
            "/reject &lt;id&gt; — 拒绝任务\n"
            "/scout — 触发发现\n"
            "/switch [claude|codex] — 切换全局后端\n"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        uptime = datetime.now(tz=timezone.utc) - self._start_time
        uptime_str = str(uptime).split(".")[0]
        today_tokens = await repo.get_today_token_count(self._db)
        daily_limit = self._cfg.budget.daily_token_limit
        budget_pct = (today_tokens / daily_limit * 100) if daily_limit > 0 else 0
        counts = await repo.count_tasks_by_status(self._db)
        chat_id = self._chat_id(update)
        session_lines: list[str] = []
        if self._session_mgr is not None:
            active = self._session_mgr.get_active(chat_id)
            all_s = self._session_mgr.list_sessions(chat_id)
            if active:
                session_lines.append(f"活跃会话: <b>{active.name}</b> ({active.backend})")
                session_lines.append(f"工作目录: <code>{active.working_dir}</code>")
            session_lines.append(f"会话总数: {len(all_s)}")

        # Query auth status for both backends (if auth_manager available)
        auth_buttons: list[InlineKeyboardButton] = []
        if self._auth_manager is not None:
            for backend in (CLIBackend.CLAUDE, CLIBackend.CODEX):
                try:
                    st = await self._auth_manager.check_auth(backend)
                    icon = "已登录" if st.logged_in else "未绑定"
                    if not st.logged_in:
                        auth_buttons.append(
                            InlineKeyboardButton(
                                f"一键绑定 {backend.display_name}",
                                callback_data=f"login:{backend.value}",
                            )
                        )
                except Exception:
                    icon = "?"
                    auth_buttons.append(
                        InlineKeyboardButton(
                            f"一键绑定 {backend.display_name}",
                            callback_data=f"login:{backend.value}",
                        )
                    )

        lines = [
            "<b>VibeFairy 状态</b>",
            f"运行时间: {uptime_str}",
            f"全局后端: <b>{self._worker.backend.display_name}</b>",
            f"消息模式: {'分拣模式' if self._cfg.features.triage_auto else '会话模式'}",
            f"预算: {today_tokens:,}/{daily_limit:,} tokens ({budget_pct:.1f}%)",
            "",
        ]
        if session_lines:
            lines.extend(session_lines)
            lines.append("")
        lines.extend([
            "<b>任务看板</b>",
            f"  待分拣: {counts.get('received', 0) + counts.get('triaging', 0)}",
            f"  待确认: {counts.get('awaiting_user_decision', 0)}",
            f"  执行中: {counts.get('executing', 0)}",
        ])

        keyboard = None
        if auth_buttons:
            lines.append("\n未绑定的后端，点击一键完成认证:")
            keyboard = InlineKeyboardMarkup([auth_buttons])

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _cmd_reload(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._reload_fn is None:
            await update.message.reply_text("重载功能未配置。")
            return
        try:
            await self._reload_fn()
            await update.message.reply_text("配置已重新加载。")
        except Exception as e:
            await update.message.reply_text(f"配置重载失败: {e}")

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
        pct = (today_tokens / daily_limit * 100) if daily_limit > 0 else 0
        over = self._policy.is_over_budget
        icon = "[超限]" if over else ("[警告]" if pct > 75 else "[正常]")
        await update.message.reply_text(
            f"{icon} <b>今日 Token 预算</b>\n"
            f"已用: {today_tokens:,}\n"
            f"每日上限: {daily_limit:,} ({pct:.1f}%)\n"
            f"单任务上限: {self._cfg.budget.per_task_token_limit:,}\n"
            f"状态: {'超限' if over else '正常'}",
            parse_mode="HTML",
        )

    # ---------------------------------------------------------------------- #
    # Legacy task commands
    # ---------------------------------------------------------------------- #

    async def _cmd_triage(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("用法: /triage <消息内容>")
            return
        text = " ".join(args)
        chat_id = self._chat_id(update)
        task = Task(
            id=None, raw_message=text, chat_id=chat_id, user_id=self._user_id(update),
            source_message_id=update.message.message_id,
        )
        task_id = await repo.create_task(self._db, task)
        await update.message.reply_text(f"Task #{task_id} 已创建，正在分析...")
        if self._triage_fn is not None:
            asyncio.create_task(self._triage_and_notify(task_id, update))
        else:
            await update.message.reply_text(
                "分拣代理未配置。在 vibefairy.toml 中设置 features.triage_auto = true 后重启。"
            )

    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        active_statuses = ["received", "triaging", "planned", "awaiting_user_decision", "approved", "executing"]
        tasks = await repo.list_tasks_by_statuses(self._db, active_statuses, limit=20)
        action_tasks = [t for t in tasks if t.kind in ("action", "unknown")]
        pending = [t for t in action_tasks if t.status == "awaiting_user_decision"]
        in_progress = [t for t in action_tasks if t.status in ("approved", "executing")]
        queued = [t for t in action_tasks if t.status in ("received", "triaging", "planned")]
        lines = ["<b>任务看板</b>"]
        if pending:
            lines.append(f"\n<b>待确认 ({len(pending)})</b>")
            for t in pending[:5]:
                lines.append(f"  #{t.id} [{t.priority or '?'}/{t.effort or '?'}] {(t.summary or t.raw_message)[:60]}")
        if in_progress:
            lines.append(f"\n<b>执行中 ({len(in_progress)})</b>")
            for t in in_progress[:5]:
                lines.append(f"  #{t.id} [{t.status}] {(t.summary or t.raw_message)[:60]}")
        if queued:
            lines.append(f"\n<b>队列中 ({len(queued)})</b>")
            for t in queued[:5]:
                lines.append(f"  #{t.id} [{t.status}] {(t.summary or t.raw_message)[:60]}")
        if not action_tasks:
            lines.append("\n暂无活跃任务。")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_done(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
            lines.append(f"  #{t.id} [{t.priority or '?'}] {(t.summary or t.raw_message)[:70]}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_approve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /approve <task_id>")
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
        if task.status != "awaiting_user_decision":
            await update.message.reply_text(f"Task #{task_id} 当前状态为 '{task.status}'，不能批准。")
            return
        await update.message.reply_text(f"Task #{task_id} 批准中...")
        await self._approve_task_by_id(task_id, update)

    async def _approve_task_by_id(self, task_id: int, update: Update) -> None:
        task = await repo.get_task(self._db, task_id)
        if task is None:
            return
        imp = Improvement(
            id=None, target=task.target or self._primary_target_name() or "unknown",
            summary=task.summary or task.raw_message[:200], detail=task.plan or task.raw_message,
            effort=task.effort, priority=task.priority, status="proposed",
        )
        imp_id = await repo.create_improvement(self._db, imp)
        ttl = self._cfg.approval_default_ttl_minutes
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl)
        approval = Approval(
            id=None, improvement_id=imp_id, approved_by=self._user_id(update),
            chat_id=self._chat_id(update), prompt_snapshot=task.plan or task.raw_message,
            execution_mode="write", ttl_minutes=ttl, expires_at=expires,
        )
        approval_id = await repo.create_approval(self._db, approval)
        await repo.update_improvement_status(self._db, imp_id, "approved")
        await repo.update_task(self._db, task_id, status="approved", decision_needed=False,
                               improvement_id=imp_id, approval_id=approval_id)
        asyncio.create_task(self._execute_task_text(task_id, task, imp, approval_id, update))

    async def _execute_task_text(
        self, task_id: int, task: Task, imp: Improvement, approval_id: int, update: Update
    ) -> None:
        from vibefairy.engine.policy import ExecutionMode
        from vibefairy.engine.worker import WorkerTask
        await repo.update_task(self._db, task_id, status="executing")
        worker_task = WorkerTask(
            improvement=imp, prompt=imp.detail or imp.summary,
            approval_id=approval_id, requested_mode=ExecutionMode.WRITE,
        )
        try:
            result = await self._worker.execute(worker_task)
            status = "done" if result.success else "failed"
            await repo.update_task(self._db, task_id, status=status, run_id=result.run_id)
            await update.message.reply_text(
                f"<b>Task #{task_id} 执行{'完成' if result.success else '失败'}</b>\n"
                f"状态: {status} | Tokens: {result.token_count:,} | 耗时: {result.duration_secs:.1f}s\n\n"
                f"<code>{(result.output or '')[:500]}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Text-command execution failed for task #%d", task_id)
            await repo.update_task(self._db, task_id, status="failed", last_error=str(e))
            await update.message.reply_text(f"Task #{task_id} 执行出错: {e}")

    async def _cmd_approve_imp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
            await update.message.reply_text(f"Improvement #{imp_id} 状态为 '{imp.status}'，无法批准。")
            return
        ttl = self._cfg.approval_default_ttl_minutes
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl)
        approval = Approval(
            id=None, improvement_id=imp_id, approved_by=self._user_id(update),
            chat_id=self._chat_id(update), prompt_snapshot=imp.detail or imp.summary,
            execution_mode="write", ttl_minutes=ttl, expires_at=expires,
        )
        approval_id = await repo.create_approval(self._db, approval)
        await repo.update_improvement_status(self._db, imp_id, "approved")
        await update.message.reply_text(f"Improvement #{imp_id} 已批准 (approval #{approval_id})，执行中...")
        asyncio.create_task(self._execute_improvement(update, imp, approval_id))

    async def _execute_improvement(self, update: Update, imp: Improvement, approval_id: int) -> None:
        from vibefairy.engine.policy import ExecutionMode
        from vibefairy.engine.worker import WorkerTask
        worker_task = WorkerTask(
            improvement=imp, prompt=imp.detail or imp.summary,
            approval_id=approval_id, requested_mode=ExecutionMode.WRITE,
        )
        try:
            result = await self._worker.execute(worker_task)
            status = "applied" if result.success else "failed"
            await update.message.reply_text(
                f"Improvement #{imp.id} {status} (run #{result.run_id})\n"
                f"Tokens: {result.token_count:,} | Duration: {result.duration_secs:.1f}s\n\n"
                f"<code>{(result.output or '')[:500]}</code>",
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
            await update.message.reply_text(f"Task #{task_id} 不存在。")
            return
        await repo.update_task(self._db, task_id, status="cancelled", decision_needed=False)
        await repo.log_event(self._db, "human_action", source="telegram",
                             detail=f"rejected task #{task_id} by {self._user_id(update)}")
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
            await update.message.reply_text(f"Task #{task_id} 状态为 '{task.status}'，只有 failed/dead_letter 可以重试。")
            return
        await repo.update_task(self._db, task_id, status="received", last_error=None,
                               triage_retries=0, execute_retries=0)
        await update.message.reply_text(f"Task #{task_id} 已重置，将自动重新分析。")

    async def _cmd_dismiss(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        await repo.log_event(self._db, "human_action", source="telegram",
                             detail=f"dismissed improvement #{imp_id} by {self._user_id(update)}")
        await update.message.reply_text(f"Improvement #{imp_id} 已清除。")

    async def _cmd_scout(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth_check(update):
            return
        if self._scout_trigger is None:
            await update.message.reply_text(
                "Scout 未启用。在 vibefairy.toml 中设置 features.scout_enabled = true 后重启。"
            )
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
            lines.append(f"    Target: {imp.target} | /approve_imp {imp.id}")
        if not discoveries and not improvements:
            lines.append("暂无数据。运行 /scout 开始发现。")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch global CLI backend. Usage: /switch [claude|codex]"""
        if not await self._auth_check(update):
            return
        args = ctx.args or []
        current = self._worker.backend
        if not args:
            other = CLIBackend.CODEX if current == CLIBackend.CLAUDE else CLIBackend.CLAUDE

            # Build per-backend status rows with switch + login buttons
            kbd_rows: list[list[InlineKeyboardButton]] = []
            for backend in (CLIBackend.CLAUDE, CLIBackend.CODEX):
                row: list[InlineKeyboardButton] = []
                if backend != current:
                    row.append(InlineKeyboardButton(
                        f"切换到 {backend.display_name}",
                        callback_data=f"switch:{backend.value}",
                    ))
                if self._auth_manager is not None:
                    try:
                        st = await self._auth_manager.check_auth(backend)
                        auth_label = "已绑定" if st.logged_in else "一键绑定"
                    except Exception:
                        auth_label = "一键绑定"
                    if auth_label == "一键绑定":
                        row.append(InlineKeyboardButton(
                            f"一键绑定 {backend.display_name}",
                            callback_data=f"login:{backend.value}",
                        ))
                if row:
                    kbd_rows.append(row)

            keyboard = InlineKeyboardMarkup(kbd_rows) if kbd_rows else None
            await update.message.reply_text(
                f"当前全局后端: <b>{current.display_name}</b>\n\n"
                f"注: /switch 切换全局后端（影响传统任务流程）\n"
                f"会话级后端请使用 /backend 命令\n\n"
                f"未绑定的后端可点击下方按钮一键完成认证:",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        target_str = args[0].lower()
        try:
            target = CLIBackend(target_str)
        except ValueError:
            await update.message.reply_text(
                f"未知后端: <code>{target_str}</code>\n可选值: claude, codex", parse_mode="HTML"
            )
            return
        if target == current:
            await update.message.reply_text(
                f"当前已是 <b>{current.display_name}</b>，无需切换。", parse_mode="HTML"
            )
            return
        await self._do_switch(update, target)

    async def _do_switch(self, update_or_query, target: CLIBackend) -> None:
        if self._switch_fn:
            await self._switch_fn(target)
        old_name = (CLIBackend.CLAUDE if target == CLIBackend.CODEX else CLIBackend.CODEX).display_name
        msg = f"全局执行后端已切换: <b>{old_name}</b> → <b>{target.display_name}</b>"
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(msg, parse_mode="HTML")
        elif hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(msg, parse_mode="HTML")

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
