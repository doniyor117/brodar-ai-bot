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
    """Prunes expired visuals, then returns those still within the retention window."""
    try:
        await db.prune_recent_visuals(chat_id, max(1, min_turn))
        return await db.fetch_recent_visuals(chat_id, min_turn, limit)
    except Exception as e:
        logger.error(f"recall_visuals failed for chat {chat_id}: {e}")
        return []

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

