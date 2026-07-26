import os
import asyncio
import logging

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(__file__)

# PERSONA.md is the bot's identity/personality, loaded fresh into every prompt.
# It ships in git and is the default. Admins can rewrite it at runtime via the
# privileged edit_persona_file tool, which keeps a .bak and mirrors the result
# to Postgres so the edit survives a redeploy. Non-admins can never touch it.
PERSONA_FILE_PATH = os.path.join(_BASE_DIR, "PERSONA.md")

# Previous version of the persona, written on every edit so a bad rewrite is
# recoverable rather than permanent.
PERSONA_BACKUP_PATH = os.path.join(_BASE_DIR, "PERSONA.md.bak")

# DB key for runtime persona edits. Only populated once an admin actually edits
# the persona; the file shipped in git is the default until then.
PERSONA_DB_KEY = "persona_md_v1"

# MEMORY.md holds mutable "learned facts" only. It is DB-synced so it survives
# restarts, and the model may append to it via save_memory_fact.
MEMORY_FILE_PATH = os.path.join(_BASE_DIR, "MEMORY.md")

# DB key for the learned-facts store. Bumped from the old "memory_md" (which held
# the old combined persona+facts blob) so the stale personality is not resurrected
# from the database on startup.
MEMORY_DB_KEY = "learned_facts_v1"


def read_persona() -> str:
    """Returns the fixed persona text. Falls back to a minimal identity if missing."""
    if os.path.exists(PERSONA_FILE_PATH):
        try:
            with open(PERSONA_FILE_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading PERSONA.md: {e}")
    logger.warning("PERSONA.md missing — falling back to minimal identity.")
    return (
        "you're brodar, an ai chat companion made by doniyor. talk casually, in "
        "lowercase, short like texting a friend. never say you're a language model."
    )

def write_persona_md(content: str) -> bool:
    """
    Overwrite PERSONA.md, keeping a backup and syncing to Postgres.

    An admin can rewrite the bot's whole personality in one sentence, and the
    model does the actual writing — so a single sloppy edit could permanently
    destroy the persona with no way back. PERSONA.md.bak always holds the
    version immediately before the last edit (see restore_persona_backup), and
    the DB copy means a Render redeploy doesn't silently revert the change back
    to whatever is committed in git.
    """
    if not content or not content.strip():
        logger.error("Refusing to write an empty PERSONA.md.")
        return False

    try:
        previous = read_persona() if os.path.exists(PERSONA_FILE_PATH) else ""
        if previous:
            try:
                with open(PERSONA_BACKUP_PATH, "w", encoding="utf-8") as f:
                    f.write(previous)
            except Exception as e:
                logger.warning(f"Could not write PERSONA.md.bak: {e}")

        with open(PERSONA_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        import db
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(db.save_stored_memory(content, PERSONA_DB_KEY))
        except RuntimeError:
            # No running loop (e.g. called from a sync test); the file write is
            # still the source of truth for this process.
            pass
        return True
    except Exception as e:
        logger.error(f"Error writing to PERSONA.md: {e}")
        return False


def restore_persona_backup() -> bool:
    """Roll PERSONA.md back to the version before the last edit."""
    if not os.path.exists(PERSONA_BACKUP_PATH):
        logger.warning("No PERSONA.md.bak to restore from.")
        return False
    try:
        with open(PERSONA_BACKUP_PATH, "r", encoding="utf-8") as f:
            return write_persona_md(f.read())
    except Exception as e:
        logger.error(f"Error restoring PERSONA.md from backup: {e}")
        return False


async def sync_persona_from_db() -> None:
    """
    Restore an admin's persona edits from Postgres on startup.

    Without this, every Render deploy silently reverted the persona to whatever
    is committed in git — the admin's change appeared to work, then vanished
    hours later with no explanation. The DB copy only exists once someone has
    actually edited the persona at runtime; otherwise the shipped file wins.
    """
    import db
    try:
        stored = await db.fetch_stored_memory(PERSONA_DB_KEY)
        if stored and stored.strip():
            with open(PERSONA_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(stored)
            logger.info("Restored runtime persona edits (PERSONA.md) from Postgres.")
    except Exception as e:
        logger.error(f"Failed to sync persona from DB: {e}")


def read_memory_md() -> str:
    """Reads and returns the full content of the mutable learned-facts file."""
    if os.path.exists(MEMORY_FILE_PATH):
        try:
            with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading MEMORY.md: {e}")
            return ""
    return ""


async def sync_memory_from_db() -> None:
    """
    Syncs the mutable learned-facts store (MEMORY.md) from Postgres on startup.
    If Postgres has stored facts, they win (they may be newer than the file).
    If Postgres is empty, seed it from the local file.
    The persona (PERSONA.md) is intentionally NOT involved here.
    """
    import db
    try:
        stored_content = await db.fetch_stored_memory(MEMORY_DB_KEY)
        if stored_content:
            with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(stored_content)
            logger.info("Synced learned facts (MEMORY.md) from Postgres.")
        else:
            local_content = read_memory_md()
            if local_content:
                await db.save_stored_memory(local_content, MEMORY_DB_KEY)
                logger.info("Seeded learned facts to Postgres.")
    except Exception as e:
        logger.error(f"Failed to sync learned facts from DB: {e}")


def write_memory_md(content: str) -> bool:
    """Overwrites MEMORY.md (learned facts) and write-through to Postgres."""
    try:
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        import db
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(db.save_stored_memory(content, MEMORY_DB_KEY))
        except RuntimeError:
            pass
        return True
    except Exception as e:
        logger.error(f"Error writing MEMORY.md: {e}")
        return False


def append_user_fact(fact: str) -> bool:
    """Appends a new learned fact to MEMORY.md and syncs with Postgres."""
    fact = fact.strip()
    if not fact:
        return False

    try:
        content = read_memory_md()
        new_content = content.rstrip() + f"\n- {fact}\n"
        return write_memory_md(new_content)
    except Exception as e:
        logger.error(f"Error appending fact to MEMORY.md: {e}")
        return False
