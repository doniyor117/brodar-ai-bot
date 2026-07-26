import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from aiogram import Bot, types

logger = logging.getLogger(__name__)

# Telegram treats an until_date less than 30s or more than 366 days away as
# "forever". A 10-second mute therefore silences someone PERMANENTLY — which,
# combined with the naive-local-time bug below, is what "when i said mute it went
# rogue" actually was.
TELEGRAM_MIN_RESTRICT_SECONDS = 31
TELEGRAM_MAX_RESTRICT_SECONDS = 366 * 24 * 3600


def _until(duration_seconds: int) -> Optional[datetime]:
    """
    Absolute UTC expiry for a timed restriction, or None for permanent.

    Must be timezone-aware. datetime.now() is naive local time, so on any host
    that isn't UTC the deadline was silently shifted by the offset — a 10-minute
    mute could land in the past (permanent) or hours in the future.
    """
    if duration_seconds <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)


def validate_duration(duration_seconds: int, action: str) -> Optional[str]:
    """
    Explain why this duration won't do what the caller intends, or None if fine.

    Returned to the model as the tool result so it corrects itself instead of
    cheerfully reporting a 10-second mute that was actually permanent.
    """
    if duration_seconds <= 0:
        return None  # deliberate permanent action
    if duration_seconds < TELEGRAM_MIN_RESTRICT_SECONDS:
        return (
            f"Telegram treats a {action} shorter than {TELEGRAM_MIN_RESTRICT_SECONDS}s "
            f"as PERMANENT, so {duration_seconds}s would not do what you meant. "
            f"Use at least {TELEGRAM_MIN_RESTRICT_SECONDS} seconds, or say explicitly "
            f"that you want a permanent {action}."
        )
    if duration_seconds > TELEGRAM_MAX_RESTRICT_SECONDS:
        return (
            f"Telegram treats a {action} longer than 366 days as PERMANENT. "
            f"Use a shorter duration or ask for a permanent {action} explicitly."
        )
    return None


def _fmt_duration(seconds: int) -> str:
    """Human phrasing for a duration, so results read like a person wrote them."""
    if seconds <= 0:
        return "permanently"
    if seconds < 60:
        return f"for {seconds} seconds"
    if seconds < 3600:
        return f"for {seconds // 60} minutes"
    if seconds < 86400:
        return f"for {seconds // 3600} hours"
    return f"for {seconds // 86400} days"


async def ban_member(bot: Bot, chat_id: int, user_id: int, duration_seconds: int = 0) -> str:
    """Bans a user from the group."""
    problem = validate_duration(duration_seconds, "ban")
    if problem:
        return f"Did not ban anyone. {problem}"
    try:
        await bot.ban_chat_member(
            chat_id=chat_id, user_id=user_id,
            until_date=_until(duration_seconds), revoke_messages=True,
        )
        return f"User {user_id} has been banned {_fmt_duration(duration_seconds)}."
    except Exception as e:
        logger.error(f"Error banning user {user_id} in chat {chat_id}: {e}")
        return f"Failed to ban user {user_id}: {e}"

async def unban_member(bot: Bot, chat_id: int, user_id: int) -> str:
    """Unbans a user from the group."""
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        try:
            # Generate a one-time invite link for the user to rejoin
            invite = await bot.create_chat_invite_link(chat_id=chat_id, member_limit=1)
            return f"User {user_id} has been unbanned. Since bots cannot forcefully add users, here is their one-time invite link to rejoin: {invite.invite_link}"
        except Exception as link_e:
            return f"User {user_id} has been unbanned. Failed to generate invite link: {link_e}"
    except Exception as e:
        logger.error(f"Error unbanning user {user_id} in chat {chat_id}: {e}")
        return f"Failed to unban user {user_id}: {e}"

async def mute_member(bot: Bot, chat_id: int, user_id: int, duration_seconds: int = 0) -> str:
    """Mutes a user (restricts sending messages) in the group."""
    problem = validate_duration(duration_seconds, "mute")
    if problem:
        return f"Did not mute anyone. {problem}"
    try:
        permissions = types.ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id, permissions=permissions,
            until_date=_until(duration_seconds),
        )
        return f"User {user_id} has been muted {_fmt_duration(duration_seconds)}."
    except Exception as e:
        logger.error(f"Error muting user {user_id} in chat {chat_id}: {e}")
        return f"Failed to mute user {user_id}: {e}"

async def unmute_member(bot: Bot, chat_id: int, user_id: int) -> str:
    """Unmutes a user in the group."""
    try:
        permissions = types.ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
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
        # The custom title is a separate call and frequently fails (the bot can
        # only title admins it promoted, and titles are capped at 16 chars).
        # Swallowing that while still claiming "promoted to admin (Boss)" told
        # the model a title had been set when it hadn't.
        try:
            await bot.set_chat_administrator_custom_title(
                chat_id=chat_id, user_id=user_id, custom_title=title,
            )
            return f"User {user_id} promoted to group admin with the title '{title}'."
        except Exception as title_e:
            logger.warning(f"Promoted {user_id} but could not set custom title: {title_e}")
            return (
                f"User {user_id} was promoted to group admin, but the custom title "
                f"'{title}' could not be set: {title_e}"
            )
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

