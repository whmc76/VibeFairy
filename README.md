# ClaudeFairy

> Telegram 驱动的自主 AI 助手守护进程 — 每条消息都是一个被追踪的任务。
>
> A Telegram-driven autonomous AI assistant daemon — every message becomes a tracked task.

ClaudeFairy 把你的 Telegram 与 Claude Code 连接起来。发一条消息，它自动分类、生成方案、等你确认后执行，最后回报结果。

ClaudeFairy connects your Telegram to Claude Code. Send a message, and it automatically classifies, plans, and executes — with your approval.

---

## 工作原理 / How It Works

```
你 (Telegram) → ClaudeFairy → Claude Code (claude-code-sdk)
                     ↓
       自动分拣: note（备忘）/ question（问答）/ action（执行）
                     ↓（仅 action 类）
             方案卡片 + 内联按钮确认
                     ↓（点击「批准执行」）
          在你的项目里执行 → 回报结果
```

```
You (Telegram) → ClaudeFairy → Claude Code (claude-code-sdk)
                     ↓
         Auto-triage: note / question / action
                     ↓ (action only)
         Plan card + inline buttons
                     ↓ (you tap "Approve")
         Execute in your project → Report back
```

---

## 功能特性 / Features

- **消息即任务** — 每条消息自动创建任务，无需 `/task` 命令
- **自动分拣** — AI 自动分类：`note`（闲聊备忘）、`question`（只读回答）、`action`（生成方案并执行）
- **内联按钮审批** — 点击按钮批准 / 打回 / 取消，无需记住 ID
- **原子 Claim 防竞态** — SQLite CAS 保证同一消息不会被重复处理
- **零 token 日报** — 纯 SQL 聚合任务看板，不额外消耗 token
- **静默时段** — 夜间只通知 P0 紧急任务
- **GUI 启动器** — 一键启动，内置项目目录选择器
- **任意项目** — 可指向任何代码库，不限于固定项目

---

- **Message as Task** — every message creates a tracked task (no `/task` commands needed)
- **Auto-triage** — AI classifies: `note` (chat/memo), `question` (answer only), `action` (plan + execute)
- **Inline approval** — tap buttons to approve / rework / cancel, no need to remember IDs
- **Atomic claim** — SQLite CAS prevents duplicate processing between immediate and scheduled triage
- **Zero-token daily report** — pure SQL aggregation, no extra Claude calls
- **Quiet hours** — silence non-P0 notifications at night
- **GUI launcher** — one-click start with project directory picker
- **Any project** — point it at any codebase, not just one fixed repo

---

## 快速开始 / Quick Start

### 前置条件 / Prerequisites

- Python 3.11+
- Telegram Bot token（从 [BotFather](https://t.me/BotFather) 获取）
- Anthropic API key（用于 Claude Code SDK）
- Claude Code CLI：`npm install -g @anthropic-ai/claude-code`

### 安装 / Install

```bash
git clone https://github.com/whmc76/ClaudeFairy.git
cd ClaudeFairy
pip install -e .
```

### 配置 / Configure

```bash
cp .env.example .env
# 编辑 .env，填入 Telegram Bot Token 和允许的聊天 ID
# Edit .env — fill in your Telegram bot token and allowed chat IDs

cp claudefairy.toml.example claudefairy.toml
# 编辑 claudefairy.toml，在 [[targets.projects]] 中设置你的项目路径
# Edit claudefairy.toml — set your project path in [[targets.projects]]
```

`.env` 示例 / example:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ALLOWED_CHAT_IDS=your_telegram_user_id
ANTHROPIC_API_KEY=your_anthropic_key_here
```

### 启动 / Run

**GUI（推荐 / Recommended）：**

双击 `启动ClaudeFairy.bat`（Windows）— 打开启动器，选择项目目录后点击「启动」。

Double-click `启动ClaudeFairy.bat` (Windows) — opens a launcher window, pick your project directory and click Start.

**命令行 / CLI：**
```bash
claudefairy run
# 或 / or
cf run
```

**验证配置 / Check config：**
```bash
cf check
```

---

## 任务状态机 / Task State Machine

```
Action:   received → triaging → planned → awaiting_user_decision → approved → executing → done / failed
Question: received → triaging → answered → closed
Note:     received → triaging → noted → closed
```

---

## Telegram 命令 / Commands

| 命令 / Command | 说明 / Description |
|----------------|-------------------|
| `/list` | 查看待处理任务 / Show pending tasks (action only) |
| `/approve <id>` | 按 task ID 批准 / Approve task by ID |
| `/approve_imp <id>` | 按 improvement ID 批准（向后兼容）/ Approve improvement (backward compat) |
| `/cancel <id>` | 取消任务 / Cancel task |
| `/status` | 状态 + token 预算 / Status + token budget |
| `/report` | 立即生成日报 / On-demand daily report |
| `/start` | 欢迎信息 / Welcome |

也可以直接点击方案卡片上的内联按钮，无需记住任何 ID。
You can also tap the inline buttons on the plan card — no need to remember any IDs.

---

## 配置说明 / Configuration

详见 [`claudefairy.toml.example`](claudefairy.toml.example)：

| 配置节 / Section | 说明 / Description |
|-----------------|-------------------|
| `[daemon]` | 日志、数据库路径、日报时间 / Log, DB path, daily report time |
| `[targets]` | 管理的项目列表 / Projects to manage |
| `[budget]` | 每日 / 单任务 token 上限 / Daily & per-task token limits |
| `[triage]` | 分拣模型、超时、重试 / Triage model, timeout, retries |
| `[notification]` | 静默时段 / Quiet hours |

---

## 项目结构 / Architecture

```
src/claudefairy/
├── __main__.py          CLI 入口 / Entry point
├── daemon.py            主循环 + 调度器 / Main loop + scheduler
├── agents/
│   ├── triage.py        自动分拣 / Auto-classify messages
│   ├── scout.py         趋势分析 / Trend analysis
│   ├── analyst.py       深度分析 / Deep analysis
│   ├── advisor.py       改进建议 / Improvement suggestions
│   └── runner.py        任务执行 / Execution via Claude Code SDK
├── comms/
│   └── telegram_bot.py  Bot + 内联键盘 / Inline keyboards + callbacks
├── config/
│   ├── loader.py        TOML 配置加载 / Config loader
│   └── secrets.py       .env 密钥加载 / Secret loader
├── engine/
│   ├── claude_session.py Claude Code SDK 封装 / SDK wrapper
│   ├── worker.py        改进执行器 / Improvement worker
│   ├── policy.py        安全策略 / Safety policy
│   └── scheduler.py     定时任务 / Scheduler wrapper
└── memory/
    ├── db.py            SQLite schema + 迁移 / Migrations
    ├── models.py        数据模型 / Dataclasses
    └── repo.py          CRUD + 原子 claim_for_triage
```

---

## 安全说明 / Security

- 所有任务执行前必须经过 Telegram 按钮明确授权 / All executions require explicit Telegram button approval
- 仅白名单聊天 ID 可与 Bot 交互 / Only whitelisted chat IDs can interact with the bot
- Claude Code 以本地用户权限运行，执行前请仔细审阅方案 / Claude Code runs with local user permissions — review plans before approving
- 密钥只存在于 `.env`，绝不写入配置文件 / Secrets in `.env` only, never in `claudefairy.toml`

---

## License

MIT
