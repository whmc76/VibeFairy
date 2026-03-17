"""Advisor agent — generates concrete improvement proposals.

Takes analyzed discoveries and produces structured Improvement records
with specific, actionable recommendations.
"""

from __future__ import annotations

import logging

import aiosqlite

from vibefairy.config.loader import DaemonConfig
from vibefairy.config.secrets import Secrets
from vibefairy.engine.claude_session import ClaudeSession
from vibefairy.memory import repo
from vibefairy.memory.models import Discovery, Improvement

logger = logging.getLogger(__name__)


class Advisor:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db

    async def generate_proposals(self, discovery: Discovery) -> list[Improvement]:
        """Generate improvement proposals from an analyzed discovery."""
        target = self._primary_target()
        if not target:
            return []

        session = ClaudeSession(working_dir=target.path)

        prompt = (
            f"Based on this repository, generate 3-5 specific improvement proposals.\n\n"
            f"Source repository: {discovery.title}\n"
            f"Description: {discovery.description}\n"
            f"URL: {discovery.url}\n\n"
            f"Target project: {target.name}\n"
            f"Target path: {target.path}\n\n"
            f"For each proposal, output exactly this format (one line per proposal):\n"
            f"PRIORITY | EFFORT | SUMMARY | DETAIL\n\n"
            f"Where:\n"
            f"  PRIORITY = P0, P1, P2, or P3\n"
            f"  EFFORT = S (hours), M (days), L (weeks)\n"
            f"  SUMMARY = one line, max 100 chars\n"
            f"  DETAIL = 2-3 sentences describing what to implement\n\n"
            f"Only output the proposals, nothing else."
        )

        result = await session.run_readonly(prompt, timeout_secs=60)
        improvements: list[Improvement] = []

        for line in result.output.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            priority = parts[0] if parts[0] in ("P0", "P1", "P2", "P3") else "P2"
            effort = parts[1] if parts[1] in ("S", "M", "L") else "M"
            summary = parts[2][:200] if len(parts) > 2 else ""
            detail = parts[3] if len(parts) > 3 else summary

            if not summary:
                continue

            imp = Improvement(
                id=None,
                discovery_id=discovery.id,
                target=target.name,
                summary=summary,
                detail=detail,
                effort=effort,
                priority=priority,
                status="proposed",
            )
            imp_id = await repo.create_improvement(self._db, imp)
            imp.id = imp_id
            improvements.append(imp)

        await repo.update_discovery_status(self._db, discovery.id, "proposed")
        logger.info("Advisor: generated %d proposals from discovery %d", len(improvements), discovery.id)
        return improvements

    def _primary_target(self):
        for t in self._cfg.targets:
            if t.primary:
                return t
        return self._cfg.targets[0] if self._cfg.targets else None
