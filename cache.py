import asyncio
import logging
from collections import deque
from typing import Dict, List, Optional
import db

logger = logging.getLogger(__name__)

# In-memory caches
# Maps chat_id (int) -> mention_only (bool)
_settings_cache: Dict[int, bool] = {}

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
