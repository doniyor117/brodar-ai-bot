import os
import logging
from typing import List

logger = logging.getLogger(__name__)

MEMORY_FILE_PATH = os.path.join(os.path.dirname(__file__), "MEMORY.md")

def read_memory_md() -> str:
    """Reads and returns the full content of MEMORY.md."""
    if os.path.exists(MEMORY_FILE_PATH):
        try:
            with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading MEMORY.md: {e}")
            return ""
    return ""

import asyncio

async def sync_memory_from_db() -> None:
    """
    Syncs MEMORY.md from Neon Postgres on startup.
    If Postgres contains stored memory, updates local MEMORY.md.
    If Postgres is empty, seeds Postgres with initial MEMORY.md content.
    """
    import db
    try:
        stored_content = await db.fetch_stored_memory("memory_md")
        if stored_content:
            with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(stored_content)
            logger.info("Successfully synced MEMORY.md from Neon Postgres.")
        else:
            local_content = read_memory_md()
            if local_content:
                await db.save_stored_memory(local_content, "memory_md")
                logger.info("Seeded initial MEMORY.md to Neon Postgres.")
    except Exception as e:
        logger.error(f"Failed to sync MEMORY.md from DB: {e}")

def write_memory_md(content: str) -> bool:
    """Overwrites MEMORY.md and triggers background write-through to Neon Postgres."""
    try:
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        
        # Async background update to Neon Postgres
        import db
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(db.save_stored_memory(content, "memory_md"))
        except RuntimeError:
            pass
        return True
    except Exception as e:
        logger.error(f"Error writing MEMORY.md: {e}")
        return False

def append_user_fact(fact: str) -> bool:
    """Appends a new user fact to MEMORY.md and syncs with Neon Postgres."""
    fact = fact.strip()
    if not fact:
        return False
        
    try:
        content = read_memory_md()
        new_content = content.strip() + f"\n- {fact}\n"
        return write_memory_md(new_content)
    except Exception as e:
        logger.error(f"Error appending fact to MEMORY.md: {e}")
        return False

