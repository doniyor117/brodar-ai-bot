import asyncio
import logging
import asyncpg
from typing import List, Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

# Connection pool instance
_pool: Optional[asyncpg.Pool] = None

async def init_db_pool() -> asyncpg.Pool:
    """Initializes the asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    if not config.DATABASE_URL:
        logger.error("DATABASE_URL is not set. Database functions will not work.")
        raise ValueError("DATABASE_URL is not set.")

    logger.info("Initializing asyncpg connection pool...")
    try:
        # Create connection pool with constrained size to fit Neon free tier
        _pool = await asyncpg.create_pool(
            dsn=config.DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=5.0,
            max_inactive_connection_lifetime=300.0, # 5 minutes lifetime for idle connections
        )
        logger.info("asyncpg connection pool initialized successfully.")
        return _pool
    except Exception as e:
        logger.error(f"Failed to initialize database pool: {e}")
        raise

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
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT chat_id, mention_only, is_active FROM chats WHERE chat_id = $1",
            chat_id
        )
        if row:
            return dict(row)
        
        # Insert default settings if not exists
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO chats (chat_id, mention_only, is_active)
                VALUES ($1, TRUE, FALSE)
                ON CONFLICT (chat_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
                RETURNING chat_id, mention_only, is_active
                """,
                chat_id
            )
            return dict(row)
        except Exception as e:
            logger.error(f"Error creating default chat settings for {chat_id}: {e}")
            return {"chat_id": chat_id, "mention_only": True, "is_active": False}

async def update_chat_settings(chat_id: int, mention_only: bool) -> None:
    """Updates the mention_only setting for a specific chat."""
    pool = get_pool()
    async with pool.acquire() as conn:
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
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (chat_id, is_active)
            VALUES ($1, $2)
            ON CONFLICT (chat_id) 
            DO UPDATE SET is_active = EXCLUDED.is_active
            """,
            chat_id, is_active
        )


async def fetch_chat_history(chat_id: int, limit: int = 20) -> List[Dict[str, str]]:
    """
    Fetches the recent conversation history for a chat.
    Returns a list of dicts with keys 'role' and 'content' sorted chronologically.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM messages
            WHERE chat_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            chat_id, limit
        )
        # Reverse to get chronological order (oldest to newest)
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        return history

async def save_message(chat_id: int, role: str, content: str) -> None:
    """Saves a single message to the database."""
    pool = get_pool()
    async with pool.acquire() as conn:
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
    async with pool.acquire() as conn:
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
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)

async def fetch_allowed_users() -> List[int]:
    """Fetches dynamically added allowed user IDs from the database."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """
        )
        rows = await conn.fetch("SELECT user_id FROM allowed_users")
        return [r["user_id"] for r in rows]

async def add_allowed_user(user_id: int) -> None:
    """Adds a user ID to the allowed users table in DB."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            INSERT INTO allowed_users (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING;
            """,
            user_id
        )

async def remove_allowed_user(user_id: int) -> None:
    """Removes a user ID from the allowed users table in DB."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", user_id)

async def init_session_tables() -> None:
    """Ensures sessions and session_summaries tables exist in DB."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                title VARCHAR(255) DEFAULT 'default' NOT NULL,
                is_active BOOLEAN DEFAULT TRUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_summaries (
                id SERIAL PRIMARY KEY,
                session_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                compacted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS session_id INTEGER;
            """
        )

async def fetch_or_create_active_session(chat_id: int) -> Dict[str, Any]:
    """Fetches current active session for a chat, or creates a 'default' session if missing."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await init_session_tables()
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
    async with pool.acquire() as conn:
        await init_session_tables()
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
    async with pool.acquire() as conn:
        await init_session_tables()
        rows = await conn.fetch(
            "SELECT id, title, is_active, created_at FROM sessions WHERE chat_id = $1 ORDER BY created_at DESC",
            chat_id
        )
        return [dict(r) for r in rows]

async def switch_active_session(chat_id: int, session_id: int) -> bool:
    """Switches the active session for a chat to session_id."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await init_session_tables()
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
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_summaries (session_id, summary) VALUES ($1, $2)",
            session_id, summary
        )

async def fetch_latest_session_summary(session_id: int) -> Optional[str]:
    """Fetches the latest summary checkpoint for a session."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary FROM session_summaries WHERE session_id = $1 ORDER BY compacted_at DESC LIMIT 1",
            session_id
        )
        return row["summary"] if row else None

async def init_memory_store_table() -> None:
    """Ensures memory_store table exists in DB."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_store (
                key VARCHAR(50) PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            """
        )

async def fetch_stored_memory(key: str = "memory_md") -> Optional[str]:
    """Fetches stored MEMORY.md content from Neon Postgres."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await init_memory_store_table()
        row = await conn.fetchrow("SELECT content FROM memory_store WHERE key = $1", key)
        return row["content"] if row else None

async def save_stored_memory(content: str, key: str = "memory_md") -> None:
    """Persists MEMORY.md content to Neon Postgres."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await init_memory_store_table()
        await conn.execute(
            """
            INSERT INTO memory_store (key, content, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP
            """,
            key, content
        )



