# Rate Limit 韧性增强 — 设计文档
状态: Confirmed
日期: 2026-03-08
涉及文件:
- `src/vibefairy/engine/resilience.py` (新建)
- `src/vibefairy/engine/claude_session.py` (重构)
- `src/vibefairy/config/loader.py` (RetryConfig 扩展)
- `src/vibefairy/daemon.py` (信号量初始化)
- `src/vibefairy/engine/worker.py` (锁序修正 + 异常适配)
- `src/vibefairy/agents/triage.py` (异常分类处理)
- `src/vibefairy/agents/scout.py` (Claude 异常 + GitHub 退避)
- `src/vibefairy/agents/analyst.py` (Claude 异常处理)
- `src/vibefairy/agents/advisor.py` (Claude 异常处理)
- `src/vibefairy/comms/telegram_bot.py` (重试包装)
- `vibefairy.toml.example` (新配置项文档)
- `pyproject.toml` (pytest 依赖)
- `tests/test_resilience.py` (新建)
- `tests/test_claude_session.py` (新建)
- `tests/test_worker_retry.py` (新建)

## 问题 / 背景

运行时遇到 rate limit 错误，但系统无法正确处理：

1. `ClaudeSession._run()` 把所有错误吞掉，返回 `SessionResult(exit_code=1, output="[ERROR] ...")`
2. 调用方把错误文本当正常输出解析：`[TIMEOUT]` 被 fallback 判为 note，错误文本被当 score 解析
3. Worker 外层重试失效：`_execute_once()` 在 `exit_code != 0` 时不设 `error` 字段，外层只看到 `"unknown error"`，瞬态判定为 False
4. GitHub API 只处理 429/503，未处理 403（GitHub 主要 rate limit 状态码），且不解析 Retry-After / x-ratelimit-reset 头

## 方案

修复失败契约是核心：**失败时 raise typed exception，而不是返回错误文本**。

### 失败契约
- `ClaudeSession._run()` 成功 → 返回 `SessionResult`
- 失败 → raise `ClaudeTransientError` / `ClaudePermanentError` / `ClaudeTimeoutError`
- 调用方 catch 异常，不再解析 `exit_code` 或 `[ERROR]` 文本

### 内层重试
- `_run()` 内置指数退避重试（最多 3 次，5s 起，上限 60s）
- 总时间预算（deadline）不变，每次 attempt 用剩余时间做 per-attempt timeout
- 重试耗尽 → raise `ClaudeTransientError`

### 全局并发控制
- `resilience.py` 提供全局 `asyncio.Semaphore`
- Worker 在外层持有信号量（sem → lock → run 锁序）
- ClaudeSession 接受 `semaphore=None` 参数跳过内部信号量（Worker 已持有）

## 关键决策

1. **raise 而不是 return error** — 消除了调用方对错误文本的脆弱解析，使错误类型在类型系统中可见
2. **deadline 而非 per-attempt timeout** — 总时间预算固定，多次重试不会超出 `timeout_secs`
3. **sem → lock 锁序** — 避免持锁等信号量导致的死锁
4. **Worker 传 `semaphore=None`** — Worker 自管并发，ClaudeSession 不重复限制
5. **统一 `is_transient_error()`** — 内置 patterns + 用户配置 `transient_errors`，单一来源
6. **GitHub 403 处理** — GitHub 主要用 403 返回 rate limit（Retry-After 头），不处理则一直失败

## 放弃的备选方案

- **在调用方检查 `[ERROR]` 前缀** — 脆弱，字符串匹配容易出错，不如 typed exception 清晰
- **全局重试装饰器** — 对 async generator 兼容性差，不如在 `_run()` 内直接循环
- **每次 attempt 独立 timeout** — 会导致总等待时间 = N × timeout，不可预测

## 实现要点

- `_NullSemaphore`：no-op async context manager，Worker 用 `semaphore=None` 时生效
- `_UNSET` sentinel：区分"用全局信号量"（默认）和"不用信号量"（`None`）
- `resilience._claude_semaphore` 模块级全局，`init_claude_semaphore()` 在 daemon 启动时调用
- Triage 异常处理：`ClaudeTransientError` → 回 received 重试；`ClaudePermanentError` → 直接 failed
- Telegram 关键路径（通知、审批结果、执行结果）使用 `_safe_reply`/`_safe_edit` 三次重试

## 变更历史

- 2026-03-08: 初版实现

