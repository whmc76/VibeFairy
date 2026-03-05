# HydraMind V2 — 安全自主 AI 助手守护进程 设计文档

状态: Confirmed
日期: 2026-03-05
涉及文件:
- pyproject.toml
- hydramind.toml.example
- .env.example
- src/hydramind/__main__.py
- src/hydramind/daemon.py
- src/hydramind/config/loader.py
- src/hydramind/config/secrets.py
- src/hydramind/memory/db.py
- src/hydramind/memory/models.py
- src/hydramind/memory/repo.py
- src/hydramind/engine/policy.py
- src/hydramind/engine/worker.py
- src/hydramind/engine/claude_session.py
- src/hydramind/engine/scheduler.py
- src/hydramind/comms/telegram_bot.py
- src/hydramind/agents/scout.py
- src/hydramind/agents/analyst.py
- src/hydramind/agents/advisor.py
- src/hydramind/agents/runner.py
- scripts/start.sh, start.bat, install_service.ps1

## 问题 / 背景

V1 方案审查发现 4 个 P0 + 3 个 P1 + 1 个 P2 问题：
- P0: 硬编码密钥、无权限隔离、无审批门控、无指令安全检测
- P1: 无成本闸门、无重试/死信、无锁机制
- P2: Scout 无分层过滤（所有候选都调 LLM）

## 方案

独立项目 `E:\WorkSpace\HydraMind\`，控制平面 + 执行平面分离架构。

### 核心安全机制
1. **零硬编码**: 所有密钥只从 `os.environ` 读取，缺失则 daemon 拒绝启动
2. **默认只读**: Claude session 默认不传 `--dangerously-skip-permissions`
3. **审批闸门**: `/task` 只生成提案，必须 `/approve <id>` 后才执行
4. **审批快照**: `approvals` 表不可变，存储审批时的完整提案内容
5. **指令安全**: 14 个危险模式正则扫描，命中则拦截
6. **用户白名单**: `TELEGRAM_ALLOWED_CHAT_IDS` 环境变量控制

### 7 阶段状态机
```
discovered → analyzed → proposed → approved → executing → applied/failed → verified
```

### 成本闸门
- 每日/单任务 token 上限，超限自动降级为 `report_only`
- `PolicyEngine` 每次执行前检查

### 可靠性
- 重试 + 指数退避 (30s → 60s → 120s → 300s max)
- 死信队列 (连续失败 3 次)
- 目标锁 (同一 target 防并发写)
- 崩溃恢复 (重启时扫描 executing 状态的 run，标为 failed)

## 关键决策

1. **独立项目而非子模块** — 保证 HydraMind 生命周期独立于 HydraMatrix
2. **aiosqlite 直接 SQL，不用 ORM** — 6 表 schema 简单，ORM 引入额外复杂性
3. **python-telegram-bot v20+（native async）** — 避免线程切换
4. **Claude Code SDK（claude-code-sdk）** — 复用 Claude Code 的工具系统
5. **PolicyEngine 在 Worker 内部检查，不在 Bot** — 防止绕过 Bot 直接调 Worker
6. **Approval 记录不可变** — INSERT + 无 UPDATE，保证审计链完整性
7. **Scout 三层过滤** — L1 零成本过滤 ~80% 噪音，L2 批量评分，L3 仅对高分做深度分析

## 放弃的备选方案

- **PostgreSQL**: 单机部署不需要，SQLite WAL 足够
- **Celery**: 引入 Redis 依赖，asyncio 队列足够
- **APScheduler**: 引入额外依赖，自制 Scheduler 更透明
- **langchain**: 过度封装，直接用 Claude Code SDK

## 实现要点

### 目录结构
```
src/hydramind/
  config/    — loader.py (TOML) + secrets.py (env only)
  memory/    — db.py (SQLite 6表) + models.py + repo.py (CRUD)
  engine/    — policy.py + worker.py + claude_session.py + scheduler.py
  comms/     — telegram_bot.py (全部命令 + 审批流)
  agents/    — scout.py (3层) + analyst.py + advisor.py + runner.py
  daemon.py  — 主循环 + 崩溃恢复
```

### DB Schema (6 表)
- `discoveries` — Scout 发现记录
- `improvements` — 改进建议 (7阶段状态)
- `approvals` — 不可变审批快照
- `runs` — 执行记录 + token 计数
- `events` — 审计日志
- `locks` — 目标锁 (带 TTL)

### Telegram 命令
| 命令 | 功能 |
|------|------|
| /scout | 立即触发发现 |
| /report | 最近发现+建议 |
| /status | daemon状态+预算 |
| /targets | 管理的项目 |
| /task \<prompt\> | 生成提案（不直接执行） |
| /approve \<id\> | 审批并执行 |
| /reject \<id\> | 拒绝提案 |
| /retry \<id\> | 重试死信 |
| /dismiss \<id\> | 清除死信 |
| /budget | 今日token用量 |
| 直接消息 | 只读Claude对话 |

## 变更历史

| 日期 | 变更 | 提交 |
|------|------|------|
| 2026-03-05 | 初始实现（Week 1+2+3 完整版） | — |
