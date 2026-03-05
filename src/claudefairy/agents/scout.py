"""Scout agent — layered discovery pipeline.

Layer 1: Lightweight filtering (no LLM, zero cost)
  - URL dedup (SQLite)
  - Language filter
  - Star threshold
  - Keyword match in title/description

Layer 2: LLM quick score (~200 tokens/item)
  - title + description + language + stars → score 0-10 + one-line reason

Layer 3: Deep analysis (~2000 tokens/item)
  - Read README + key files
  - Compare to target project
  - Generate full analysis + improvement suggestions
  - score >= 8 → immediate notification
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
import aiosqlite

from claudefairy.config.loader import DaemonConfig, ScoutConfig
from claudefairy.config.secrets import Secrets
from claudefairy.engine.claude_session import ClaudeSession
from claudefairy.memory import repo
from claudefairy.memory.models import Discovery, Improvement

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


@dataclass
class RawRepo:
    url: str
    title: str
    description: str
    language: str
    stars: int
    readme_url: str | None = None


class Scout:
    def __init__(
        self,
        cfg: DaemonConfig,
        secrets: Secrets,
        db: aiosqlite.Connection,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._db = db
        self._scout_cfg = cfg.scout
        self._notify_callbacks: list = []

    def add_notify_callback(self, fn) -> None:
        """Register a callback (text: str) → None for high-score discoveries."""
        self._notify_callbacks.append(fn)

    async def run_round(self) -> list[Discovery]:
        """Execute a full scout round. Returns newly created discoveries."""
        logger.info("Scout: starting discovery round")

        candidates = await self._fetch_github_search()
        logger.info("Scout: fetched %d raw candidates", len(candidates))

        # Layer 1
        l1_pass = self._layer1_filter(candidates)
        logger.info("Scout: L1 pass: %d/%d", len(l1_pass), len(candidates))

        # Layer 2
        l2_pass = await self._layer2_score(l1_pass)
        logger.info("Scout: L2 pass: %d/%d", len(l2_pass), len(l1_pass))

        # Layer 3 + persist
        results: list[Discovery] = []
        for repo_info, score, reason in l2_pass:
            discovery, is_new = await self._persist_discovery(repo_info, score)
            if not is_new:
                continue

            results.append(discovery)
            if score >= self._scout_cfg.l3_notify_score:
                await self._layer3_analyze(discovery, repo_info)
                for cb in self._notify_callbacks:
                    try:
                        await cb(
                            f"New high-score discovery (score={score:.1f}):\n"
                            f"{repo_info.title}\n{repo_info.url}\n{reason}"
                        )
                    except Exception:
                        pass

        logger.info("Scout: round complete, %d new discoveries", len(results))
        return results

    # ---------------------------------------------------------------------- #
    # Layer 1: lightweight filter
    # ---------------------------------------------------------------------- #

    def _layer1_filter(self, repos: list[RawRepo]) -> list[RawRepo]:
        passed: list[RawRepo] = []
        sc = self._scout_cfg
        for r in repos:
            # Language filter
            if sc.languages and r.language.lower() not in [l.lower() for l in sc.languages]:
                continue
            # Star threshold
            if r.stars < sc.min_stars_search:
                continue
            # Keyword match
            if sc.keywords:
                text = f"{r.title} {r.description}".lower()
                if not any(kw.lower() in text for kw in sc.keywords):
                    continue
            passed.append(r)
        return passed

    # ---------------------------------------------------------------------- #
    # Layer 2: LLM quick score
    # ---------------------------------------------------------------------- #

    async def _layer2_score(
        self, repos: list[RawRepo]
    ) -> list[tuple[RawRepo, float, str]]:
        """Return repos that pass L2 threshold, with (repo, score, reason)."""
        if not repos:
            return []

        target_desc = self._target_description()
        session = ClaudeSession(working_dir=".")

        # Batch all repos in a single prompt to minimize API calls
        items_text = "\n".join(
            f"{i+1}. [{r.language}★{r.stars}] {r.title}: {r.description[:150]}"
            for i, r in enumerate(repos)
        )

        prompt = (
            f"You are evaluating GitHub repositories for relevance to this project:\n"
            f"{target_desc}\n\n"
            f"Rate each repository 0-10 for relevance. Format: <number>: <score> | <one-line reason>\n\n"
            f"Repositories:\n{items_text}\n\n"
            f"Only include repos with score >= {self._scout_cfg.l2_min_score}."
        )

        result = await session.run_readonly(prompt, timeout_secs=60)
        return self._parse_scores(result.output, repos)

    def _parse_scores(
        self, output: str, repos: list[RawRepo]
    ) -> list[tuple[RawRepo, float, str]]:
        passed: list[tuple[RawRepo, float, str]] = []
        pattern = re.compile(r"(\d+):\s*([\d.]+)\s*\|\s*(.+)")

        for line in output.splitlines():
            m = pattern.match(line.strip())
            if not m:
                continue
            idx = int(m.group(1)) - 1
            score = float(m.group(2))
            reason = m.group(3).strip()
            if 0 <= idx < len(repos) and score >= self._scout_cfg.l2_min_score:
                passed.append((repos[idx], score, reason))

        return passed

    # ---------------------------------------------------------------------- #
    # Layer 3: deep analysis
    # ---------------------------------------------------------------------- #

    async def _layer3_analyze(self, discovery: Discovery, repo_info: RawRepo) -> None:
        """Deep analysis: fetch README, compare to target, generate improvements."""
        target = self._cfg.targets[0] if self._cfg.targets else None
        if not target:
            return

        readme = await self._fetch_readme(repo_info)
        session = ClaudeSession(working_dir=target.path)

        prompt = (
            f"Analyze this repository and extract actionable improvements for our project.\n\n"
            f"Repository: {repo_info.title}\n"
            f"URL: {repo_info.url}\n"
            f"Description: {repo_info.description}\n\n"
            f"README:\n{readme[:3000]}\n\n"
            f"Our project: {target.description}\n\n"
            f"List 3-5 specific, actionable improvements we could adopt. "
            f"Format each as: PRIORITY | EFFORT | SUMMARY | DETAIL"
        )

        result = await session.run_readonly(prompt, timeout_secs=90)
        improvements = self._parse_improvements(result.output, discovery.id, target.name)

        for imp in improvements:
            await repo.create_improvement(self._db, imp)

        await repo.update_discovery_status(self._db, discovery.id, "analyzed")

    def _parse_improvements(
        self, output: str, discovery_id: int | None, target: str
    ) -> list[Improvement]:
        improvements: list[Improvement] = []
        for line in output.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                priority = parts[0] if parts[0] in ("P0", "P1", "P2", "P3") else "P2"
                effort = parts[1] if parts[1] in ("S", "M", "L") else "M"
                summary = parts[2][:200] if len(parts) > 2 else line[:200]
                detail = parts[3] if len(parts) > 3 else ""
                if summary:
                    improvements.append(
                        Improvement(
                            id=None,
                            discovery_id=discovery_id,
                            target=target,
                            summary=summary,
                            detail=detail,
                            effort=effort,
                            priority=priority,
                            status="proposed",
                        )
                    )
        return improvements[:5]  # max 5 per discovery

    # ---------------------------------------------------------------------- #
    # GitHub API
    # ---------------------------------------------------------------------- #

    async def _fetch_github_search(self) -> list[RawRepo]:
        sc = self._scout_cfg
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self._secrets.github_token:
            headers["Authorization"] = f"token {self._secrets.github_token}"

        repos: list[RawRepo] = []
        for lang in sc.languages[:3]:  # limit to first 3 languages
            query = f"language:{lang} stars:>{sc.min_stars_search}"
            if sc.keywords:
                query += " " + " OR ".join(sc.keywords[:3])

            url = f"{GITHUB_API}/search/repositories"
            params = {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": 20,
            }
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    for item in data.get("items", []):
                        repos.append(
                            RawRepo(
                                url=item["html_url"],
                                title=item["full_name"],
                                description=item.get("description") or "",
                                language=item.get("language") or "",
                                stars=item.get("stargazers_count", 0),
                            )
                        )
                # Rate limit: respect QPS
                await asyncio.sleep(1.0 / sc.source_fetch_qps)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 503) and sc.source_fetch_backoff:
                    logger.warning("GitHub API rate limit, backing off 60s")
                    await asyncio.sleep(60)
                else:
                    logger.warning("GitHub search failed: %s", e)
            except Exception as e:
                logger.warning("GitHub search error: %s", e)

        return repos

    async def _fetch_readme(self, repo_info: RawRepo) -> str:
        headers = {"Accept": "application/vnd.github.v3.raw"}
        if self._secrets.github_token:
            headers["Authorization"] = f"token {self._secrets.github_token}"

        # Extract owner/repo from URL
        parts = repo_info.url.rstrip("/").split("/")
        if len(parts) < 2:
            return ""
        owner, repo_name = parts[-2], parts[-1]
        url = f"{GITHUB_API}/repos/{owner}/{repo_name}/readme"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.text
        except Exception:
            return ""

    async def _persist_discovery(
        self, repo_info: RawRepo, score: float
    ) -> tuple[Discovery, bool]:
        """Persist discovery if not exists. Returns (Discovery, is_new)."""
        exists = await repo.discovery_url_exists(self._db, repo_info.url)
        if exists:
            async with self._db.execute(
                "SELECT * FROM discoveries WHERE url = ?", (repo_info.url,)
            ) as cur:
                row = await cur.fetchone()
            return repo._row_to_discovery(row), False

        d = Discovery(
            id=None,
            source="github_search",
            url=repo_info.url,
            title=repo_info.title,
            description=repo_info.description,
            relevance_score=score,
            tags=[repo_info.language],
            status="discovered",
        )
        d_id = await repo.upsert_discovery(self._db, d)
        d.id = d_id
        return d, True

    def _target_description(self) -> str:
        parts = []
        for t in self._cfg.targets:
            parts.append(f"- {t.name}: {t.description}")
        return "\n".join(parts) if parts else "A software project"
