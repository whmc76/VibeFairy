# HydraMind — "消息即任务" 交互模型重构 V2 — 设计文档

状态: Confirmed
实现完成: 2026-03-05
日期: 2026-03-05
涉及文件:
- src/hydramind/memory/models.py       (新增 Task dataclass)
- src/hydramind/memory/db.py           (Migration V2: tasks 表)
- src/hydramind/memory/repo.py         (Task CRUD + claim_for_triage)
- src/hydramind/agents/triage.py       (新建: 三分类 + 方案生成)
- src/hydramind/comms/telegram_bot.py  (重写: 按钮卡片 + CallbackQuery)
- src/hydramind/daemon.py              (接入 TriageAgent + 日报改造)
- src/hydramind/config/loader.py       (新增 TriageConfig + NotificationConfig)
- hydramind.toml.example               (新增 [triage] + [notification])
不改动:
- src/hydramind/engine/policy.py
- src/hydramind/engine/worker.py
- src/hydramind/engine/claude_session.py
- src/hydramind/engine/scheduler.py
- src/hydramind/config/secrets.py

Supersedes: docs/design/2026-03-05-hydramind-v2-architecture.md (追加变更)

## 问题 / 背景

V1 是命令驱动型：用户必须手动 /task <prompt> 才能创建提案。
用户期望：直接发消息 → 系统自动分类 → 生成方案 → 按钮确认 → 执行 → 报告。

V1 审查发现的问题（已在本版全部修复）：
- P0-1: 统一状态机缺失（Action/Question/Note 混用同一状态流）
- P0-2: 即时分拣 + 定时调度之间存在竞态，同一消息可能被处理两次
- P1-1: /approve <id> 歧义（task_id vs improvement_id）
- P1-2: 消息分类只有 action/question，缺 note 导致看板污染
- P1-3: 纯文字命令要求用户记住 ID，交互体验差
- P2-1: 报告结构不清晰（发现/建议/任务混在一起）
- P2-2: 无静默时段支持，深夜 P2 任务也会发通知

## 方案

### 统一状态机

**Action 类**（需要代码改动）:
```
received → triaging → planned → awaiting_user_decision → approved → executing → done / failed / dead_letter
```

**Question 类**（只需回答）:
```
received → triaging → answered → closed
```

**Note 类**（备忘/闲聊）:
```
received → triaging → noted → closed
```

终态: done, failed, dead_letter, closed, cancelled

### 原子 Claim 防竞态

```python
async def claim_for_triage(db, task_id: int) -> bool:
    # CAS: 只有 status=received 时才能 claim
    UPDATE tasks SET status='triaging' WHERE id=? AND status='received'
```

两条触发路径（_handle_message + 定时调度器）共享同一 claim，互斥执行。

### 按钮式决策卡片

使用 InlineKeyboardMarkup 替代纯文字命令，主路径走按钮，备选文字命令保留。

### 三分类

| kind   | 判定标准                        | 处理方式                    |
|--------|---------------------------------|-----------------------------|
| note   | 闲聊/备忘/情绪/无明确意图      | 回复确认 → closed           |
| question | 询问信息/求解释/要分析        | 只读 Claude 回答 → closed   |
| action | 要求改代码/加功能/修 bug/重构  | 生成方案 → 按钮确认 → 执行 |

## 关键决策

1. **task_id 与 improvement_id 分离命令**: /approve 接受 task_id，/approve_imp 接受 improvement_id，消除歧义
2. **claim_for_triage 是 SQLite CAS，不加 Python 锁**: SQLite 串行写天然保证原子性
3. **TriageAgent 复用 ClaudeSession.run_readonly**: 分拣是只读操作，不消耗写权限预算
4. **triaging 状态是中间态**: 崩溃恢复时将 triaging → received，而不是直接 failed
5. **note 直接 closed，不进看板**: /list 命令只显示 action 类任务
6. **日报由 repo 查询生成，不额外调 Claude**: 纯 SQL 聚合，零 token 成本

## 放弃的备选方案

- **单命令 /approve 接受前缀区分** (如 `/approve t:42` vs `/approve i:42`): 用户体验差
- **所有消息统一走 action 流**: 看板被闲聊污染，用户抵触
- **Python asyncio.Lock 防竞态**: DB CAS 更简单且跨进程有效

## 实现要点

### Task 表新字段
- source_message_id: 回溯 Telegram 消息串联
- decision_needed: 用于快速查询当前需要决策的任务
- triage_retries / execute_retries: 独立重试计数
- improvement_id / approval_id / run_id: 关联执行链

### 三层报告结构
1. 即时回执 (received): "收到! Task #N 已创建, 正在分析..."
2. 决策卡片 (awaiting_user_decision): 方案 + [批准执行][打回重做][取消] 按钮
3. 执行报告 (done/failed): 状态 + token + 耗时 + 改动摘要

### 配置新增
- [triage] max_retries, model, timeout_secs
- [notification] quiet_hours_start/end (P0 不受限制)

## 变更历史

| 日期 | 变更 | 提交 |
|------|------|------|
| 2026-03-05 | 初始实现 V2 重构（4 步全量） | — |
