# Codex Review 模式 — 双重计划执行 — 设计文档
状态: Confirmed
日期: 2026-03-07
涉及文件:
- `src/vibefairy/config/loader.py` (修改: 新增 CodexReviewConfig, DaemonConfig.codex_review)
- `src/vibefairy/agents/codex_reviewer.py` (新建)
- `src/vibefairy/engine/claude_plan_mode.py` (新建)
- `src/vibefairy/engine/dual_plan_orchestrator.py` (新建)
- `src/vibefairy/__main__.py` (修改: 新增 codex-review 子命令)
- `vibefairy.toml.example` (修改: 新增 [codex_review] section)

## 问题 / 背景

现有代码库只通过 `claude-code-sdk` 的 `query()` 函数做单次只读调用（`claude_session.py:98`），没有 plan mode、多轮会话、session 管理。需要实现完整的 Claude plan mode → Codex review → option 4 回填 → Claude 改稿 → option 1 执行的编排层。

## 方案

四阶段编排流程：
1. **Phase 1**: Claude 以 `permission_mode="plan"` 生成初始计划
2. **Phase 2**: Codex CLI (`codex exec -s read-only`) 审查计划，输出 option-4 修改指令
3. **Phase 3**: 把 Codex 指令提交给同一 Claude 会话（option 4 等价），Claude 修改计划
4. **Phase 4**: Resume 会话执行最终计划（option 1 等价）

## 关键决策

| 决策 | 理由 |
|------|------|
| `ClaudeSDKClient` 而非 `query()` | 需要多轮会话保持 session_id；query() 仅支持单次调用 |
| `_find_plan_file()` 优先读 `.claude/plans/*.md` | plan mode 下 Claude 会写文件，文件内容比消息文本更完整 |
| Codex model 仅用户显式配置时才传 `-m` | 不传则继承用户本地 codex 默认配置，避免 override 用户偏好 |
| Codex 失败全部静默降级 | Codex 是可选增强，不能成为主流程阻塞点 |
| `_NO_CHANGE_SIGNALS` 关键词检测 | 避免把"方案良好"类回复当作修改指令提交给 Claude |
| `codex exec -s read-only` 强制只读 | 审查阶段不允许 Codex 修改任何文件 |

## 放弃的备选方案

- **在 TriageAgent 里多加一次 prompt refine**: 不符合目标，只是单次调用优化而非完整编排层
- **持久化 Codex review 文本到 DB**: 第一版不做，避免 schema 变更
- **修改 Telegram 决策卡片**: 第一版不做，命令行入口优先验证端到端流程

## 实现要点

### 新增配置 (`CodexReviewConfig`)
```toml
[codex_review]
enabled = false
# model = "o4-mini"  # 不填继承用户默认
timeout_secs = 120
# codex_path = "codex"
```

### 调用方式 (`CodexReviewer`)
```bash
codex exec -C <working_dir> -s read-only --color never [-m model] -
```
stdin 注入 review prompt，stdout 读取修改指令。

### ClaudePlanMode 生命周期
```
generate_plan(prompt) → session_id + plan_text
submit_revision(feedback) → revised plan_text (同 session)
close() → disconnect
```

### CLI 使用
```bash
cf codex-review "给项目添加 health check 接口" --project HydraMatrix
echo "需求" | cf codex-review --dir /path/to/project
```

## 变更历史

- 2026-03-07: 初始实现，状态 Confirmed

