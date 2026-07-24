import asyncio
import logging
from collections import deque
from typing import Dict, List, Optional
import db

logger = logging.getLogger(__name__)

# In-memory caches
# Maps chat_id (int) -> mention_only (bool)
_settings_cache: Dict[int, bool] = {}

# Maps chat_id (int) -> is_active (bool)
_active_cache: Dict[int, bool] = {}

# Maps chat_id (int) -> deque of messages (role/content dicts)
_history_cache: Dict[int, deque] = {}
HISTORY_MAXLEN = 20

def _handle_db_write_error(task: asyncio.Task):
    """Callback to log exceptions from background DB writes."""
    try:
        task.result()
    except Exception as e:
        logger.error(f"Background database write failed: {e}", exc_info=True)

async def get_chat_setting(chat_id: int) -> bool:
    """
    Retrieves the mention_only setting for a chat.
    Checks the cache first; on a miss, reads from the database.
    """
    if chat_id in _settings_cache:
        return _settings_cache[chat_id]

    try:
        settings = await db.fetch_chat_settings(chat_id)
        mention_only = settings.get("mention_only", True)
        _settings_cache[chat_id] = mention_only
        # Also cache is_active since we fetched it
        if "is_active" in settings:
            _active_cache[chat_id] = settings["is_active"]
        return mention_only
    except Exception as e:
        logger.error(f"Error fetching settings for chat {chat_id} from DB, using default (True): {e}")
        return True

def set_chat_setting(chat_id: int, mention_only: bool) -> None:
    """
    Updates the setting in the cache and triggers an async background DB write.
    """
    _settings_cache[chat_id] = mention_only
    
    # Run DB write in background
    task = asyncio.create_task(db.update_chat_settings(chat_id, mention_only))
    task.add_done_callback(_handle_db_write_error)

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
        # Also cache mention_only since we fetched it
        if "mention_only" in settings:
            _settings_cache[chat_id] = settings["mention_only"]
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
    task = asyncio.create_task(db.update_chat_active(chat_id, is_active))
    task.add_done_callback(_handle_db_write_error)

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
    task = asyncio.create_task(db.save_messages_batch(chat_id, messages))
    task.add_done_callback(_handle_db_write_error)

async def clear_chat_history(chat_id: int) -> None:
    """Clears conversation history from in-memory cache and triggers background DB deletion."""
    if chat_id in _history_cache:
        _history_cache[chat_id].clear()

    task = asyncio.create_task(db.clear_chat_history(chat_id))
    task.add_done_callback(_handle_db_write_error)

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

    task = asyncio.create_task(db.add_allowed_user(user_id))
    task.add_done_callback(_handle_db_write_error)

def remove_allowed_user(user_id: int) -> None:
    """Removes a user ID from the allowed cache and triggers DB deletion."""
    global _allowed_users_cache
    if _allowed_users_cache is not None:
        _allowed_users_cache.discard(user_id)

    task = asyncio.create_task(db.remove_allowed_user(user_id))
    task.add_done_callback(_handle_db_write_error)

