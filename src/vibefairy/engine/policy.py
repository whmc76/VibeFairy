"""Security policy engine + cost gate.

Responsibilities:
- Detect dangerous prompt patterns before execution
- Check daily/per-task token budgets
- Determine execution mode permission
- Manage over-budget degradation
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum

import aiosqlite

from vibefairy.config.loader import BudgetConfig, DaemonConfig
from vibefairy.memory import repo

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    READONLY = "readonly"
    WRITE = "write"
    DANGEROUS = "dangerous"


# Patterns that trigger mandatory human review before execution
DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"rm\s+-rf",
        r"git\s+push.*--force",
        r"drop\s+table",
        r"delete.*branch",
        r"format.*disk",
        r"curl.*\|.*sh",
        r"wget.*\|.*sh",
        r"eval\s*\(",
        r"os\.system\s*\(",
        r"subprocess\.call\s*\(\s*['\"]rm",
        r"truncate\s+table",
        r"drop\s+database",
        r"fdisk",
        r"mkfs",
        r":\s*\(\s*\)\s*\{.*\}",   # fork bomb
    ]
]


@dataclass
class PolicyResult:
    allowed: bool
    reason: str
    warnings: list[str]
    prompt_hash: str
    suggested_mode: ExecutionMode


class PolicyEngine:
    def __init__(self, cfg: DaemonConfig, db: aiosqlite.Connection):
        self._cfg = cfg
        self._db = db
        self._over_budget = False

    async def evaluate(
        self,
        prompt: str,
        requested_mode: ExecutionMode,
        approval_id: int | None = None,
        improvement_id: int | None = None,
        target: str = "",
    ) -> PolicyResult:
        """Evaluate whether a prompt+mode combination is allowed to execute.

        Returns a PolicyResult describing the decision.
        """
        warnings: list[str] = []
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

        # 1. Dangerous pattern check
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(prompt):
                warnings.append(f"Dangerous pattern detected: {pattern.pattern!r}")

        if warnings:
            return PolicyResult(
                allowed=False,
                reason="Dangerous patterns require explicit /approve with WRITE mode",
                warnings=warnings,
                prompt_hash=prompt_hash,
                suggested_mode=ExecutionMode.WRITE,
            )

        # 2. Budget check
        today_tokens = await repo.get_today_token_count(self._db)
        daily_limit = self._cfg.budget.daily_token_limit
        if daily_limit > 0 and today_tokens >= daily_limit:
            self._over_budget = True
            await repo.log_event(
                self._db,
                "budget_warn",
                source="policy",
                detail=f"Daily token limit reached: {today_tokens}/{daily_limit}",
            )
            mode = self._cfg.budget.over_budget_mode
            if mode != "alert_only":
                return PolicyResult(
                    allowed=False,
                    reason=f"Daily token budget exhausted ({today_tokens}/{daily_limit}). Mode: {mode}",
                    warnings=["over_daily_budget"],
                    prompt_hash=prompt_hash,
                    suggested_mode=ExecutionMode.READONLY,
                )

        # 3. Write-mode requires valid approval
        if requested_mode == ExecutionMode.WRITE:
            if approval_id is None or improvement_id is None:
                return PolicyResult(
                    allowed=False,
                    reason="WRITE mode requires a valid approval_id",
                    warnings=[],
                    prompt_hash=prompt_hash,
                    suggested_mode=ExecutionMode.READONLY,
                )
            valid, reason = await repo.is_approval_valid(
                self._db, approval_id, target, improvement_id
            )
            if not valid:
                return PolicyResult(
                    allowed=False,
                    reason=f"Approval invalid: {reason}",
                    warnings=[],
                    prompt_hash=prompt_hash,
                    suggested_mode=ExecutionMode.READONLY,
                )

        # 4. DANGEROUS mode is never auto-approved
        if requested_mode == ExecutionMode.DANGEROUS:
            return PolicyResult(
                allowed=False,
                reason="DANGEROUS mode requires manual /force command (not implemented for safety)",
                warnings=["dangerous_mode_blocked"],
                prompt_hash=prompt_hash,
                suggested_mode=ExecutionMode.READONLY,
            )

        return PolicyResult(
            allowed=True,
            reason="ok",
            warnings=warnings,
            prompt_hash=prompt_hash,
            suggested_mode=requested_mode,
        )

    def scan_for_hardcoded_secrets(self, text: str) -> list[str]:
        """Scan text for patterns that look like hardcoded secrets.

        Used in audit/review agent prompts.
        """
        patterns = [
            (re.compile(r'sk-ant-[a-zA-Z0-9\-_]{20,}'), "Anthropic API key"),
            (re.compile(r'ghp_[a-zA-Z0-9]{36}'), "GitHub PAT"),
            (re.compile(r'AKIA[0-9A-Z]{16}'), "AWS Access Key"),
            (re.compile(r'["\']password["\']\s*[:=]\s*["\'][^"\']{8,}'), "hardcoded password"),
            (re.compile(r'bot\d{9,10}:[A-Za-z0-9_\-]{35}'), "Telegram bot token"),
        ]
        found: list[str] = []
        for pat, label in patterns:
            if pat.search(text):
                found.append(label)
        return found

    @property
    def is_over_budget(self) -> bool:
        return self._over_budget
