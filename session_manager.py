import logging
from typing import Dict, List, Any, Optional
import config
import db
import cache

logger = logging.getLogger(__name__)

# Keep the last 12 messages intact during a MANUAL /compact.
COMPACTION_KEEP_COUNT = 12

# Chats currently being compacted, to prevent overlapping runs from rapid messages.
_compacting: set = set()

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

def _flatten_content(content) -> str:
    """Turn a message's content (string or multimodal blocks) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict) and b.get("type") == "image_url":
                parts.append("[image]")
        return " ".join(parts)
    return str(content)


async def _do_compaction(chat_id: int, keep_count: int) -> str:
    """
    Shared compaction routine: fold everything except the newest `keep_count`
    messages into the session's running summary, persist that checkpoint, delete
    the folded messages from the DB, and drop the in-memory history so the next
    read reloads the trimmed set. Returns the new summary text (or "" on failure).
    """
    session = await get_active_session(chat_id)
    session_id = session.get("id", 0)

    history = await cache.get_chat_history(chat_id)
    if len(history) <= keep_count:
        return ""

    older_messages = history[:-keep_count]
    existing_summary = await get_session_summary(session_id) or ""

    older_text = "\n".join(f"{m['role']}: {_flatten_content(m.get('content'))}" for m in older_messages)
    prompt = (
        "You maintain a rolling summary of an ongoing chat so context isn't lost when "
        "old messages are dropped. Merge the EXISTING SUMMARY with the OLDER MESSAGES "
        "into a single updated summary. Keep it tight but preserve key facts, decisions, "
        "user preferences, names, and anything referenced later. Use short bullet points.\n\n"
        f"EXISTING SUMMARY:\n{existing_summary or '(none yet)'}\n\n"
        f"OLDER MESSAGES:\n{older_text}"
    )

    import agent
    summary = (await agent.generate_direct_completion(prompt)).strip()
    if not summary:
        logger.warning(f"Compaction for chat {chat_id} produced an empty summary; skipping.")
        return ""

    await db.save_session_summary(session_id, summary)
    try:
        deleted = await db.delete_old_messages(chat_id, keep_count)
        cache.invalidate_history(chat_id)
        logger.info(f"Compacted chat {chat_id}: summarized+deleted {deleted} messages, kept {keep_count}.")
    except Exception as e:
        logger.error(f"Compaction saved a summary but failed to trim messages for chat {chat_id}: {e}")
    return summary


async def compact_session_history(chat_id: int) -> str:
    """Manual /compact: summarize everything but the last COMPACTION_KEEP_COUNT messages."""
    history = await cache.get_chat_history(chat_id)
    if len(history) <= COMPACTION_KEEP_COUNT:
        return (
            f"chat history has {len(history)} messages, below the compaction threshold "
            f"({COMPACTION_KEEP_COUNT}). no compaction needed."
        )
    summary = await _do_compaction(chat_id, COMPACTION_KEEP_COUNT)
    if summary:
        return f"context compacted successfully!\n\ncheckpoint summary:\n{summary}"
    return "compaction failed. try again later."


async def maybe_auto_compact(chat_id: int) -> bool:
    """
    Auto-compaction engine. If the working context exceeds COMPACT_TOKEN_THRESHOLD
    tokens, summarize the older portion and keep the most recent messages (within
    COMPACT_KEEP_TOKENS). Returns True if a compaction ran. Safe to call after
    every turn; it's a cheap token count unless the threshold is crossed.
    """
    if chat_id in _compacting:
        return False  # a compaction is already in flight for this chat
    # Reserve the slot atomically (no await between the check and the add).
    _compacting.add(chat_id)
    try:
        history = await cache.get_chat_history(chat_id)
        if len(history) <= config.COMPACT_MIN_KEEP_MESSAGES:
            return False

        import agent
        model = None  # let the token counter use its default; good enough for the gate
        total = agent.count_tokens(history, model)
        if total <= config.COMPACT_TOKEN_THRESHOLD:
            return False

        # Walk from the newest message backwards, keeping messages until we'd
        # exceed the keep-budget — those are retained verbatim; the rest are folded.
        keep_count = 0
        acc = 0
        for msg in reversed(history):
            t = agent.count_tokens([msg], model)
            if acc + t > config.COMPACT_KEEP_TOKENS and keep_count >= config.COMPACT_MIN_KEEP_MESSAGES:
                break
            acc += t
            keep_count += 1

        keep_count = max(config.COMPACT_MIN_KEEP_MESSAGES, min(keep_count, len(history) - 1))
        logger.info(
            f"Auto-compaction triggered for chat {chat_id}: ~{total} tokens > "
            f"{config.COMPACT_TOKEN_THRESHOLD}; keeping last {keep_count} messages."
        )
        summary = await _do_compaction(chat_id, keep_count)
        return bool(summary)
    except Exception as e:
        logger.error(f"Auto-compaction check failed for chat {chat_id}: {e}", exc_info=True)
        return False
    finally:
        _compacting.discard(chat_id)
