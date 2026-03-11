"""Provider-agnostic plan reviewer."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vibefairy.config.loader import DaemonConfig
from vibefairy.engine.claude_session import (
    ClaudePermanentError,
    ClaudeTimeoutError,
    ClaudeTransientError,
)
from vibefairy.engine.model_session import build_model_session

logger = logging.getLogger(__name__)

_REVIEW_PROMPT_TEMPLATE = """\
你是一位独立的代码方案审查专家。

用户原始需求：
---
{user_request}
---

另一个 AI 生成的实施方案：
---
{plan}
---

工作目录：{working_dir}

审查要求：
1. 检查方案完整性、正确性、风险、步骤顺序
2. 你的输出将被直接传递给主模型作为"修改说明"
3. 以具体修改指令的格式输出，例如："请将第 3 步改为..."、"请在第 2 步后增加..."
4. 不要输出泛泛评论，要输出可执行的修改指令
5. 如果方案已经很好，输出："方案良好，无需修改，建议按原计划执行"
"""


@dataclass
class ReviewResult:
    success: bool
    instruction: str
    error: str | None = None
    duration_secs: float = 0.0


class PlanReviewer:
    """Runs the configured review model in read-only mode."""

    def __init__(self, cfg: DaemonConfig) -> None:
        self._cfg = cfg
        self._review_cfg = cfg.models.review

    async def review_plan(
        self,
        user_request: str,
        plan: str,
        working_dir: str,
    ) -> ReviewResult:
        prompt = _REVIEW_PROMPT_TEMPLATE.format(
            user_request=user_request,
            plan=plan,
            working_dir=working_dir,
        )
        session = build_model_session(
            self._review_cfg,
            working_dir=working_dir,
            retry_cfg=self._cfg.retry,
        )
        try:
            result = await session.run_readonly(
                prompt,
                timeout_secs=self._review_cfg.timeout_secs,
            )
            logger.info(
                "Review completed via provider=%s in %.1fs",
                self._review_cfg.provider,
                result.duration_secs,
            )
            return ReviewResult(
                success=True,
                instruction=result.output.strip(),
                duration_secs=result.duration_secs,
            )
        except (ClaudeTransientError, ClaudeTimeoutError, ClaudePermanentError) as exc:
            return ReviewResult(
                success=False,
                instruction="",
                error=str(exc),
            )


