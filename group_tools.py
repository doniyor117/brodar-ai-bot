import logging
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Bot, types

logger = logging.getLogger(__name__)

async def ban_member(bot: Bot, chat_id: int, user_id: int, duration_seconds: int = 0) -> str:
    """Bans a user from the group."""
    try:
        until_date = datetime.now() + timedelta(seconds=duration_seconds) if duration_seconds > 0 else None
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date, revoke_messages=True)
        time_str = f"for {duration_seconds} seconds" if duration_seconds > 0 else "permanently"
        return f"User {user_id} has been banned {time_str}."
    except Exception as e:
        logger.error(f"Error banning user {user_id} in chat {chat_id}: {e}")
        return f"Failed to ban user {user_id}: {e}"

async def unban_member(bot: Bot, chat_id: int, user_id: int) -> str:
    """Unbans a user from the group."""
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        return f"User {user_id} has been unbanned."
    except Exception as e:
        logger.error(f"Error unbanning user {user_id} in chat {chat_id}: {e}")
        return f"Failed to unban user {user_id}: {e}"

async def mute_member(bot: Bot, chat_id: int, user_id: int, duration_seconds: int = 0) -> str:
    """Mutes a user (restricts sending messages) in the group."""
    try:
        until_date = datetime.now() + timedelta(seconds=duration_seconds) if duration_seconds > 0 else None
        permissions = types.ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=permissions, until_date=until_date)
        time_str = f"for {duration_seconds} seconds" if duration_seconds > 0 else "indefinitely"
        return f"User {user_id} has been muted {time_str}."
    except Exception as e:
        logger.error(f"Error muting user {user_id} in chat {chat_id}: {e}")
        return f"Failed to mute user {user_id}: {e}"

async def unmute_member(bot: Bot, chat_id: int, user_id: int) -> str:
    """Unmutes a user in the group."""
    try:
        permissions = types.ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=permissions)
        return f"User {user_id} has been unmuted."
    except Exception as e:
        logger.error(f"Error unmuting user {user_id} in chat {chat_id}: {e}")
        return f"Failed to unmute user {user_id}: {e}"

async def set_group_title(bot: Bot, chat_id: int, title: str) -> str:
    """Updates the group chat title."""
    try:
        await bot.set_chat_title(chat_id=chat_id, title=title)
        return f"Group title updated to '{title}'."
    except Exception as e:
        logger.error(f"Error setting chat title for {chat_id}: {e}")
        return f"Failed to set title: {e}"

async def set_group_description(bot: Bot, chat_id: int, description: str) -> str:
    """Updates the group chat description."""
    try:
        await bot.set_chat_description(chat_id=chat_id, description=description)
        return "Group description updated successfully."
    except Exception as e:
        logger.error(f"Error setting chat description for {chat_id}: {e}")
        return f"Failed to set description: {e}"

async def pin_message(bot: Bot, chat_id: int, message_id: int) -> str:
    """Pins a message in the group."""
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id)
        return f"Message {message_id} pinned."
    except Exception as e:
        logger.error(f"Error pinning message {message_id} in chat {chat_id}: {e}")
        return f"Failed to pin message: {e}"

async def unpin_message(bot: Bot, chat_id: int, message_id: Optional[int] = None) -> str:
    """Unpins a message or all messages in the group."""
    try:
        if message_id:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
            return f"Message {message_id} unpinned."
        else:
            await bot.unpin_all_chat_messages(chat_id=chat_id)
            return "All pinned messages unpinned."
    except Exception as e:
        logger.error(f"Error unpinning message in chat {chat_id}: {e}")
        return f"Failed to unpin message: {e}"

async def promote_to_admin(bot: Bot, chat_id: int, user_id: int, title: str = "Admin") -> str:
    """Promotes a group member to Administrator."""
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_change_info=True
        )
        try:
            await bot.set_chat_administrator_custom_title(chat_id=chat_id, user_id=user_id, custom_title=title)
        except Exception:
            pass
        return f"User {user_id} promoted to group admin ({title})."
    except Exception as e:
        logger.error(f"Error promoting user {user_id} in chat {chat_id}: {e}")
        return f"Failed to promote user {user_id}: {e}"

async def demote_from_admin(bot: Bot, chat_id: int, user_id: int) -> str:
    """Demotes a group administrator back to regular member."""
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            is_anonymous=False,
            can_manage_chat=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_change_info=False
        )
        return f"User {user_id} demoted from group admin."
    except Exception as e:
        logger.error(f"Error demoting user {user_id} in chat {chat_id}: {e}")
        return f"Failed to demote user {user_id}: {e}"

