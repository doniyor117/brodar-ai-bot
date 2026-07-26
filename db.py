import asyncio
import logging
import asyncpg
from typing import List, Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

# Connection pool instance
_pool: Optional[asyncpg.Pool] = None

# Full schema. Every table the bot touches is created here at startup.
# Previously `chats` and `messages` only existed as SQL in README.md, so on a
# fresh database every settings read and history write failed silently and
# group activation lived only in process memory.
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS chats (
        chat_id BIGINT PRIMARY KEY,
        mention_only BOOLEAN DEFAULT FALSE NOT NULL,
        is_active BOOLEAN DEFAULT FALSE NOT NULL,
        show_tool_notes BOOLEAN DEFAULT TRUE NOT NULL
    )
    """,
    # Migration for chats tables created before show_tool_notes existed.
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS show_tool_notes BOOLEAN DEFAULT TRUE NOT NULL",
    # Per-chat monotonic turn counter + the last turn that carried a visual,
    # used to age out retained visuals without extra queries.
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS msg_turn INTEGER DEFAULT 0 NOT NULL",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_visual_turn INTEGER DEFAULT 0 NOT NULL",
    # Recently-sent images/GIF frames, kept for a few turns for follow-up vision.
    """
    CREATE TABLE IF NOT EXISTS recent_visuals (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        turn INTEGER NOT NULL,
        data_url TEXT NOT NULL,
        is_gif BOOLEAN DEFAULT FALSE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recent_visuals_chat_turn ON recent_visuals (chat_id, turn)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        role VARCHAR(20) NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    # History reads are always "latest N for one chat" — without this they are
    # a full table scan that gets slower every day.
    """
    CREATE INDEX IF NOT EXISTS idx_messages_chat_created
        ON messages (chat_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS allowed_users (
        user_id BIGINT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        title VARCHAR(255) DEFAULT 'default' NOT NULL,
        is_active BOOLEAN DEFAULT TRUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sessions_chat_active
        ON sessions (chat_id, is_active)
    """,
    """
    CREATE TABLE IF NOT EXISTS session_summaries (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL,
        summary TEXT NOT NULL,
        compacted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_store (
        key VARCHAR(50) PRIMARY KEY,
        content TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
    )
    """,
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS session_id INTEGER",
]


async def init_db_pool() -> asyncpg.Pool:
    """Initializes the asyncpg connection pool and ensures the schema exists."""
    global _pool
    if _pool is not None:
        return _pool

    if not config.DATABASE_URL:
        logger.error("DATABASE_URL is not set. Database functions will not work.")
        raise ValueError("DATABASE_URL is not set.")

    logger.info("Initializing asyncpg connection pool...")
    try:
        _pool = await asyncpg.create_pool(
            dsn=config.DATABASE_URL,
            min_size=1,
            max_size=10,
            # Neon scales to zero; a cold start regularly takes longer than the
            # 5s this used to allow, which made the first query after idle fail.
            command_timeout=30.0,
            timeout=30.0,
            max_inactive_connection_lifetime=300.0,
        )
        # NOTE: command_timeout only bounds a query once a connection is held.
        # Waiting for a *free* connection is unbounded by default, so every
        # acquire() below passes timeout=config.DB_ACQUIRE_TIMEOUT — otherwise a
        # saturated pool leaves callers waiting forever and the bot goes quiet.
        logger.info("asyncpg connection pool initialized successfully.")
        await init_schema()
        return _pool
    except Exception as e:
        logger.error(f"Failed to initialize database pool: {e}")
        raise


async def init_schema() -> None:
    """Creates every table and index the bot needs. Safe to run repeatedly."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        failures = 0
        for statement in SCHEMA_STATEMENTS:
            try:
                await conn.execute(statement)
            except Exception as e:
                # Don't abort the whole schema over one statement — create what we
                # can so the bot runs as much as possible, and surface the problem.
                failures += 1
                logger.error(f"Schema statement failed (continuing): {e}\nStatement: {statement.strip()[:120]}")
    if failures:
        logger.warning(f"Database schema verified with {failures} failed statement(s).")
    else:
        logger.info("Database schema verified (chats, messages, allowed_users, sessions, summaries, memory_store).")


async def close_db_pool():
    """Closes the asyncpg connection pool."""
    global _pool
    if _pool is not None:
        logger.info("Closing asyncpg connection pool...")
        await _pool.close()
        _pool = None
        logger.info("asyncpg connection pool closed.")

def get_pool() -> asyncpg.Pool:
    """Returns the current pool. Raises ValueError if not initialized."""
    if _pool is None:
        raise ValueError("Database pool is not initialized. Call init_db_pool first.")
    return _pool

async def fetch_chat_settings(chat_id: int) -> Dict[str, Any]:
    """
    Fetches the settings for a specific chat.
    If the chat doesn't exist, inserts the default settings and returns them.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        row = await conn.fetchrow(
            "SELECT chat_id, mention_only, is_active, show_tool_notes FROM chats WHERE chat_id = $1",
            chat_id
        )
        if row:
            return dict(row)

        # Insert default settings if not exists
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO chats (chat_id, mention_only, is_active, show_tool_notes)
                VALUES ($1, FALSE, FALSE, TRUE)
                ON CONFLICT (chat_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
                RETURNING chat_id, mention_only, is_active, show_tool_notes
                """,
                chat_id
            )
            return dict(row)
        except Exception as e:
            logger.error(f"Error creating default chat settings for {chat_id}: {e}")
            return {"chat_id": chat_id, "mention_only": False, "is_active": False, "show_tool_notes": True}

async def update_chat_settings(chat_id: int, mention_only: bool) -> None:
    """Updates the mention_only setting for a specific chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO chats (chat_id, mention_only)
            VALUES ($1, $2)
            ON CONFLICT (chat_id)
            DO UPDATE SET mention_only = EXCLUDED.mention_only
            """,
            chat_id, mention_only
        )

async def update_chat_active(chat_id: int, is_active: bool) -> None:
    """Updates the is_active status for a specific chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO chats (chat_id, is_active)
            VALUES ($1, $2)
            ON CONFLICT (chat_id)
            DO UPDATE SET is_active = EXCLUDED.is_active
            """,
            chat_id, is_active
        )


async def update_chat_tool_notes(chat_id: int, show_tool_notes: bool) -> None:
    """Updates whether tool-activity notes are shown for a specific chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO chats (chat_id, show_tool_notes)
            VALUES ($1, $2)
            ON CONFLICT (chat_id)
            DO UPDATE SET show_tool_notes = EXCLUDED.show_tool_notes
            """,
            chat_id, show_tool_notes
        )


async def fetch_chat_history(chat_id: int, limit: int = 20) -> List[Dict[str, str]]:
    """
    Fetches the recent conversation history for a chat.
    Returns a list of dicts with keys 'role' and 'content' sorted chronologically.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM messages
            WHERE chat_id = $1
            ORDER BY id DESC
            LIMIT $2
            """,
            chat_id, limit
        )
        # Order by id (monotonic insert order), NOT created_at: a user message and
        # the bot's reply are saved in one batch and share an identical timestamp,
        # so ordering by created_at scrambles who-said-what and breaks the model's
        # view of the conversation. id always reflects true insertion order.
        # Reverse to get chronological order (oldest to newest).
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        return history

async def save_message(chat_id: int, role: str, content: str) -> None:
    """Saves a single message to the database."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO messages (chat_id, role, content)
            VALUES ($1, $2, $3)
            """,
            chat_id, role, content
        )

async def save_messages_batch(chat_id: int, messages: List[Dict[str, str]]) -> None:
    """Saves a list of messages to the database in a transaction."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            for msg in messages:
                await conn.execute(
                    """
                    INSERT INTO messages (chat_id, role, content)
                    VALUES ($1, $2, $3)
                    """,
                    chat_id, msg["role"], msg["content"]
                )

async def clear_chat_history(chat_id: int) -> None:
    """Deletes conversation history for a chat from the database."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)


async def bump_turn(chat_id: int) -> Dict[str, int]:
    """
    Increments the per-chat turn counter and returns both it and the last turn
    that carried a visual (so callers can decide whether to look for retained
    images without an extra query).
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chats (chat_id, msg_turn) VALUES ($1, 1)
            ON CONFLICT (chat_id) DO UPDATE SET msg_turn = chats.msg_turn + 1
            RETURNING msg_turn, last_visual_turn
            """,
            chat_id,
        )
        if not row:
            return {"msg_turn": 0, "last_visual_turn": 0}
        return {"msg_turn": row["msg_turn"], "last_visual_turn": row["last_visual_turn"]}


async def add_recent_visuals(chat_id: int, turn: int, items: List[Dict[str, Any]]) -> None:
    """Stores this turn's visuals and marks it as the chat's last visual turn."""
    if not items:
        return
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            for it in items:
                await conn.execute(
                    "INSERT INTO recent_visuals (chat_id, turn, data_url, is_gif) VALUES ($1, $2, $3, $4)",
                    chat_id, turn, it["data_url"], bool(it.get("is_gif")),
                )
            await conn.execute(
                "UPDATE chats SET last_visual_turn = $2 WHERE chat_id = $1",
                chat_id, turn,
            )


async def fetch_recent_visuals(chat_id: int, min_turn: int, limit: int) -> List[Dict[str, Any]]:
    """Returns retained visuals with turn >= min_turn, oldest first, capped at `limit`."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            """
            SELECT data_url, is_gif, turn FROM recent_visuals
            WHERE chat_id = $1 AND turn >= $2
            ORDER BY id DESC
            LIMIT $3
            """,
            chat_id, min_turn, limit,
        )
        # DESC + reverse => newest kept under the cap, returned oldest-first.
        return [dict(r) for r in reversed(rows)]


async def prune_recent_visuals(chat_id: int, min_turn: int) -> None:
    """Deletes visuals older than the retention window for a chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            "DELETE FROM recent_visuals WHERE chat_id = $1 AND turn < $2",
            chat_id, min_turn,
        )


async def delete_old_messages(chat_id: int, keep_last_n: int) -> int:
    """
    Deletes all but the newest `keep_last_n` messages for a chat (used by
    compaction after the older messages have been folded into a summary).
    Returns the number of rows deleted.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        result = await conn.execute(
            """
            DELETE FROM messages
            WHERE chat_id = $1
              AND id NOT IN (
                  SELECT id FROM messages
                  WHERE chat_id = $1
                  ORDER BY id DESC
                  LIMIT $2
              )
            """,
            chat_id, keep_last_n,
        )
        # asyncpg returns a status string like "DELETE 42"
        try:
            return int(result.split()[-1])
        except Exception:
            return 0


async def clear_session_summaries(chat_id: int) -> None:
    """
    Deletes summary checkpoints for every session of a chat.
    /clear used to wipe messages but leave summaries behind, so the bot kept
    recalling "cleared" context from the injected checkpoint.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            DELETE FROM session_summaries
            WHERE session_id IN (SELECT id FROM sessions WHERE chat_id = $1)
            """,
            chat_id
        )

async def fetch_allowed_users() -> List[int]:
    """Fetches dynamically added allowed user IDs from the database."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch("SELECT user_id FROM allowed_users")
        return [r["user_id"] for r in rows]

async def add_allowed_user(user_id: int) -> None:
    """Adds a user ID to the allowed users table in DB."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO allowed_users (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id
        )

async def remove_allowed_user(user_id: int) -> None:
    """Removes a user ID from the allowed users table in DB."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", user_id)

async def fetch_or_create_active_session(chat_id: int) -> Dict[str, Any]:
    """Fetches current active session for a chat, or creates a 'default' session if missing."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        row = await conn.fetchrow(
            "SELECT id, chat_id, title, is_active FROM sessions WHERE chat_id = $1 AND is_active = TRUE ORDER BY created_at DESC LIMIT 1",
            chat_id
        )
        if row:
            return dict(row)

        new_row = await conn.fetchrow(
            "INSERT INTO sessions (chat_id, title, is_active) VALUES ($1, 'default', TRUE) RETURNING id, chat_id, title, is_active",
            chat_id
        )
        return dict(new_row)

async def create_new_session(chat_id: int, title: str = "new session") -> Dict[str, Any]:
    """Deactivates current sessions and creates a new active session for the chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            await conn.execute("UPDATE sessions SET is_active = FALSE WHERE chat_id = $1", chat_id)
            row = await conn.fetchrow(
                "INSERT INTO sessions (chat_id, title, is_active) VALUES ($1, $2, TRUE) RETURNING id, chat_id, title, is_active",
                chat_id, title
            )
            return dict(row)

async def list_chat_sessions(chat_id: int) -> List[Dict[str, Any]]:
    """Lists all sessions for a chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            "SELECT id, title, is_active, created_at FROM sessions WHERE chat_id = $1 ORDER BY created_at DESC",
            chat_id
        )
        return [dict(r) for r in rows]

async def switch_active_session(chat_id: int, session_id: int) -> bool:
    """Switches the active session for a chat to session_id."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            target = await conn.fetchrow("SELECT id FROM sessions WHERE id = $1 AND chat_id = $2", session_id, chat_id)
            if not target:
                return False
            await conn.execute("UPDATE sessions SET is_active = FALSE WHERE chat_id = $1", chat_id)
            await conn.execute("UPDATE sessions SET is_active = TRUE WHERE id = $1", session_id)
            return True

async def save_session_summary(session_id: int, summary: str) -> None:
    """Saves a context summary checkpoint for a session."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            "INSERT INTO session_summaries (session_id, summary) VALUES ($1, $2)",
            session_id, summary
        )

async def fetch_latest_session_summary(session_id: int) -> Optional[str]:
    """Fetches the latest summary checkpoint for a session."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        row = await conn.fetchrow(
            "SELECT summary FROM session_summaries WHERE session_id = $1 ORDER BY compacted_at DESC LIMIT 1",
            session_id
        )
        return row["summary"] if row else None

async def fetch_stored_memory(key: str = "memory_md") -> Optional[str]:
    """Fetches stored MEMORY.md content from Neon Postgres."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        row = await conn.fetchrow("SELECT content FROM memory_store WHERE key = $1", key)
        return row["content"] if row else None

async def save_stored_memory(content: str, key: str = "memory_md") -> None:
    """Persists MEMORY.md content to Neon Postgres."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO memory_store (key, content, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP
            """,
            key, content
        )
