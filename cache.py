import asyncio
import logging
from collections import deque
from typing import Dict, List, Optional
import config
import db

logger = logging.getLogger(__name__)

# In-memory caches
# Maps chat_id (int) -> mention_only (bool)
_settings_cache: Dict[int, bool] = {}

# Maps chat_id (int) -> is_active (bool)
_active_cache: Dict[int, bool] = {}

# Maps chat_id (int) -> show_tool_notes (bool)
_tool_notes_cache: Dict[int, bool] = {}

# Maps chat_id (int) -> deque of messages (role/content dicts)
_history_cache: Dict[int, deque] = {}
HISTORY_MAXLEN = config.HISTORY_MAXLEN

# Strong references to background DB-write tasks. Without this the event loop
# only weakly references them and the GC can cancel a write before it lands.

# Write-through memory of who we've seen, so a lookup right after a message
# doesn't have to wait on Postgres. The DATABASE is the source of truth — this
# used to be the only store, with no table behind it, so every Render sleep
# wiped the bot's knowledge of everyone in the group.
# Maps chat_id -> user_id -> {"username", "full_name", "is_bot"}
_member_cache: Dict[int, Dict[int, dict]] = {}
_MEMBER_CACHE_CHAT_LIMIT = 200


def track_user(chat_id: int, user_id: int, name: str,
               username: Optional[str] = None, is_bot: bool = False,
               is_admin: Optional[bool] = None) -> None:
    """
    Record that a user was seen in a chat, in memory and in Postgres.

    Called from every incoming message — including commands and DMs, which the
    old call site skipped, so anyone who only ever sent commands was invisible
    to the lookup tool.

    `is_admin=None` (the default, used by ordinary message tracking) leaves
    the stored admin flag untouched — only a chat_member update or a live
    group_tools.list_admins() call actually knows admin status and should be
    allowed to change it.
    """
    if not chat_id or not user_id:
        return

    chat = _member_cache.setdefault(chat_id, {})
    existing = chat.get(user_id, {})
    record = {
        "username": username or existing.get("username"),
        "full_name": name or existing.get("full_name"),
        "is_bot": is_bot,
        "is_admin": is_admin if is_admin is not None else existing.get("is_admin", False),
    }
    chat[user_id] = record

    if len(_member_cache) > _MEMBER_CACHE_CHAT_LIMIT:
        _member_cache.pop(next(iter(_member_cache)), None)

    _spawn_db_write(db.upsert_chat_member(
        chat_id, user_id, record["username"], record["full_name"], is_bot,
        is_admin=is_admin,
    ))


def mark_member_left(chat_id: int, user_id: int) -> None:
    """Fire-and-forget: mark someone as gone from a chat (see db.mark_member_left)."""
    if not chat_id or not user_id:
        return
    _member_cache.get(chat_id, {}).pop(user_id, None)
    _spawn_db_write(db.mark_member_left(chat_id, user_id))


async def search_users(chat_id: Optional[int] = None, query: str = "",
                       limit: int = 25) -> List[dict]:
    """
    Find members by @username, display name, or user id.

    Returns a list of structured records with real chat_id / user_id fields.
    The old version returned {user_id: "name (in chat -100…)"}, which both
    collapsed the same person across different chats into one entry and forced
    the model to parse the chat id back out of an English sentence.
    """
    try:
        rows = await db.search_chat_members(chat_id, query, limit)
        if rows:
            return rows
    except Exception as e:
        logger.error(f"search_chat_members failed: {e}")

    # Fall back to whatever this process has seen since it started.
    q = (query or "").strip().lstrip("@").lower()
    chats = ({chat_id: _member_cache.get(chat_id, {})} if chat_id is not None
             else _member_cache)
    out = []
    for cid, users in chats.items():
        for uid, rec in users.items():
            haystack = f"{rec.get('username') or ''} {rec.get('full_name') or ''} {uid}".lower()
            if not q or q in haystack:
                out.append({
                    "chat_id": cid, "user_id": uid,
                    "username": rec.get("username"),
                    "full_name": rec.get("full_name"),
                    "is_bot": rec.get("is_bot", False),
                    "is_admin": rec.get("is_admin", False),
                })
    return out[:limit]
_write_tasks: set = set()

def _spawn_db_write(coro) -> None:
    """Fire-and-forget a DB write, keeping a strong reference and logging failures."""
    task = asyncio.create_task(coro)
    _write_tasks.add(task)
    task.add_done_callback(_write_tasks.discard)
    task.add_done_callback(_handle_db_write_error)

def _handle_db_write_error(task: asyncio.Task):
    """Callback to log exceptions from background DB writes."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"Background database write failed: {exc}", exc_info=exc)

async def get_chat_setting(chat_id: int) -> bool:
    """
    Retrieves the mention_only setting for a chat.
    Checks the cache first; on a miss, reads from the database.
    """
    if chat_id in _settings_cache:
        return _settings_cache[chat_id]

    try:
        settings = await db.fetch_chat_settings(chat_id)
        # Default False: once a group is /activate-d it replies to everything.
        # Defaulting to mention-only made an activated bot look dead unless
        # every message @-mentioned it.
        mention_only = settings.get("mention_only", False)
        _settings_cache[chat_id] = mention_only
        # Also cache is_active / show_tool_notes since we fetched the row
        if "is_active" in settings:
            _active_cache[chat_id] = settings["is_active"]
        if "show_tool_notes" in settings:
            _tool_notes_cache[chat_id] = settings["show_tool_notes"]
        return mention_only
    except Exception as e:
        logger.error(f"Error fetching settings for chat {chat_id} from DB, using default (False): {e}")
        return False

def set_chat_setting(chat_id: int, mention_only: bool) -> None:
    """
    Updates the setting in the cache and triggers an async background DB write.
    """
    _settings_cache[chat_id] = mention_only
    
    # Run DB write in background
    _spawn_db_write(db.update_chat_settings(chat_id, mention_only))

async def get_chat_active(chat_id: int) -> bool:
    """
    Retrieves the is_active setting for a chat.
    Checks the cache first; on a miss, reads from the database.
    """
    if chat_id in _active_cache:
        return _active_cache[chat_id]

    try:
        settings = await db.fetch_chat_settings(chat_id)
        is_active = settings.get("is_active", False)
        _active_cache[chat_id] = is_active
        # Also cache mention_only / show_tool_notes since we fetched the row
        if "mention_only" in settings:
            _settings_cache[chat_id] = settings["mention_only"]
        if "show_tool_notes" in settings:
            _tool_notes_cache[chat_id] = settings["show_tool_notes"]
        return is_active
    except Exception as e:
        logger.error(f"Error fetching active status for chat {chat_id} from DB, using default (False): {e}")
        return False

def set_chat_active(chat_id: int, is_active: bool) -> None:
    """
    Updates the active status in the cache and triggers an async background DB write.
    """
    _active_cache[chat_id] = is_active

    # Run DB write in background
    _spawn_db_write(db.update_chat_active(chat_id, is_active))

async def get_chat_tool_notes(chat_id: int) -> bool:
    """Returns whether tool-activity notes should be shown in this chat."""
    if chat_id in _tool_notes_cache:
        return _tool_notes_cache[chat_id]

    import config
    try:
        settings = await db.fetch_chat_settings(chat_id)
        show = settings.get("show_tool_notes", config.SHOW_TOOL_NOTES_DEFAULT)
        _tool_notes_cache[chat_id] = show
        if "mention_only" in settings:
            _settings_cache[chat_id] = settings["mention_only"]
        if "is_active" in settings:
            _active_cache[chat_id] = settings["is_active"]
        return show
    except Exception as e:
        logger.error(f"Error fetching tool-notes setting for chat {chat_id}: {e}")
        return config.SHOW_TOOL_NOTES_DEFAULT

def set_chat_tool_notes(chat_id: int, show_tool_notes: bool) -> None:
    """Updates the tool-notes setting in cache and triggers a background DB write."""
    _tool_notes_cache[chat_id] = show_tool_notes
    _spawn_db_write(db.update_chat_tool_notes(chat_id, show_tool_notes))

async def get_chat_history(chat_id: int) -> List[Dict[str, str]]:
    """
    Retrieves the conversation history for a chat.
    Checks the cache first; on a miss, reads the last 20 messages from DB.
    """
    if chat_id in _history_cache:
        return list(_history_cache[chat_id])

    try:
        history = await db.fetch_chat_history(chat_id, limit=HISTORY_MAXLEN)
        # Create a deque and populate it
        _history_cache[chat_id] = deque(history, maxlen=HISTORY_MAXLEN)
        return list(_history_cache[chat_id])
    except Exception as e:
        logger.error(f"Error fetching history for chat {chat_id} from DB: {e}")
        _history_cache[chat_id] = deque(maxlen=HISTORY_MAXLEN)
        return []

def add_message_to_cache(chat_id: int, role: str, content: str) -> None:
    """
    Synchronously appends a message to the in-memory history cache.
    Note: does NOT write to database. Use save_messages_async for write-through.
    """
    if chat_id not in _history_cache:
        # If cache is not initialized, we initialize it empty.
        # This will be populated from DB on next message if someone calls get_chat_history.
        _history_cache[chat_id] = deque(maxlen=HISTORY_MAXLEN)
    
    _history_cache[chat_id].append({"role": role, "content": content})

def save_messages_async(chat_id: int, messages: List[Dict[str, str]]) -> None:
    """
    Saves a batch of messages to the cache instantly and triggers an async database write.
    Each message is a dict with keys 'role' and 'content'.
    """
    # 1. Update Cache
    for msg in messages:
        add_message_to_cache(chat_id, msg["role"], msg["content"])
    
    # 2. Trigger Async DB Write
    _spawn_db_write(db.save_messages_batch(chat_id, messages))

# In-memory fallback turn counter if the DB is unreachable.
_turn_fallback: Dict[int, int] = {}

async def bump_chat_turn(chat_id: int) -> tuple:
    """Advances the per-chat turn counter. Returns (turn, last_visual_turn)."""
    try:
        info = await db.bump_turn(chat_id)
        return info["msg_turn"], info["last_visual_turn"]
    except Exception as e:
        logger.error(f"bump_turn failed for chat {chat_id}: {e}")
        _turn_fallback[chat_id] = _turn_fallback.get(chat_id, 0) + 1
        return _turn_fallback[chat_id], 0

async def remember_visuals(chat_id: int, turn: int, items: list) -> None:
    """Persists this turn's visuals for later follow-up reuse."""
    try:
        await db.add_recent_visuals(chat_id, turn, items)
    except Exception as e:
        logger.error(f"remember_visuals failed for chat {chat_id}: {e}")

async def recall_visuals(chat_id: int, min_turn: int, limit: int) -> list:
    """Prunes expired media, then returns what's still within the retention window."""
    try:
        await db.prune_recent_visuals(chat_id, max(1, min_turn))
        return await db.fetch_recent_visuals(chat_id, min_turn, limit)
    except Exception as e:
        logger.error(f"recall_visuals failed for chat {chat_id}: {e}")
        return []


async def prune_visuals(chat_id: int, min_turn: int) -> None:
    """
    Drop media that has aged out, without reading anything back.

    Pruning used to happen ONLY inside recall_visuals, which the passive
    group handler never calls — so in a mention-only group, multi-megabyte
    base64 rows accumulated in Postgres forever. Same when the retention window
    lapses with no new media: nothing read, so nothing pruned.
    """
    try:
        await db.prune_recent_visuals(chat_id, max(1, min_turn))
    except Exception as e:
        logger.error(f"prune_visuals failed for chat {chat_id}: {e}")


async def clear_visuals(chat_id: int) -> None:
    """Drops all retained media for a chat and resets its turn counters."""
    try:
        await db.clear_recent_visuals(chat_id)
    except Exception as e:
        logger.error(f"clear_visuals failed for chat {chat_id}: {e}")
    _turn_fallback.pop(chat_id, None)

def invalidate_history(chat_id: int) -> None:
    """
    Drops the in-memory history for a chat WITHOUT touching the DB, forcing the
    next read to reload from the database. Used after compaction trims the DB.
    """
    _history_cache.pop(chat_id, None)

async def clear_chat_history(chat_id: int) -> None:
    """Clears conversation history from in-memory cache and triggers background DB deletion."""
    if chat_id in _history_cache:
        _history_cache[chat_id].clear()

    _spawn_db_write(db.clear_chat_history(chat_id))
    # Also drop summary checkpoints, otherwise "cleared" context keeps getting
    # re-injected into the system prompt from the last compaction.
    _spawn_db_write(db.clear_session_summaries(chat_id))

# Global active model key (persisted in memory_store under "active_model").
_active_model_cache: Optional[str] = None

async def get_active_model() -> str:
    """Returns the globally selected model key, loading from DB on first call."""
    global _active_model_cache
    if _active_model_cache is not None:
        return _active_model_cache

    import config
    stored = None
    try:
        stored = await db.fetch_stored_memory("active_model")
    except Exception as e:
        logger.error(f"Failed to fetch active model from DB: {e}")
    _active_model_cache = stored or config.MODEL_NAME
    return _active_model_cache

def set_active_model(model_key: str) -> None:
    """Sets the global active model and persists it."""
    global _active_model_cache
    _active_model_cache = model_key
    _spawn_db_write(db.save_stored_memory(model_key, "active_model"))

# Set of allowed user IDs (combining env config and DB)
_allowed_users_cache: Optional[set] = None

async def is_user_allowed(user_id: int) -> bool:
    """
    Checks if a user ID is allowed for DMs and activation commands.
    Loads from env and DB on cache miss.
    """
    global _allowed_users_cache
    if _allowed_users_cache is None:
        import config
        _allowed_users_cache = set(config.ALLOWED_DM_USER_IDS)
        try:
            db_users = await db.fetch_allowed_users()
            _allowed_users_cache.update(db_users)
        except Exception as e:
            logger.error(f"Failed to fetch allowed users from DB: {e}")

    return user_id in _allowed_users_cache

def add_allowed_user(user_id: int) -> None:
    """Adds a user ID to the allowed cache and triggers DB write."""
    global _allowed_users_cache
    import config
    if _allowed_users_cache is not None:
        _allowed_users_cache.add(user_id)
    else:
        _allowed_users_cache = set(config.ALLOWED_DM_USER_IDS) | {user_id}

    _spawn_db_write(db.add_allowed_user(user_id))

def remove_allowed_user(user_id: int) -> None:
    """Removes a user ID from the allowed cache and triggers DB deletion."""
    global _allowed_users_cache
    if _allowed_users_cache is not None:
        _allowed_users_cache.discard(user_id)

    _spawn_db_write(db.remove_allowed_user(user_id))

