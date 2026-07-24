# Implementation Report: Telegram AI Agent Bot

The implementation of the group-chat capable Telegram AI Agent Bot has been completed according to the architecture and parameters approved in `RESEARCH_PLAN.md`. The full local codebase has been written and structured for seamless deployment.

---

## 1. Directory & File Structure
All source code files are in the project workspace directory:
`[brodar-ai-bot/](file:///mnt/projects/brodar-ai-bot)`

```text
telegram_bot/
├── config.py          # Configuration parser, validating required env variables.
├── db.py              # asyncpg Postgres database connection pooler and CRUD functions.
├── cache.py           # In-memory write-through cache with async background tasks.
├── tools.py           # Safe command executor (shell=False) and DuckDuckGo search.
├── agent.py           # ZaiClient LLM completion loop, tool routing, and 429 retry logic.
├── bot.py             # aiogram filters, handler routers, and admin toggle commands.
├── main.py            # FastAPI entry point, lifespan hooks, and webhook routes.
├── requirements.txt   # Pinned Python package dependencies.
├── .env.example       # Template for configure environment variables.
├── render.yaml        # Render Blueprint file for automated service creation.
└── README.md          # User manual, credentials guide, and setup instructions.
```

---

## 2. Technical Highlights & Default Decisions
Following the user instructions and open questions in the plan, the following technical defaults have been implemented:
1. **Raw Database Pool**: Utilizes direct `asyncpg` pools with a maximum of 5 active connections and 5-minute idle timeouts. This matches the Neon Postgres free tier limits perfectly without SQLAlchemy overhead.
2. **Write-Through Caching**: Immediate response is guaranteed by maintaining in-memory deques of the last 20 messages and a mapping of group settings. Writes are executed asynchronously using `asyncio.create_task()` with background callback error logging, keeping DB latencies off the Telegram response path.
3. **Safe Command Executor**: Runs processes with `shell=False` to neutralize shell-injection. Whitelisted commands (`ping`, `uptime`, `df`, `whoami`, `date`, `uname`) are restricted by strict regex patterns (e.g. `ping` only accepts `-c [1-3] [hostname]`).
4. **Rate Limit Recovery**: Exposes a global `asyncio.Semaphore(1)` to queue LLM requests and serializes calls. Includes a 5-step exponential backoff wrapper that retries on HTTP `429` / Zhipu rate limit codes (`1302` or `1305`) with random jitter.
5. **DB-Free Uptime Endpoint**: The `/health` endpoint is database-free. Pinging this route keeps the Render container awake without querying the DB, allowing Neon to scale down to 0 CUs after 5 minutes of idle time.

---

## 3. Deviations from the Plan
There were **no structural deviations** from the finalized architecture in `RESEARCH_PLAN.md`. All recommended packages, patterns, and integrations have been implemented exactly as designed.

---

## 4. Manual Action Items (Next Steps for the User)

To run or deploy the bot, you need to perform the following steps:

1. **Activate the Workspace**:
   Set `/mnt/projects/brodar-ai-bot` as your active project workspace.


2. **Obtain API Keys & Tokens**:
   * **Telegram Bot**: Message [@BotFather](https://t.me/BotFather) on Telegram and run `/newbot` to get your `TELEGRAM_BOT_TOKEN`.
   * **Zhipu AI API Key**: Sign up at [Zhipu AI](https://open.bigmodel.cn/) and copy the API Key.
   * **Neon Postgres Database**: Log in to [Neon.tech](https://neon.tech/), create a project, and retrieve the Postgres connection URI.

3. **Run Postgres Migration**:
   Connect to your Neon database via the Neon SQL console and run the following queries to create the schema:
   ```sql
    CREATE TABLE IF NOT EXISTS chats (
        chat_id BIGINT PRIMARY KEY,
        mention_only BOOLEAN DEFAULT TRUE NOT NULL,
        is_active BOOLEAN DEFAULT FALSE NOT NULL
    );

    -- To migrate an existing chats table:
    ALTER TABLE chats ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT FALSE NOT NULL;

    CREATE TABLE IF NOT EXISTS allowed_users (
        user_id BIGINT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    );

   CREATE TABLE IF NOT EXISTS messages (
       id SERIAL PRIMARY KEY,
       chat_id BIGINT NOT NULL,
       role VARCHAR(20) NOT NULL,
       content TEXT NOT NULL,
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
   );

   CREATE INDEX IF NOT EXISTS idx_messages_chat_id_created_at 
   ON messages (chat_id, created_at DESC);
   ```

4. **Deploying (to Render or local)**:
   * **Local**: Copy `.env.example` to `.env`, populate the credentials, and execute `python main.py` (ensure to use a tunnel like ngrok for the webhook).
   * **Production**: Push the codebase to a GitHub repository, create a **Blueprint** service on Render, select the repository, and approve the deployment.

---

## 5. Security, Memory & Skills Updates (2026-07-24)

1. **Root MEMORY.md Integration**: Persistent memory file storing identity, casual lowercase style guidelines, jailbreak roasting rules, and creator facts (`Doniyor`). Automatically injected into LLM context.
2. **Hermes-Style SKILL.md System**: Created `skills/` directory with `SKILL.md` instruction files (`jailbreak_roast`, `system_diagnostics`, `web_research`), parsed via `skills.py`. Exposed `use_skill(skill_name)` tool call to the agent loop.
3. **Dynamic DM Access Control**: Added `/allow_user <user_id>` and `/disallow_user <user_id>` commands for authorized administrators, backed by a persistent `allowed_users` table in Neon Postgres.
4. **Essential Commands Added**: `/status`, `/clear`, `/skills`, `/memory`, `/allow_user`, `/disallow_user`, `/activate`, `/deactivate`, `/toggle_reply`.
5. **Expanded Whitelisted Tools**: Enabled `free`, `ps`, `git`, `curl` with regex parameter validation in `tools.py`.


