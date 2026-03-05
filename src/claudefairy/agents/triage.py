"""TriageAgent — classify incoming messages and generate plans/answers.

Three-category classification:
  note     — chat / memo / emotional expression / no clear intent → close immediately
  question — request for information / explanation / analysis    → answer with Claude (readonly)
  action   — request to change code / add feature / fix bug      → generate plan → await approval

All classification and answering uses ClaudeSession.run_readonly (no write budget consumed).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import aiosqlite

from claudefairy.config.loader import DaemonConfig, TriageConfig
from claudefairy.config.secrets import Secrets
from claudefairy.engine.claude_session import ClaudeSession
from claudefairy.memory import repo
from claudefairy.memory.models import Task

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

_CLASSIFY_PROMPT = """You are a task classifier for an autonomous AI assistant.

Classify the following user message into exactly one category:

- note: casual chat, memo, emotional expression, greetings, or no clear actionable intent
- question: request for information, explanation, analysis, or understanding something
- action: explicit request to change code, add a feature, fix a bug, refactor, or do something concrete

User message:
---
{message}
---

Reply ONLY with a JSON object like this (no markdown, no extra text):
{{"kind": "note"|"question"|"action", "summary": "<one sentence, max 80 chars>", "target": "<project name or null>", "priority": "P0"|"P1"|"P2"|"P3"|null, "effort": "S"|"M"|"L"|null}}

Rules:
- kind must be exactly one of: note, question, action
- summary is required (max 80 chars)
- target, priority, effort are optional (null if not determinable)
- priority: P0=critical/blocking, P1=important, P2=normal, P3=nice-to-have
- effort: S=<1h, M=1-4h, L=>4h
"""

_ANSWER_PROMPT = """You are a helpful AI assistant. The user asked:

{message}

Project context: Working directory is {working_dir}.

Please provide a concise, accurate answer. Be direct and practical.
"""

_PLAN_PROMPT = """You are a senior software engineer. The user has requested:

{message}

Project context: Working directory is {working_dir}.

Generate a concrete implementation plan. Format:

1. [Step description]
2. [Step description]
...

Keep each step specific and actionable. Max 5 steps. No code blocks unless essential for clarity.
Also assess: Priority (P0-P3) and Effort (S/M/L) if not already determined.
"""


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

@dataclass
class TriageResult:
    kind: str                       # note / question / action
    summary: str
    target: str | None
    priority: str | None
    effort: str | None
    answer: str | None              # populated for question
    plan: str | None                # populated for action


# --------------------------------------------------------------------------- #
# TriageAgent
# --------------------------------------------------------------------------- #

class TriageAgent:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db
        self._triage_cfg: TriageConfig = cfg.triage

    def _primary_target_path(self) -> str:
        for t in self._cfg.targets:
            if t.primary:
                return t.path
        return self._cfg.targets[0].path if self._cfg.targets else "."

    def _primary_target_name(self) -> str | None:
        for t in self._cfg.targets:
            if t.primary:
                return t.name
        return self._cfg.targets[0].name if self._cfg.targets else None

    async def triage(self, task: Task) -> TriageResult:
        """Full triage pipeline for a task.

        1. Classify message (note / question / action)
        2. For question: generate answer
        3. For action: generate plan
        """
        working_dir = self._primary_target_path()
        session = ClaudeSession(
            working_dir=working_dir,
            model=self._triage_cfg.model,
        )

        # Step 1: classify
        classify_prompt = _CLASSIFY_PROMPT.format(message=task.raw_message)
        classify_result = await session.run_readonly(
            classify_prompt,
            timeout_secs=self._triage_cfg.timeout_secs,
        )
        kind, summary, target, priority, effort = self._parse_classification(
            classify_result.output, task
        )

        answer: str | None = None
        plan: str | None = None

        if kind == "question":
            answer_prompt = _ANSWER_PROMPT.format(
                message=task.raw_message,
                working_dir=working_dir,
            )
            answer_result = await session.run_readonly(
                answer_prompt,
                timeout_secs=self._triage_cfg.timeout_secs,
            )
            answer = answer_result.output[:3000] if answer_result.output else "(no answer)"

        elif kind == "action":
            plan_prompt = _PLAN_PROMPT.format(
                message=task.raw_message,
                working_dir=working_dir,
            )
            plan_result = await session.run_readonly(
                plan_prompt,
                timeout_secs=self._triage_cfg.timeout_secs,
            )
            plan = plan_result.output[:2000] if plan_result.output else "(no plan)"

        if target is None:
            target = self._primary_target_name()

        return TriageResult(
            kind=kind,
            summary=summary,
            target=target,
            priority=priority,
            effort=effort,
            answer=answer,
            plan=plan,
        )

    def _parse_classification(
        self, output: str, task: Task
    ) -> tuple[str, str, str | None, str | None, str | None]:
        """Parse Claude's JSON classification response.

        Returns (kind, summary, target, priority, effort).
        Falls back gracefully on parse error.
        """
        # Extract JSON from output (Claude may wrap in markdown)
        json_match = re.search(r"\{[^{}]+\}", output, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                kind = data.get("kind", "note")
                if kind not in ("note", "question", "action"):
                    kind = "note"
                summary = str(data.get("summary", task.raw_message[:80]))[:80]
                target = data.get("target") or None
                priority = data.get("priority") or None
                effort = data.get("effort") or None
                return kind, summary, target, priority, effort
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Failed to parse triage classification JSON: %s", output[:200])

        # Fallback: best-effort text scan
        lower = output.lower()
        if "action" in lower:
            kind = "action"
        elif "question" in lower:
            kind = "question"
        else:
            kind = "note"
        return kind, task.raw_message[:80], None, None, None

    async def process_task(self, task_id: int) -> None:
        """Claim and process a single task. Idempotent via claim_for_triage.

        This is the main entry point called by both:
        - _handle_message (inline, immediately after creation)
        - Scheduler (periodic scan for unclaimed tasks)
        """
        claimed = await repo.claim_for_triage(self._db, task_id)
        if not claimed:
            logger.debug("Task #%d already claimed or not in 'received' state", task_id)
            return

        task = await repo.get_task(self._db, task_id)
        if task is None:
            logger.error("Task #%d not found after claim", task_id)
            return

        logger.info("Triaging task #%d: %s", task_id, task.raw_message[:60])

        try:
            result = await self.triage(task)
        except Exception as e:
            logger.exception("Triage failed for task #%d", task_id)
            new_retries = task.triage_retries + 1
            if new_retries >= self._triage_cfg.max_retries:
                await repo.update_task(
                    self._db, task_id,
                    status="failed",
                    last_error=f"triage error after {new_retries} retries: {e}",
                    triage_retries=new_retries,
                )
            else:
                await repo.update_task(
                    self._db, task_id,
                    status="received",  # back to received so scheduler can retry
                    last_error=str(e),
                    triage_retries=new_retries,
                )
            return

        # Update task with triage results
        if result.kind == "note":
            await repo.update_task(
                self._db, task_id,
                kind="note",
                summary=result.summary,
                target=result.target,
                status="noted",
            )
        elif result.kind == "question":
            await repo.update_task(
                self._db, task_id,
                kind="question",
                summary=result.summary,
                target=result.target,
                priority=result.priority,
                effort=result.effort,
                answer=result.answer,
                status="answered",
            )
        else:  # action
            await repo.update_task(
                self._db, task_id,
                kind="action",
                summary=result.summary,
                target=result.target,
                priority=result.priority,
                effort=result.effort,
                plan=result.plan,
                decision_needed=True,
                status="awaiting_user_decision",
            )

        logger.info(
            "Task #%d triaged → kind=%s status=%s",
            task_id, result.kind,
            "noted" if result.kind == "note" else
            "answered" if result.kind == "question" else
            "awaiting_user_decision",
        )

    async def process_pending_queue(self) -> int:
        """Scan for 'received' tasks and attempt to claim+triage each one.

        Called by the scheduler periodically. Returns count processed.
        """
        tasks = await repo.list_received_tasks(self._db)
        count = 0
        for task in tasks:
            try:
                await self.process_task(task.id)
                count += 1
            except Exception:
                logger.exception("Scheduler triage failed for task #%d", task.id)
        return count
