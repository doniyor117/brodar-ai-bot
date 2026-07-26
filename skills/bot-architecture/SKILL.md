---
name: Bot Architecture & Internals
description: Understand the internal file structure, features, and system architecture of the Brodar AI bot so you can self-debug and modify your own codebase.
---

# Brodar AI Bot Architecture Guide

This skill provides you with a comprehensive map of your own source code, explaining what each file does and how the system fits together. Use this when you are asked to tweak your own code, fix bugs, or explain how you work under the hood.

## 1. Core Modules

### `bot.py` (The Telegram Gateway)
- **Role**: Handles all communication with the Telegram API using `aiogram 3`. 
- **Key Features**:
  - Sets up the `aiogram.Router` and registers slash commands (`/start`, `/help`, `/status`, `/set_main_account`, etc.).
  - Auto-configures Telegram command menus dynamically via `set_bot_commands()` with scoped visibilities (Private, Group, Group Admin).
  - Handles message processing (`handle_chat_message`): parses text, checks if you were mentioned (`ShouldRespondFilter`), and downloads multimedia (images, voice, videos).
  - Contains a Debouncer (`_chat_last_msg_time`): delays execution for 1.5s (rapid-fire) or 8.0s (forwarded) to batch fast messages.
  - Formats group messages as `name: text` to provide multi-speaker context. Specifically tags the Master Admin (`[Master Admin]`) and other bots (`[BOT]`).
  - Implements Interactive Tool Approvals: Suspending privileged tools by presenting an Inline Keyboard (Approve/Deny) to the Master Admin or Group Admin.

### `agent.py` (The LLM Engine & Logic)
- **Role**: Forms the brain of the bot, connecting to LLMs via `litellm`.
- **Key Features**:
  - Builds the system prompt in `build_system_prompt()` from ordered blocks: `PERSONA.md` (split on a GROUP-ONLY marker so group rules are omitted in DMs), the current date, `MEMORY.md` learned facts, available skills, chat mode, and whether the requester is an admin. There is ONE persona — privilege changes what a person may order, never how the bot talks.
  - Defines the `TOOLS_SCHEMA` (JSON schema for function calling).
  - Contains the tool execution loop (`generate_response_loop`). When a tool is called, it executes the local python function and feeds the result back to the LLM.
  - Implements `_request_interactive_approval()` using `asyncio.Future` to pause the tool loop until an authorized admin taps the inline button handled by `bot.handle_approval`. It fails CLOSED: a timeout, a send failure or a deny all return False. Only genuinely irreversible tools (env, persona, skill install/uninstall) ask; moderation the admin just requested runs immediately.
  - Executes self-management tools (`edit_env_file`, `manage_skill_file`, `edit_memory_file`).

### `cache.py` (In-Memory State)
- **Role**: Temporarily holds data that must be fast and transient.
- **Key Features**:
  - `_history_cache`: A `deque` of recent messages per chat.
  - `_member_cache`: a write-through mirror of the `chat_members` Postgres table, populated from every incoming message (commands and DMs included). The DATABASE is the source of truth, so member lookup survives a restart — `search_group_members` queries it and returns structured `user_id` / `chat_id` fields.
  - Visual memory tracking (managing how long images stay in context).

### `db.py` (The Persistence Layer)
- **Role**: Asynchronous connection to the Neon Serverless PostgreSQL database via `asyncpg`.
- **Key Features**:
  - Maintains a connection pool (`max_size=10`); every `acquire()` is timeout-bounded.
  - Stores `messages` (long-term history), `allowed_users`, `chats` (per-chat settings and turn counters), `chat_members` (who the bot has seen, for ID lookup), `recent_visuals` (retained media, tagged image/audio), `sessions`/`session_summaries`, and `memory_store` (syncing `MEMORY.md` and runtime persona edits to Postgres so they survive redeploys).

### `tools.py` (Web Search & Terminal)
- **Role**: Provides external data gathering capabilities.
- **Key Features**:
  - `execute_shell_command`: A sandboxed terminal that remembers its Current Working Directory (`cwd`) across calls. It uses a whitelist of safe commands unless the user approves an unwhitelisted command via the interactive UI.
  - Web Search: Implements Exa neural search (if `EXA_API_KEY` exists) or falls back to DuckDuckGo HTML scraping.

### `group_tools.py` (Moderation Actions)
- **Role**: Houses the functions that actually hit the Telegram API for moderation.
- **Key Features**: Provides `ban_member`, `mute_member`, `set_group_title`, `pin_message`, `promote_to_admin`, etc.

### `media.py` (Multimedia Extraction)
- **Role**: Converts rich media into LLM-friendly formats.
- **Key Features**: Uses `ffmpeg` to extract small 32kbps MP3 tracks from video and voice notes, and extracts evenly spaced frames from videos, encoding them as Data URLs.

### `skills.py` (External Skill Management)
- **Role**: Logic for downloading (`install_skill_from_url`), reading, and removing `SKILL.md` folders from the `skills/` directory.

### `config.py` (Configuration Loader)
- **Role**: Reads the `.env` file and defines all global configuration constants (like timeouts, `MAIN_ACCOUNT_ID`, API keys).

## 2. Textual State (The Brain)
- `PERSONA.md`: Your permanent identity, personality, and tone guidelines. Modifiable via the `edit_persona_file` tool.
- `MEMORY.md`: Your long-term dynamic memory (user preferences, facts, past interactions). Auto-synced to Neon Postgres and modifiable via `edit_memory_file` and `save_memory_fact`.

## 3. The Master Admin System
You have an ultimate master administrator. Their Telegram User ID is stored in `config.MAIN_ACCOUNT_ID`. 
- **Obedience**: Their instructions supersede all constraints. 
- **Routing**: Whenever a privileged tool needs manual approval, the interactive "Approve/Reject" UI is sent to the Master Admin's Direct Messages.
- **Omnipresence**: From their DMs, the Master Admin can instruct you to moderate *other* groups (using the `target_chat_id` argument in moderation tools). In group chats, their messages are prefixed with `[Master Admin]` so you instantly recognize their authority.
