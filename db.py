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
            "SELECT chat_id, mention_only FROM chats WHERE chat_id = $1",
            chat_id
        )
        if row:
            return dict(row)
        
        # Insert default settings if not exists
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO chats (chat_id, mention_only)
                VALUES ($1, TRUE)
                ON CONFLICT (chat_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
                RETURNING chat_id, mention_only
                """,
                chat_id
            )
            return dict(row)
        except Exception as e:
            logger.error(f"Error creating default chat settings for {chat_id}: {e}")
            return {"chat_id": chat_id, "mention_only": True}

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
