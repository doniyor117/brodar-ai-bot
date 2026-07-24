import logging
from typing import Dict, List, Any, Optional
import db
import cache

logger = logging.getLogger(__name__)

# Keep the last 12 messages intact during compaction
COMPACTION_KEEP_COUNT = 12

async def get_active_session(chat_id: int) -> Dict[str, Any]:
    """Retrieves or creates the active session for a chat."""
    try:
        return await db.fetch_or_create_active_session(chat_id)
    except Exception as e:
        logger.error(f"Failed to fetch active session for chat {chat_id}: {e}")
        return {"id": 0, "chat_id": chat_id, "title": "default", "is_active": True}

async def new_session(chat_id: int, title: str = "new session") -> Dict[str, Any]:
    """Creates a new active session and clears local history cache."""
    try:
        session = await db.create_new_session(chat_id, title)
        # Clear in-memory history cache so next message fetches new session context
        await cache.clear_chat_history(chat_id)
        return session
    except Exception as e:
        logger.error(f"Failed to create new session for chat {chat_id}: {e}")
        return {"id": 0, "chat_id": chat_id, "title": title, "is_active": True}

async def list_sessions(chat_id: int) -> List[Dict[str, Any]]:
    """Lists all sessions for a chat."""
    try:
        return await db.list_chat_sessions(chat_id)
    except Exception as e:
        logger.error(f"Failed to list sessions for chat {chat_id}: {e}")
        return []

async def switch_session(chat_id: int, session_id: int) -> bool:
    """Switches active session and clears history cache."""
    try:
        success = await db.switch_active_session(chat_id, session_id)
        if success:
            await cache.clear_chat_history(chat_id)
        return success
    except Exception as e:
        logger.error(f"Failed to switch session to {session_id}: {e}")
        return False

async def get_session_summary(session_id: int) -> Optional[str]:
    """Fetches the latest context summary for a session."""
    if session_id <= 0:
        return None
    try:
        return await db.fetch_latest_session_summary(session_id)
    except Exception as e:
        logger.error(f"Failed to fetch session summary for session {session_id}: {e}")
        return None

async def compact_session_history(chat_id: int) -> str:
    """
    Summarizes older conversation history into a context checkpoint
    while retaining the last 12 messages intact.
    """
    session = await get_active_session(chat_id)
    session_id = session.get("id", 0)
    
    # Fetch full history from DB
    history = await cache.get_chat_history(chat_id)
    if len(history) <= COMPACTION_KEEP_COUNT:
        return f"chat history has {len(history)} messages, which is below the compaction threshold ({COMPACTION_KEEP_COUNT}). no compaction needed."

    # Split into older messages (to summarize) and recent messages (to keep)
    older_messages = history[:-COMPACTION_KEEP_COUNT]
    
    # Format older messages for LLM summarization
    formatted_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in older_messages])
    prompt = f"Summarize the key facts, user preferences, and context from this chat history in 3-5 concise bullet points:\n\n{formatted_text}"
    
    try:
        import agent
        summary = await agent.generate_direct_completion(prompt)
        if summary:
            await db.save_session_summary(session_id, summary.strip())
            logger.info(f"Compacted {len(older_messages)} messages for session {session_id}.")
            return f"context compacted successfully!\n\ncheckpoint summary:\n{summary.strip()}"
        return "failed to generate context summary."
    except Exception as e:
        logger.error(f"Error compacting session history for chat {chat_id}: {e}")
        return f"error during compaction: {e}"
