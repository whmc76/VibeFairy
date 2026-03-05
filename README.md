# ClaudeFairy

> A Telegram-driven autonomous AI assistant daemon — every message becomes a tracked task.

ClaudeFairy connects your Telegram to Claude Code. Send a message, and it automatically classifies, plans, and executes — with your approval.

## How It Works

```
You (Telegram) → ClaudeFairy → Claude Code (claude-code-sdk)
                     ↓
         Auto-triage: note / question / action
                     ↓ (action only)
         Plan card + inline buttons
                     ↓ (you tap "Approve")
         Execute in your project → Report back
```

## Features

- **Message as Task** — every message creates a tracked task (no `/task` commands needed)
- **Auto-triage** — AI classifies: `note` (chat), `question` (answer only), `action` (plan + execute)
- **Inline approval** — tap buttons to approve / rework / cancel before anything runs
- **Atomic claim** — SQLite CAS prevents duplicate processing between immediate and scheduled triage
- **Daily report** — task board summary every morning (zero extra tokens, pure SQL)
- **Quiet hours** — silence non-P0 notifications at night
- **GUI launcher** — one-click start with project directory picker
- **Any project** — point it at any codebase, not just one fixed repo

## Quick Start

### 1. Prerequisites

- Python 3.11+
- A Telegram Bot token ([BotFather](https://t.me/BotFather))
- An Anthropic API key (for Claude Code SDK)
- Claude Code CLI installed: `npm install -g @anthropic-ai/claude-code`

### 2. Install

```bash
git clone https://github.com/whmc76/ClaudeFairy.git
cd ClaudeFairy
pip install -e .
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env — fill in your Telegram bot token and allowed chat IDs

cp claudefairy.toml.example claudefairy.toml
# Edit claudefairy.toml — set your project path in [[targets.projects]]
```

`.env` contents:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ALLOWED_CHAT_IDS=your_telegram_user_id
ANTHROPIC_API_KEY=your_anthropic_key_here
```

### 4. Run

**GUI (recommended):**
Double-click `启动ClaudeFairy.bat` (Windows) — opens a launcher window where you can pick the project directory and start/stop the daemon.

**CLI:**
```bash
claudefairy run
# or
cf run
```

**Check config:**
```bash
cf check
```

## Task State Machine

```
Action:   received → triaging → planned → awaiting_user_decision → approved → executing → done / failed
Question: received → triaging → answered → closed
Note:     received → triaging → noted → closed
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/list` | Show pending tasks (action class only) |
| `/approve <id>` | Approve a task by task ID |
| `/approve_imp <id>` | Approve an improvement (backward compat) |
| `/cancel <id>` | Cancel a task |
| `/status` | Current daemon status + budget |
| `/report` | On-demand daily report |
| `/start` | Welcome message |

Or just use the inline buttons on the plan card — no need to remember IDs.

## Configuration

See [`claudefairy.toml.example`](claudefairy.toml.example) for all options including:
- `[daemon]` — log level, DB path, scout interval, daily report time
- `[targets]` — projects ClaudeFairy can work on
- `[budget]` — daily/per-task token limits
- `[triage]` — classification model, retries, timeout
- `[notification]` — quiet hours

## Architecture

```
src/claudefairy/
├── __main__.py          Entry point (CLI)
├── daemon.py            Main daemon loop + scheduler
├── agents/
│   ├── triage.py        Auto-classify messages → note/question/action
│   ├── scout.py         Repository trend analysis
│   ├── analyst.py       Deep analysis agent
│   ├── advisor.py       Improvement suggestions
│   └── runner.py        Task execution via Claude Code SDK
├── comms/
│   └── telegram_bot.py  Bot + inline keyboards + callback handler
├── config/
│   ├── loader.py        TOML config loader
│   └── secrets.py       .env secret loader
├── engine/
│   ├── claude_session.py Claude Code SDK wrapper
│   ├── worker.py        Improvement worker
│   ├── policy.py        Safety policy checks
│   └── scheduler.py     APScheduler wrapper
└── memory/
    ├── db.py            SQLite schema + migrations
    ├── models.py        Dataclasses (Task, Improvement, etc.)
    └── repo.py          CRUD + atomic claim_for_triage
```

## Security

- All task executions require explicit user approval via Telegram button
- Only whitelisted chat IDs can interact with the bot
- Claude Code runs with the permissions of your local user — review plans before approving
- Secrets in `.env` only, never in `claudefairy.toml`

## License

MIT
