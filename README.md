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
telegram_bot/
├── config.py          # Configuration loader & validator
├── db.py              # asyncpg database connection & operations
├── cache.py           # In-memory write-through caching layer
├── tools.py           # Core agent tools (DuckDuckGo search & safe subprocess runner)
├── agent.py           # LLM client, prompt templates & tool-calling agent loop
├── bot.py             # aiogram filters, commands, and chat handlers
├── main.py            # FastAPI entry point, lifespan hooks, and webhook routes
├── requirements.txt   # Pinned Python package dependencies
├── .env.example       # Example environment configuration file
├── render.yaml        # Render Blueprint deployment definition
└── README.md          # Technical documentation (this file)
```

---

## 2. Neon Postgres Database Setup & Schema
To keep the bot lightweight and efficient, we use **raw SQL migrations** via the Neon console or any SQL editor. This avoids adding the overhead of SQLAlchemy and running automated migration checks on boot (which would wake up Neon unnecessarily, wasting free-tier Compute Hours).

### Migration SQL
Log in to your [Neon Console](https://console.neon.tech/), select your database, open the **SQL Editor**, and run the following statements:

```sql
-- 1. Create the Chats table to persist reply preferences
CREATE TABLE IF NOT EXISTS chats (
    chat_id BIGINT PRIMARY KEY,
    mention_only BOOLEAN DEFAULT TRUE NOT NULL
);

-- 2. Create the Messages table for long-term chat histories
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 3. Create an index to optimize chronological history retrieval
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
