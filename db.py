import asyncio
import logging
import re
import asyncpg
from typing import List, Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

# Cyrillic -> Latin transliteration, covering both Russian and the
# Uzbek-specific letters (ў, қ, ғ, ҳ). This is what lets a Latin-typed query
# find a Cyrillic-stored name and vice versa — the normal case in these chats,
# where the same person's Telegram display name might read "Aziz Karimov" or
# "Азиз Каримов" depending on which keyboard their phone was using that day.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "u", "қ": "q", "ғ": "g", "ҳ": "h",
}
_NON_SEARCH_CHARS_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def fold_name(s: str) -> str:
    """
    Normalize a name/username for matching: lowercase, transliterate Cyrillic
    to Latin, strip everything but letters/digits/spaces.

    Used both when writing `search_key` on upsert and when folding an incoming
    query, so the two sides always compare in the same alphabet regardless of
    which script the source text used.
    """
    if not s:
        return ""
    folded = "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in s.lower())
    folded = _NON_SEARCH_CHARS_RE.sub(" ", folded)
    return _MULTI_SPACE_RE.sub(" ", folded).strip()

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
    # Recently-sent media (images, extracted video frames, audio), kept for a few
    # turns so follow-up questions about them still work.
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
    # "image" or "audio". Without it, audio rows were indistinguishable from
    # image rows on read-back and got rebuilt as image content blocks.
    "ALTER TABLE recent_visuals ADD COLUMN IF NOT EXISTS kind VARCHAR(16) DEFAULT 'image' NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_recent_visuals_chat_turn ON recent_visuals (chat_id, turn)",
    # Who the bot has seen, per chat. This used to be an in-process dict with no
    # table behind it at all — and on Render's free tier the process sleeps
    # constantly, so the lookup table was empty most of the time. That is why
    # "find the id of @someone" reliably failed.
    """
    CREATE TABLE IF NOT EXISTS chat_members (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        username TEXT,
        full_name TEXT,
        is_bot BOOLEAN DEFAULT FALSE NOT NULL,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_members_user ON chat_members (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_members_username ON chat_members (lower(username))",
    # Folded (lowercase, Cyrillic->Latin, punctuation-stripped) "full_name
    # username", written by upsert_chat_member and matched token-wise by
    # search_chat_members. left_at marks someone as gone without deleting their
    # row, so a search for someone who left last week still resolves instead of
    # just vanishing. is_admin is a best-effort cache of the last live check —
    # group_tools.list_admins() is the authoritative source and refreshes it.
    "ALTER TABLE chat_members ADD COLUMN IF NOT EXISTS search_key TEXT",
    "ALTER TABLE chat_members ADD COLUMN IF NOT EXISTS left_at TIMESTAMP",
    "ALTER TABLE chat_members ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_chat_members_search_key ON chat_members (search_key)",
    # One-time ASCII-only backfill for rows written before search_key existed.
    # Real Cyrillic folding needs Python, so this only ever fills NULLs — every
    # live upsert_chat_member call afterwards recomputes the real thing.
    """
    UPDATE chat_members SET search_key = lower(regexp_replace(
        COALESCE(full_name, '') || ' ' || COALESCE(username, ''),
        '[^a-zA-Z0-9 ]', ' ', 'g'
    )) WHERE search_key IS NULL
    """,
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
    """Stores this turn's media and marks it as the chat's last visual turn."""
    if not items:
        return
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            for it in items:
                await conn.execute(
                    "INSERT INTO recent_visuals (chat_id, turn, data_url, is_gif, kind) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    chat_id, turn, it["data_url"], bool(it.get("is_gif")),
                    it.get("kind") or "image",
                )
            await conn.execute(
                "UPDATE chats SET last_visual_turn = $2 WHERE chat_id = $1",
                chat_id, turn,
            )


async def fetch_recent_visuals(chat_id: int, min_turn: int, limit: int) -> List[Dict[str, Any]]:
    """
    Retained media from turns STRICTLY BEFORE `before_turn` is the caller's job
    to filter; this returns everything with turn >= min_turn, oldest first,
    capped at `limit`.

    The cap applies to retained history only. It must never be used to cap the
    current turn's own media: this is "ORDER BY id DESC LIMIT n", so applying a
    limit of 5 to a video that produced 10 frames kept only the LAST five and
    silently discarded the beginning of the clip the user had just sent.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            """
            SELECT data_url, is_gif, turn, kind FROM recent_visuals
            WHERE chat_id = $1 AND turn >= $2
            ORDER BY id DESC
            LIMIT $3
            """,
            chat_id, min_turn, limit,
        )
        # DESC + reverse => newest kept under the cap, returned oldest-first.
        return [dict(r) for r in reversed(rows)]


async def prune_recent_visuals(chat_id: int, min_turn: int) -> None:
    """Deletes media older than the retention window for a chat."""
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            "DELETE FROM recent_visuals WHERE chat_id = $1 AND turn < $2",
            chat_id, min_turn,
        )


async def upsert_chat_member(
    chat_id: int, user_id: int, username: Optional[str],
    full_name: Optional[str], is_bot: bool = False,
    is_admin: Optional[bool] = None,
) -> None:
    """
    Record (or refresh) one person's presence in a chat.

    `is_admin=None` means "unknown/don't change" — a message from a regular
    member shouldn't silently overwrite what a live getChatAdministrators call
    established. Pass True/False explicitly from a source that actually knows
    (a chat_member update, or group_tools.list_admins()).

    Rejoining clears left_at; search_key is recomputed from whatever the row's
    username/full_name end up being after the COALESCE, in a second statement,
    since the transliteration fold itself has to happen in Python.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_members (chat_id, user_id, username, full_name, is_bot, is_admin, last_seen, left_at)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, FALSE), CURRENT_TIMESTAMP, NULL)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                username  = COALESCE(EXCLUDED.username, chat_members.username),
                full_name = COALESCE(EXCLUDED.full_name, chat_members.full_name),
                is_bot    = EXCLUDED.is_bot,
                is_admin  = COALESCE($6, chat_members.is_admin),
                last_seen = CURRENT_TIMESTAMP,
                left_at   = NULL
            RETURNING username, full_name
            """,
            chat_id, user_id, username, full_name, is_bot, is_admin,
        )
        search_key = fold_name(f"{row['full_name'] or ''} {row['username'] or ''}")
        await conn.execute(
            "UPDATE chat_members SET search_key = $1 WHERE chat_id = $2 AND user_id = $3",
            search_key, chat_id, user_id,
        )


async def mark_member_left(chat_id: int, user_id: int) -> None:
    """
    Marks someone as gone from a chat without deleting their row, so a search
    for them still resolves ("aziz left last week, but here's his id") instead
    of them just vanishing the moment they leave.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            "UPDATE chat_members SET left_at = CURRENT_TIMESTAMP WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )


async def search_chat_members(
    chat_id: Optional[int], query: str, limit: int = 25,
) -> List[Dict[str, Any]]:
    """
    Find members by username, display name, or user id — token-wise and
    script-folded.

    A single whole-phrase `LIKE '%…%'` (the old behaviour) missed "Karimov
    Aziz" when asked for "aziz karimov", and a Latin-typed query never matched
    a Cyrillic-stored name at all. Here the query is split into tokens and each
    is matched independently against the folded search_key, then results are
    ranked in Python: exact match > prefix match > token-hit count > recency.
    Only current members (left_at IS NULL) are returned — see mark_member_left.
    """
    pool = get_pool()
    q_raw = (query or "").strip().lstrip("@")
    q_folded = fold_name(q_raw)
    tokens = [t for t in q_folded.split(" ") if t]
    id_query = bool(q_raw) and q_raw.lstrip("-").isdigit()

    conditions, params = ["left_at IS NULL"], []
    if chat_id is not None:
        params.append(chat_id)
        conditions.append(f"chat_id = ${len(params)}")

    match_clauses = []
    for tok in tokens:
        params.append(f"%{tok}%")
        match_clauses.append(f"search_key LIKE ${len(params)}")
    if id_query:
        params.append(int(q_raw))
        match_clauses.append(f"user_id = ${len(params)}")
    if match_clauses:
        conditions.append("(" + " OR ".join(match_clauses) + ")")

    where = "WHERE " + " AND ".join(conditions)

    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            f"""
            SELECT chat_id, user_id, username, full_name, is_bot, is_admin, last_seen
            FROM chat_members {where}
            ORDER BY last_seen DESC
            LIMIT 500
            """,
            *params,
        )

    candidates = [dict(r) for r in rows]
    if not tokens:
        return candidates[:limit]

    candidates.sort(key=lambda row: _member_match_score(row, q_folded, tokens), reverse=True)
    return candidates[:limit]


def _member_match_score(row: Dict[str, Any], q_folded: str, tokens: List[str]) -> tuple:
    """
    Rank one candidate row against a folded query: exact match > prefix match >
    number of tokens that hit > (recency is preserved for free by the stable
    sort, since rows arrive ORDER BY last_seen DESC).

    Pulled out of search_chat_members as its own function so it's directly
    unit-testable without a database connection.
    """
    uname = fold_name(row.get("username") or "")
    fname = fold_name(row.get("full_name") or "")
    exact = 1 if (uname == q_folded or fname == q_folded) else 0
    prefix = 1 if (uname.startswith(q_folded) or fname.startswith(q_folded)) else 0
    hits = sum(1 for t in tokens if t in uname or t in fname)
    return (exact, prefix, hits)


async def clear_recent_visuals(chat_id: int) -> None:
    """
    Drops every retained visual for a chat and resets its turn counters.

    /clear did neither, so a "fresh start" immediately re-attached images from
    before the clear — the user wiped the history and the bot kept talking about
    the old pictures.
    """
    pool = get_pool()
    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM recent_visuals WHERE chat_id = $1", chat_id)
            await conn.execute(
                "UPDATE chats SET msg_turn = 0, last_visual_turn = 0 WHERE chat_id = $1",
                chat_id,
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
