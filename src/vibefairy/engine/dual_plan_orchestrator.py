"""DualPlanOrchestrator — provider-agnostic main model + review model pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vibefairy.agents.plan_reviewer import PlanReviewer
from vibefairy.engine.model_session import build_model_session
from vibefairy.engine.claude_session import SessionResult

logger = logging.getLogger(__name__)

# Keywords that indicate the review model is satisfied with the plan as-is
_NO_CHANGE_SIGNALS = ("无需修改", "按原计划", "no changes needed", "looks good")

_PLAN_PROMPT_TEMPLATE = """\
你是一位资深软件工程师。请先阅读需求并给出执行计划，不要直接动手修改。

用户需求：
---
{user_request}
---

工作目录：{working_dir}

要求：
1. 输出 1-5 步的具体实施计划
2. 每一步都要明确、可执行
3. 先考虑风险、依赖和验证方式
4. 不要输出代码，除非澄清方案必须引用极短片段
"""

_REVISION_PROMPT_TEMPLATE = """\
这是原始需求：
---
{user_request}
---

这是当前实施计划：
---
{plan}
---

这是 review 模型给出的修改意见：
---
{instruction}
---

请根据修改意见给出一份更新后的最终实施计划。要求仍然是 1-5 步、具体、可执行。
"""

_EXECUTION_PROMPT_TEMPLATE = """\
请在当前项目中完成下面的需求。

用户需求：
---
{user_request}
---

已经批准的实施计划：
---
{plan}
---

执行要求：
1. 按计划实现，必要时根据代码库现实情况做小幅调整
2. 实际修改代码并自检
3. 最终总结实际改动、验证结果和剩余风险
"""


@dataclass
class OrchestrationResult:
    initial_plan: str
    revised_plan: str
    review_used: bool
    session_id: str
    execution_output: str
    execution_tokens: int
    success: bool


class DualPlanOrchestrator:
    """Orchestrates the four-phase dual-plan execution pipeline."""

    def __init__(self, cfg: object) -> None:
        # cfg: DaemonConfig
        self._cfg = cfg
        self._main_cfg = cfg.models.main
        self._review_cfg = cfg.models.review

    async def run(self, user_request: str, working_dir: str) -> OrchestrationResult:
        main_session = build_model_session(
            self._main_cfg,
            working_dir=working_dir,
            retry_cfg=self._cfg.retry,
        )
        session_id = f"{self._main_cfg.provider}:{working_dir}"

        try:
            # ── Phase 1: Generate initial plan ──────────────────────────────
            logger.info("Phase 1: Generating initial plan with provider=%s", self._main_cfg.provider)
            plan_result = await main_session.run_readonly(
                _PLAN_PROMPT_TEMPLATE.format(
                    user_request=user_request,
                    working_dir=working_dir,
                ),
                timeout_secs=self._main_cfg.timeout_secs,
            )
            initial_plan = plan_result.output
            logger.info("Initial plan generated (%d chars)", len(initial_plan))

            # ── Phase 2 & 3: Review + main-model revision ───────────────────
            revised_plan = initial_plan
            review_used = False

            if self._review_cfg.enabled:
                logger.info("Phase 2: Sending plan to review provider=%s", self._review_cfg.provider)
                reviewer = PlanReviewer(self._cfg)
                review = await reviewer.review_plan(
                    user_request=user_request,
                    plan=initial_plan,
                    working_dir=working_dir,
                )

                if review.success and review.instruction:
                    no_change = any(sig in review.instruction for sig in _NO_CHANGE_SIGNALS)
                    if no_change:
                        logger.info("Review approved plan as-is — skipping revision")
                        review_used = True
                    else:
                        logger.info("Phase 3: Revising plan with main provider=%s", self._main_cfg.provider)
                        try:
                            revision = await main_session.run_readonly(
                                _REVISION_PROMPT_TEMPLATE.format(
                                    user_request=user_request,
                                    plan=initial_plan,
                                    instruction=review.instruction,
                                ),
                                timeout_secs=self._main_cfg.timeout_secs,
                            )
                            revised_plan = revision.output
                            review_used = True
                            logger.info("Plan revised (%d chars)", len(revised_plan))
                        except Exception as e:
                            logger.warning("Plan revision failed (%s) — using original plan", e)
                else:
                    logger.warning("Review failed (%s) — using original plan", review.error)
            else:
                logger.info("Phase 2: Review disabled — skipping")

            # ── Phase 4: Execute final plan ──────────────────────────────────
            logger.info("Phase 4: Executing final plan with provider=%s", self._main_cfg.provider)
            exec_result = await self._execute_plan(
                session=main_session,
                user_request=user_request,
                plan=revised_plan,
            )

            return OrchestrationResult(
                initial_plan=initial_plan,
                revised_plan=revised_plan,
                review_used=review_used,
                session_id=session_id,
                execution_output=exec_result.output,
                execution_tokens=exec_result.token_count,
                success=exec_result.exit_code == 0,
            )

        except Exception:
            logger.exception("Dual plan orchestration failed")
            raise

    async def _execute_plan(self, session, user_request: str, plan: str) -> SessionResult:
        return await session.run_write(
            _EXECUTION_PROMPT_TEMPLATE.format(
                user_request=user_request,
                plan=plan,
            ),
            timeout_secs=self._main_cfg.timeout_secs,
        )

