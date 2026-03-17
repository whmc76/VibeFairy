"""Analyst agent — deep analysis of discovered items.

Takes a Discovery in 'discovered' state and produces:
- Detailed technical analysis
- Relevance assessment relative to target project
- Specific file/function-level improvement candidates
"""

from __future__ import annotations

import logging

import aiosqlite

from vibefairy.config.loader import DaemonConfig
from vibefairy.config.secrets import Secrets
from vibefairy.engine.claude_session import ClaudeSession
from vibefairy.memory import repo
from vibefairy.memory.models import Discovery

logger = logging.getLogger(__name__)


class Analyst:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db

    async def analyze(self, discovery: Discovery) -> str:
        """Run deep analysis on a discovery. Returns analysis text."""
        target = self._primary_target()
        if not target:
            return "No target configured"

        session = ClaudeSession(working_dir=target.path)

        prompt = (
            f"Perform a technical analysis of this repository for relevance to our codebase.\n\n"
            f"Repository: {discovery.title}\n"
            f"URL: {discovery.url}\n"
            f"Description: {discovery.description}\n"
            f"Tags: {', '.join(discovery.tags)}\n\n"
            f"Our project: {target.name} — {target.description}\n\n"
            f"Analyze:\n"
            f"1. Key technical patterns or innovations\n"
            f"2. Specific APIs or approaches we could adopt\n"
            f"3. Integration difficulty (S/M/L)\n"
            f"4. Potential risks\n"
            f"5. Priority recommendation (P0-P3)\n\n"
            f"Be concrete and reference specific files/modules if possible."
        )

        result = await session.run_readonly(prompt, timeout_secs=90)
        analysis = result.output

        await repo.update_discovery_status(self._db, discovery.id, "analyzed")
        logger.info("Analyst: analyzed discovery %d", discovery.id)
        return analysis

    def _primary_target(self):
        for t in self._cfg.targets:
            if t.primary:
                return t
        return self._cfg.targets[0] if self._cfg.targets else None
