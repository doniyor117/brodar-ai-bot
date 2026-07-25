# Telegram AI Agent Bot

A group-chat capable Telegram AI Bot with tool-calling capabilities (web search, sandboxed system commands) and conversational memory. Powered by **Zhipu AI (Z.ai) GLM-4.7-Flash** (permanently free tier) and backed by a **Neon serverless Postgres** database with an in-memory write-through cache to maximize performance and preserve free-tier limits.

---

## Technical Stack
* **Framework**: `aiogram 3.x` (Asynchronous Telegram Bot API)
* **Web Server**: `FastAPI` (with Uvicorn)
* **AI Engine**: `zai-sdk` using `glm-4.7-flash` (with fallback configured via env vars)
* **Search Engine**: `ddgs` (DuckDuckGo search wrapper)
* **Database**: `Neon Postgres` (via `asyncpg` connection pooling)
* **Deployment**: `Render` Free Tier + optional `UptimeRobot`

---

## 1. Project Directory Structure
```text
brodar-ai-bot/
├── MEMORY.md               # Persistent memory file storing persona, facts, and learned rules
├── skills/                 # Hermes-style SKILL.md instruction directory
│   ├── jailbreak_roast/    # Skill for playful jailbreak defense & roasting
│   ├── system_diagnostics/ # Skill for server diagnostics & monitoring
│   └── web_research/       # Skill for real-time web research
├── skills.py               # Dynamic SKILL.md loader & parser
├── memory.py               # MEMORY.md & Neon DB memory manager
├── config.py               # Configuration loader & validator
├── db.py                   # asyncpg database connection pooler & CRUD
├── cache.py                # In-memory write-through caching layer
├── tools.py                # Whitelisted command runner (ping, uptime, df, whoami, date, uname, free, ps, git, curl) & DDGS search
├── agent.py                # LLM agent loop, MEMORY.md injection, & tool routing
├── bot.py                  # aiogram AccessControlMiddleware, filters, & command handlers
├── main.py                 # FastAPI entry point & webhook endpoints
├── requirements.txt        # Python package dependencies
├── .env.example            # Environment configuration template
└── README.md               # Technical documentation
```

---

## 2. Neon Postgres Database Setup & Schema
The full schema (all tables **and** indexes) is created automatically on startup
by `db.init_schema()` — you do **not** need to run any SQL by hand. Just point
`DATABASE_URL` at your Neon database and start the app.

The SQL below is provided for reference / manual inspection only:

```sql
-- 1. Create the Chats table to persist reply preferences and activation state
CREATE TABLE IF NOT EXISTS chats (
    chat_id BIGINT PRIMARY KEY,
    mention_only BOOLEAN DEFAULT TRUE NOT NULL,
    is_active BOOLEAN DEFAULT FALSE NOT NULL
);

-- 2. Create the Allowed Users table for dynamic DM access control
CREATE TABLE IF NOT EXISTS allowed_users (
    user_id BIGINT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 3. Create the Messages table for long-term chat histories
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 4. Create an index to optimize chronological history retrieval
CREATE INDEX IF NOT EXISTS idx_messages_chat_id_created_at 
ON messages (chat_id, created_at DESC);
```

---

## 3. How to Obtain Credentials & API Keys

### A. Telegram Bot Token
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to name your bot.
3. Save the HTTP API Token provided (looks like `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).

### B. Zhipu AI (Z.ai) API Key
1. Register/Login on the [Zhipu AI Open Platform](https://open.bigmodel.cn/).
2. Navigate to the API Key management page on your console dashboard.
3. Generate a new API Key and copy it.

### C. Neon Postgres Database URL
1. Sign up for a free account on [Neon.tech](https://neon.tech/).
2. Create a new Postgres project.
3. Under the **Connection Details** dashboard, copy the Connection String. Choose the `Connection pooler` mode for serverless backends and append `?sslmode=require` if not already present.

---

## 4. Local Development Installation
To run this bot locally, you will need a tool like **ngrok** to expose your local port `8000` to the internet so Telegram can reach your webhook endpoint.

1. **Clone/Copy Project Files** and navigate to the directory:
   ```bash
   cd telegram_bot
   ```

2. **Create and Activate a Virtual Environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Setup Environment Variables**:
   Copy `.env.example` to `.env` and fill in the values:
   ```bash
   cp .env.example .env
   ```
   *Note: Set `WEBHOOK_URL` to your ngrok URL (e.g. `https://1234-abcd.ngrok-free.app`).*

5. **Run the Database Migrations** as detailed in Section 2.

6. **Launch the Server**:
   ```bash
   python main.py
   ```

---

## 5. Deploying to Render (Free Tier)
Render supports automated infrastructure provisioning using the included `render.yaml` Blueprint file.

1. Push your codebase to a private/public **GitHub repository**.
2. Go to the [Render Dashboard](https://dashboard.render.com/) and click **New** -> **Blueprint**.
3. Link your GitHub repository.
4. Render will parse `render.yaml` and prompt you for the required variables:
   * `TELEGRAM_BOT_TOKEN`
   * `ZAI_API_KEY`
   * `DATABASE_URL`
   * `WEBHOOK_URL` (Enter your Render App URL, e.g. `https://my-tg-bot.onrender.com`. Leave out trailing slashes and the `/webhook` path).
   * Note: `WEBHOOK_SECRET_TOKEN` will be generated automatically for you.
5. Click **Approve** to deploy. Render will automatically install packages and spin up the FastAPI webhook application.

---

## 6. Documented System Defaults & Open Decisions

### 1. Shell Command Executor Sandboxing
The shell tool inside [tools.py](file:///root/.gemini/antigravity-cli/scratch/telegram_bot/tools.py) runs with `shell=False` to neutralize command injection exploits. It enforces a strict timeout of `5.0` seconds and strips secrets out of environment variables.
* **Default Whitelist**: `ping`, `uptime`, `df`, `whoami`, `date`, and `uname`.
* **Adding New Commands**: To allow more commands, edit the `ALLOWED_COMMANDS` dictionary in `tools.py`. You must supply the binary name and a strict **regex** pattern validating the parameters. For example:
  ```python
  "curl": {
      "bin": "curl",
      "args_regex": r"^-[I]\s+https://[a-zA-Z0-9.-]+$" # Only allow curl -I with safe https domains
  }
  ```

### 2. Keep-Alive / Cold Starts
Render's Free Tier spins down web servers after 15 minutes of inactivity, causing a 30-second delay on the next message.
* **UptimeRobot Keep-Alive**: To keep the bot responsive 24/7, set up a free HTTP ping monitor on [UptimeRobot](https://uptimerobot.com/) targeting `https://your-app.onrender.com/health` every 10 minutes.
* **Neon Compute Hour Preservation**: Our `/health` endpoint is completely **DB-free** (it does not query Postgres). Therefore, UptimeRobot pings keep the Render server awake but allow the Neon database to sleep after 5 minutes of inactivity, saving Neon's free 100 CU-hours quota.

### 3. Database Layer
Instead of ORMs (like SQLAlchemy), the project implements a raw SQL connection pool via **`asyncpg`**. It initializes a small pool (maximum 5 connections, closing idle ones after 5 minutes) to ensure optimal response times while avoiding exhausting Neon connections.

### 4. LLM Concurrency Guard
The Z.ai free tier rate limits request concurrency to 1. To prevent `429` (Too Many Requests) errors, the agent utilizes a global `asyncio.Semaphore(1)` in [agent.py](file:///root/.gemini/antigravity-cli/scratch/telegram_bot/agent.py) to serialize LLM queries. Overlapping messages in group chats are queued and answered sequentially. If peak-hour throttling triggers rate limit responses (`1302` or `1305`), the API client performs up to 5 retries with exponential backoff and random jitter.

---

## 7. Changelog

### Image Generation, Moderation Fixes & Admin Persona (2026-07-25)
* **Image Generation Native Tool**: Added a robust `image_generate` tool powered by LiteLLM image API, enabling the bot to create custom AI images directly in chat.
* **Group Moderation & Unban Links**: Refined the `group_moderation_tool` to explicitly differentiate between "banning" (kicking) and "muting" (silencing). When unbanning a user, the bot now automatically generates a temporary, single-use **invite link** and shares it with the admin, bypassing Telegram API restrictions on bot-initiated re-adds.
* **Strict Admin & DM Compliance**: Dynamically adjusts the bot's system prompt to completely drop its sarcastic, casual persona when interacting with a privileged admin or operating within a Direct Message.
* **Tool Loop Prevention & Stability**: Lowered the interactive approval timeout (from 5 minutes to 30 seconds) to prevent `asyncio` blocking and "circuits fried" exceptions. Optimized the `use_skill` prompt logic to prevent redundant fetching of already active skills.
* **File Sending Skill**: Hardened the `send-media` skill to enforce pre-flight path validation (`ls`) to eliminate hallucinated file paths during delivery.

### Exa Web Search, Advanced Terminal & Interactive Approvals (2026-07-25)
* **Exa Web Search Native Support (`tools.py`)**: Web search natively supports `exa_py` for neural-search AI web results if `EXA_API_KEY` is present in `.env`. Falls back to DuckDuckGo search automatically. No bloated plugins required.
* **Advanced Terminal Sandbox (`tools.py`)**: The `execute_shell_command` tool now maintains a persistent `cwd` (Current Working Directory) per-chat using a `_terminal_sessions` dict, enabling native `cd` operations and persistent terminal traversal without leaving the sandbox.
* **Telegram Interactive Approvals (`agent.py`, `bot.py`)**: Execution of strictly privileged tools (such as `.env` modification or downloading skills) now triggers an asynchronous **Inline Keyboard Prompt** (Approve / Deny) directly in the Telegram chat. 
  - Admins can use `/set_main_account` to designate a master account; approvals will route seamlessly to that account's DMs.
* **Auto-Registered Scoped Commands (`bot.py`)**: The bot automatically registers its command menus to Telegram via `set_my_commands` on startup with scoped visibilities. Private chats see full admin capabilities, while group chats only see basic safe commands (unless you are a group admin). No BotFather manual config needed.

### Multimodal Context, Forwarded Messages & Natural Human Dynamics (2026-07-25)
* **Full Audio/Video Multimodal Context (`media.py`, `bot.py`)**: Uses `ffmpeg` to extract full audio tracks (normalized to tiny 32kbps mono mp3s) from Voice messages, Audio files, Video Notes, and Videos. Dynamically extracts evenly-spaced video frames and bundles BOTH audio and visual timelines into the LLM context (data URLs) for complete multimodal awareness. Safely skips files > 20MB.
* **Rapid-Fire & Forwarded Debounce System**: Implemented a sliding-window debounce for chat handlers using a global timestamp tracker. Normal messages wait 1.5s, Forwarded messages wait 8.0s. If the user rapidly sends multiple messages or a follow-up to a forward, only the newest handler wakes up, effortlessly batching all immediately-saved history into a single cohesive response without duplicating LLM replies.
* **Natural Human Typing Delays (`bot.py`)**: Calculated generation time vs a 25 char/sec fast human typing speed. The bot mathematically pauses (staying in a "typing..." state) if it generated text faster than a human could type it (clamped between 0.5s and 5.0s) for maximum realism.
* **Smart Telegram Reactions (`bot.py`, `agent.py`)**: Trained the model (via few-shots and rules) to optionally append `|[emoji]|` syntax to responses. The bot intercepts this syntax, strips it, and natively reacts to the Telegram message.
* **Bot-to-Bot Loop Auto-Silencing (`[SILENT]`)**: Identifies `is_bot` flags to tag messages with `[BOT]` in the LLM's history. Trained the LLM to autonomously output `[SILENT]` to end unnatural bot-to-bot loops or conversational dead-ends without invoking expensive server-side heuristics.

### Neon-Backed Memory Persistence, Global Abort & Interactive Permission Prompts (2026-07-24)
* **Neon Postgres Memory Persistence (`memory_store`)**: `MEMORY.md` is now stored persistently in Neon Postgres. Automatically synced to local disk on app startup and updated via async write-through on every memory change, guaranteeing **100% memory persistence across Render container redeploys and restarts**.
* **Global Emergency Abort (`/stop_all`, `/cancel_all`)**: Immediately halts all running agent tasks, LLM completions, and tool loops across **all chats globally**. Restricted to authorized bot admins.
* **Group Admin Role Promotion & Demotion (`/promote`, `/demote`)**: Allows promoting members to Group Administrator (via `aiogram 3` `promote_chat_member`) or demoting them back to regular members. Exposed as commands and LLM tool actions.
* **Telegram Interactive Permission Prompt System ([permissions.py](file:///mnt/projects/brodar-ai-bot/permissions.py))**: Non-whitelisted commands or `.env` file read attempts send an **Inline Keyboard message** with `[ ✅ Approve ]` and `[ ❌ Reject ]` buttons. Restricted to Group Admins in group chats with non-admin toast alerts.
* **Terminal Read Tool Whitelist & `.env` Protection**: Safe diagnostic reading commands (`ls`, `cat`, `grep`, `head`, `tail`, `find`, `df`, `free`, `uptime`, `ps`, `whoami`, `date`, `uname`, `git status`, `git log`, `git diff`) run automatically. Any attempt to read `.env` or `.env.*` files triggers an interactive Permission Prompt.
* **Agent Self-Management Tools**: Brodar can create/update skill files in `skills/` (`manage_skill_file`) and rewrite persona rules in `MEMORY.md` (`edit_memory_file`).

### Hermes Sessions, Compaction, Skill Hub & Emergency Abort (2026-07-24)

* **Emergency Abort Kill Switch (`/stop`, `/cancel`)**: Immediately halts active LLM generation or multi-step tool loops for the chat using `asyncio.Task` cancellation.
* **Persistent Sessions & Context Compaction (`/compress`)**:
  - `/new [title]` / `/reset`: Starts a fresh session.
  - `/sessions` & `/switch_session <id>`: Lists and switches active sessions.
  - `/compress`: LLM context compaction algorithm that summarizes older conversation context into a checkpoint while retaining the last 12 messages in full detail.
* **Hermes Skill Hub & External Skill Manager**:
  - `/install_skill <url>`: Installs external `SKILL.md` files from raw URLs or GitHub repos into `skills/<name>/SKILL.md`.
  - `/enable_skill <name>`, `/disable_skill <name>`, `/uninstall_skill <name>`.
* **Group Moderation System (`skills/group_admin/SKILL.md`)**:
  - Commands `/ban`, `/mute`, `/unban`, `/unmute`, `/set_title` strictly guarded by `is_sender_admin` verification.
  - Introduced `group_tools.py` and `group_moderation_tool` schema for LLM tool execution.

### Hermes-Style SKILL.md System, MEMORY.md & Dynamic DM Access (2026-07-24)
* **Root MEMORY.md System**: Persistent `MEMORY.md` file introduced to define identity, style guidelines (casual, lowercase, witty, jailbreak roasting), user facts, and learned rules. Automatically injected into LLM system prompt on boot.
* **Hermes-Style SKILL.md System**: Created `skills/` directory with `SKILL.md` instruction files (`jailbreak_roast`, `system_diagnostics`, `web_research`). Introduced `skills.py` loader module and LLM tool `use_skill(skill_name)`.
* **Dynamic DM User Allowlist**: Added `/allow_user <user_id>` and `/disallow_user <user_id>` commands for authorized administrators. Added `allowed_users` table to Postgres for persistence across restarts.

* **Management Commands Added**:
  - `/status`: Uptime, RAM usage, model name, active status.
  - `/clear`: Resets chat context and history.
  - `/skills`: Lists available skill instruction files.
  - `/memory`: Displays `MEMORY.md` contents.
  - `/allow_user` / `/disallow_user`: Dynamically grant/revoke DM access.
* **Expanded Command Whitelist**: Added `free`, `ps`, `git`, and `curl` to safe shell executor whitelist.

### DM Access Control & Group Chat Opt-in (2026-07-24)
* **DM Access Control (Allowlist Only)**: The bot is updated to only respond in private/DM chats to specific user IDs (`2030903420`, `8116285130`). DMs from any other users are silently ignored. Configured via the `ALLOWED_DM_USER_IDS` environment variable.
* **Group Opt-in Model**: The bot will no longer respond in group chats by default. It must be explicitly activated in each group chat first using the `/activate` command.
* **Commands**:
  * `/activate` (restricted to allowed user IDs): Activates the bot in a group chat.
  * `/deactivate` (restricted to allowed user IDs): Deactivates the bot in a group chat (silences all responses).

* **Database Updates**: Added the `is_active` boolean column to the `chats` table, defaulting to `FALSE`. Added a migration script segment and updated the cache mechanism to support write-through caching of the active status.

