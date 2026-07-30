# Brodar — Telegram AI Companion Bot

A group-and-DM capable Telegram AI bot with tool-calling (web search, a sandboxed
shell, group moderation, member lookup, skill loading), multimodal understanding
(vision + audio via Gemini), persistent memory, and a human-in-the-loop approval
system for anything destructive. Routed through **LiteLLM** across two providers —
**Z.ai (GLM-4.7 Flash)** for text, **Gemini 3.x Flash-Lite** for vision/audio — and
backed by **Neon serverless Postgres** with an in-memory write-through cache.

---

## Technical Stack
* **Framework**: `aiogram 3.x` (async Telegram Bot API), webhook-driven
* **Web Server**: `FastAPI` + `uvicorn`
* **AI Engine**: `litellm`, one unified interface across every model. See `models.py`
  for the registry — GLM-4.7 Flash (text only) and two Gemini Flash-Lite models
  (vision + audio), switchable at runtime with `/model`.
* **Search**: `ddgs` (DuckDuckGo), with optional Exa neural search if `EXA_API_KEY` is set
* **Media**: `ffmpeg` (via `imageio-ffmpeg`, no system package needed) — frame
  extraction for video/GIF, 16kHz mono FLAC re-encoding for speech
* **Database**: Neon Postgres via `asyncpg`, schema auto-created on startup
* **Deployment**: Render free tier + `UptimeRobot` for keep-alive

---

## 1. Project Directory Structure
```text
brodar-ai-bot/
├── PERSONA.md               # The bot's fixed identity/voice/rules (code-owned)
├── MEMORY.md                # Mutable learned facts, editable by admins/the bot itself
├── skills/                  # Hermes-style SKILL.md instruction directory
│   ├── bot-architecture/    # This file's in-chat equivalent — self-debugging map
│   ├── group_admin/         # Moderation actions and the approval rules around them
│   ├── telegram-directory/  # What member/id lookups can and can't answer
│   ├── web_research/        # When to search vs. answer from memory
│   ├── send-media/          # Delivering files back to the chat
│   ├── chart-generation/, image-generation/, jailbreak_roast/, system_diagnostics/
├── config.py                 # Env var loading, validation, and small derived helpers
├── db.py                     # asyncpg pool + schema + all SQL (chat settings, history,
│                              #   chat_members incl. Cyrillic/Latin fold, visuals, sessions)
├── cache.py                  # In-memory write-through layer in front of db.py
├── models.py                 # The model registry LiteLLM calls route through
├── media.py                  # ffmpeg wrapper: frame extraction, audio re-encoding
├── response_mode.py          # Per-turn "extraction vs conversation" classifier
├── tools.py                  # Sandboxed shell (whitelisted commands) + web search
├── group_tools.py            # Every Bot-API moderation call (ban/mute/permissions/
│                              #   invite links/join requests/forum topics/...)
├── skills.py                 # SKILL.md discovery, caching, install/uninstall
├── memory.py                 # PERSONA.md / MEMORY.md read-write + Postgres sync
├── session_manager.py        # Multi-session support and context-compaction checkpoints
├── agent.py                  # System prompt builder, tool schema, tool-execution
│                              #   loop, and the approval flow
├── bot.py                    # aiogram router: middleware, commands, message handling,
│                              #   join/leave tracking, approval callback
├── main.py                   # FastAPI app, webhook endpoint, startup/shutdown
├── requirements.txt
├── .env.example
├── run_tests.py               # Runs every test_phaseN.py suite in sequence
└── test_phase*.py, test_support.py   # Dependency-free test suites (see below)
```

---

## 2. Database Schema

The full schema is created automatically on startup by `db.init_schema()` — you
never run SQL by hand. Point `DATABASE_URL` at a Neon database and start the app.
The tables that actually exist (see `db.py:SCHEMA_STATEMENTS` for the authoritative
source, including every index and migration):

| Table | Holds |
|---|---|
| `chats` | Per-chat settings: activation, mention-only mode, tool-notes toggle, turn counters |
| `messages` | Long-term conversation history, keyed by chat and insertion order |
| `allowed_users` | Dynamically DM-allowlisted user ids |
| `sessions` / `session_summaries` | Multi-session support and compaction checkpoints |
| `memory_store` | `MEMORY.md` and runtime `PERSONA.md` edits, synced to disk on boot |
| `recent_visuals` | Retained media (images, extracted frames, audio) for follow-up questions, tagged by kind |
| `chat_members` | Everyone the bot has seen — via messages, joins/leaves/promotions (`chat_member` updates), or a live `getChatAdministrators` call. Carries a folded `search_key` (lowercased, Cyrillic→Latin) for token-wise name search, and `left_at` (marks departed rather than deleting) |

---

## 3. How to Obtain Credentials

### A. Telegram Bot Token
1. Open Telegram, message [@BotFather](https://t.me/BotFather).
2. `/newbot`, follow the prompts.
3. Save the token (`123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).

### B. Z.ai (GLM) API Key
1. Register at the [Zhipu AI / Z.ai platform](https://open.bigmodel.cn/).
2. Generate an API key from the console.

### C. Gemini API Key (needed for vision + audio)
1. Get one at [Google AI Studio](https://aistudio.google.com/apikey).
2. Without this set, `models.resolve_spec()` falls back to the text-only GLM model
   and every image/voice feature silently does nothing — set it before relying on
   media understanding.

### D. Neon Postgres
1. Sign up at [Neon.tech](https://neon.tech/), create a project.
2. Copy the pooled connection string, append `?sslmode=require` if missing.

---

## 4. Local Development

You'll need something like **ngrok** to expose local port 8000 for Telegram's webhook.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values; see section 3
python main.py
```

Set `WEBHOOK_URL` in `.env` to your ngrok URL before starting.

### Running the tests

No real Telegram/Postgres/LLM connection needed — `test_support.install_stubs()`
fakes just enough of `aiogram`/`asyncpg` for the bot's own logic to import and run.

```bash
python3 -m py_compile *.py     # syntax/import sanity
python3 run_tests.py           # every test_phaseN.py suite, one summary at the end
python3 test_phase9.py         # or run just one suite directly
```

---

## 5. Deploying to Render (Free Tier)

1. Push to a GitHub repository.
2. Render Dashboard → **New** → **Blueprint**, link the repo (`render.yaml` is included).
3. Set the required env vars: `TELEGRAM_BOT_TOKEN`, `ZAI_API_KEY` and/or `GEMINI_API_KEY`,
   `DATABASE_URL`, `WEBHOOK_URL` (your Render app URL, no trailing slash), `MAIN_ACCOUNT_ID`.
   `WEBHOOK_SECRET_TOKEN` is generated for you.
4. Deploy. `config.validate_config()` logs loudly (without crash-looping) if anything
   required is still missing or a placeholder — check the logs after first boot.

**Two things silently degrade the bot if left unset, with no crash to warn you:**
- `GEMINI_API_KEY` — without it, the model falls back to text-only GLM and every
  vision/audio feature does nothing.
- `MAIN_ACCOUNT_ID` — without it, approvals fall back to the first
  `ALLOWED_DM_USER_IDS` entry (or fail closed if that's empty too). Set explicitly,
  or run `/set_main_account` once the bot is live.

---

## 6. How the Bot Actually Works

### Task modes: extraction vs conversation
`response_mode.classify()` decides, per turn, whether the user wants something
extracted verbatim from media (a transcript, translation, OCR read) or just wants
to chat. Extraction mode drops the persona's lowercase/joke/brevity rules for that
one reply, uses a lower LLM temperature (`EXTRACTION_TEMPERATURE`), and — past a
few Telegram message chunks — delivers the result as a file instead of a wall of
text. See `PERSONA.md`'s "When You're Given a Job" section and `agent.build_system_prompt()`.

### Media pipeline
`media.py` extracts JPEG frames from video/GIF/stickers and re-encodes audio to
16kHz mono FLAC (falling back to 96kbps MP3 only if that overruns a byte budget) —
lossless at the rate a speech model actually uses, unlike the 32kbps MP3 this used
to ship with. A language-hint block naming the likely spoken languages
(`SPEECH_LANGUAGES`, default Uzbek/Russian/English) is injected on any turn
carrying audio. Truncation (`MEDIA_MAX_AUDIO_SECONDS`) is always reported to the
user, never silent.

### Approvals
Destructive or self-modifying tool calls (`kick_ban`, `mute`, `.env`/persona edits,
skill install/uninstall, ...) require an explicit human tap before they run — see
`agent._tool_needs_approval()` for the full list. The prompt is sent to
`config.approval_recipient_id()`'s **DM only**, carrying a provenance card (who it
affects, where it came from, who asked, the triggering message, what it does), and
is edited in place with the outcome once resolved — never posted in the group that
triggered it, and only that one account can tap it.

### Member lookup
The Bot API has no method to list every member of a group. `search_group_members`
combines a live `getChatAdministrators` call (always accurate) with the bot's own
memory of who it's seen (via messages, `chat_member` join/leave/promotion updates,
or a prior admin check) — with Cyrillic↔Latin name folding so a Latin-typed query
finds a Cyrillic-stored name. See `skills/telegram-directory/SKILL.md`.

---

## 7. Shell Sandbox

`tools.execute_shell_command` runs with `shell=False`, a hard timeout, and secrets
stripped from the subprocess environment. Commands are whitelisted in
`ALLOWED_COMMANDS` (`tools.py`) with a strict regex per command validating
arguments; a per-chat session remembers `cwd` across calls (bounded, LRU-evicted).
To add a command, add an entry there with a regex tight enough to reject anything
that could escape the sandbox or reach `.env`.

---

## 8. Changelog

Entries below are historical — each describes the state *at that date*, not
necessarily today's. For current behavior, read section 6 and the module docstrings;
`git log` has the full detail behind every fix.

### Rebrand to Claire; Phase 10 — web fetch, download, deliver (2026-07-30)
The project moved to a new repo under the name **claire**, with the bot's own
identity (persona, in-code voice references, test fixtures, skill docs) renamed
to match — see `PERSONA.md`. Separately, added `webio.py` with two new tools:
`fetch_url` (reads a page/API response as text, HTML reduced to readable text)
and `download_url` (saves a file into the chat's workspace, per-chat quota with
LRU eviction). Both go through a shared SSRF guard — rejects non-http(s)
schemes, embedded credentials, non-standard ports, and any hostname that
resolves to a private/loopback/link-local/CGNAT/multicast/reserved address,
re-checked on every redirect hop. `send_file` gained extension-based type
inference and pre-upload size checks, and can now take an `http(s)://` URL
directly (Telegram fetches it, no local download) as an alternative to
`download_url` + a local path. Also closed a bare-IP-literal hole in the
`curl` shell tool's allowlist regex.

### Recovery plan, phases 1–9 (2026-07-26)
A large prior session had added many features that didn't actually work end to end.
Fixed across nine phases: freeze-prone paths and security holes (unbounded waits,
no timeouts, a broken shell sandbox); one persona/one prompt builder instead of
several contradicting blocks; a rebuilt media engine with real audio support;
task-mode separation so "transcribe this" doesn't get a joke instead of a
transcript; 16kHz FLAC speech re-encoding with language hints (the old 32kbps MP3
was why Uzbek came back as Turkish); member lookup that actually finds people
(Cyrillic/Latin folding, live admin merge, `chat_member` event tracking); and
approvals that go to the master admin's DM with real provenance instead of the
group chat, gating every destructive moderation action behind a tap even when an
admin asks directly. See `git log` for the full per-phase commit messages.

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
* **Telegram Interactive Permission Prompt System (`permissions.py`, since removed — superseded by the approval flow in `agent.py`/`bot.py`)**: Non-whitelisted commands or `.env` file read attempts send an **Inline Keyboard message** with `[ ✅ Approve ]` and `[ ❌ Reject ]` buttons. Restricted to Group Admins in group chats with non-admin toast alerts.
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
