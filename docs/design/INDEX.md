# Design Documents Index

按时间倒序排列。开始任何改动前请先查阅此索引。

| 日期 | 文档 | 状态 | 涉及模块 |
|------|------|------|----------|
| 2026-03-08 | [Rate Limit 韧性增强](./2026-03-08-rate-limit-resilience.md) | Confirmed | engine/resilience, engine/claude_session, engine/worker, agents/triage, agents/scout, agents/analyst, agents/advisor, comms/telegram_bot, config/loader, daemon |
| 2026-03-07 | [Codex Review 模式 — 双重计划执行](./2026-03-07-codex-review-dual-plan.md) | Confirmed | config/loader, agents/codex_reviewer, engine/claude_plan_mode, engine/dual_plan_orchestrator, __main__ |
| 2026-03-05 | [VibeFairy V2 架构设计](./2026-03-05-vibefairy-v2-architecture.md) | Confirmed | __main__, daemon, config, memory, engine, comms, agents, scripts |
| 2026-03-05 | [VibeFairy Task Triage V2](./2026-03-05-vibefairy-task-triage-v2.md) | Confirmed | memory/models, memory/db, memory/repo, agents/triage, comms/telegram_bot, daemon, config/loader |
