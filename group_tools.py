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

async def list_admins(bot: Bot, chat_id: int) -> dict:
    """
    Live admin roster + total member count, straight from the Bot API.

    This is the one member question the API can answer perfectly. The Bot API
    has no method to enumerate every member of a chat at all — only
    getChatAdministrators, getChatMember (needs an id you already have), and
    getChatMemberCount — so this is also the most reliable way to (re)seed the
    chat_members table with confirmed admin status.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.error(f"Error fetching admins for chat {chat_id}: {e}")
        return {"ok": False, "error": str(e), "admins": [], "member_count": None}

    member_count = None
    try:
        member_count = await bot.get_chat_member_count(chat_id)
    except Exception as e:
        logger.warning(f"Error fetching member count for chat {chat_id}: {e}")

    rows = []
    for m in admins:
        user = m.user
        rows.append({
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "is_bot": user.is_bot,
            "status": m.status,  # "creator" or "administrator"
            "custom_title": getattr(m, "custom_title", None),
        })
    return {"ok": True, "admins": rows, "member_count": member_count}


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


async def delete_message(bot: Bot, chat_id: int, message_id: int) -> str:
    """Deletes a single message."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return f"Message {message_id} deleted."
    except Exception as e:
        logger.error(f"Error deleting message {message_id} in chat {chat_id}: {e}")
        return f"Failed to delete message {message_id}: {e}"


async def delete_messages(bot: Bot, chat_id: int, message_ids: list) -> str:
    """
    Deletes up to 100 messages in one call (Bot API's own cap). Falls back to
    one-by-one deletion for a bot/aiogram version without the bulk method, so
    this degrades gracefully instead of hard-failing.
    """
    ids = [i for i in (message_ids or []) if isinstance(i, int)][:100]
    if not ids:
        return "No valid message ids given — nothing deleted."
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=ids)
        return f"Deleted {len(ids)} message(s)."
    except AttributeError:
        ok, failed = 0, []
        for mid in ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
                ok += 1
            except Exception as e:
                failed.append(mid)
        result = f"Deleted {ok}/{len(ids)} message(s) (one at a time — bulk delete unavailable)."
        if failed:
            result += f" Failed: {failed}."
        return result
    except Exception as e:
        logger.error(f"Error bulk-deleting {len(ids)} messages in chat {chat_id}: {e}")
        return f"Failed to delete messages: {e}"


async def set_chat_permissions(bot: Bot, chat_id: int, permissions: Optional[dict] = None) -> str:
    """
    Sets the group's default permissions for non-admin members — locking or
    unlocking the whole chat at once. `permissions` maps ChatPermissions field
    names (can_send_messages, can_send_photos, can_invite_users, ...) to
    booleans; anything omitted defaults to False (locked), so "lock the group"
    is just calling this with an empty dict.
    """
    try:
        perms = types.ChatPermissions(**(permissions or {}))
        await bot.set_chat_permissions(chat_id=chat_id, permissions=perms)
        allowed = [k for k, v in (permissions or {}).items() if v] or ["(none — fully locked)"]
        return f"Chat permissions updated. Allowed for members: {', '.join(allowed)}."
    except Exception as e:
        logger.error(f"Error setting chat permissions for chat {chat_id}: {e}")
        return f"Failed to set chat permissions: {e}"


async def set_custom_title(bot: Bot, chat_id: int, user_id: int, title: str) -> str:
    """
    Sets an existing admin's custom title on its own, without re-running the
    whole promotion. Only works for admins the bot itself promoted, and titles
    are capped at 16 characters by Telegram.
    """
    try:
        await bot.set_chat_administrator_custom_title(
            chat_id=chat_id, user_id=user_id, custom_title=title,
        )
        return f"Custom title for user {user_id} set to '{title}'."
    except Exception as e:
        logger.error(f"Error setting custom title for user {user_id} in chat {chat_id}: {e}")
        return f"Failed to set custom title: {e}"


async def create_invite_link(
    bot: Bot, chat_id: int, member_limit: Optional[int] = None,
    expire_seconds: Optional[int] = None, name: Optional[str] = None,
) -> str:
    """Creates a new (non-primary) invite link, optionally capped or time-limited."""
    try:
        expire_date = _until(expire_seconds) if expire_seconds else None
        link = await bot.create_chat_invite_link(
            chat_id=chat_id, name=name, member_limit=member_limit, expire_date=expire_date,
        )
        return f"Invite link created: {link.invite_link}"
    except Exception as e:
        logger.error(f"Error creating invite link for chat {chat_id}: {e}")
        return f"Failed to create invite link: {e}"


async def revoke_invite_link(bot: Bot, chat_id: int, invite_link: str) -> str:
    """Revokes a previously created invite link."""
    if not invite_link:
        return "Need the invite_link to revoke — none was given."
    try:
        await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite_link)
        return f"Invite link revoked: {invite_link}"
    except Exception as e:
        logger.error(f"Error revoking invite link for chat {chat_id}: {e}")
        return f"Failed to revoke invite link: {e}"


async def export_invite_link(bot: Bot, chat_id: int) -> str:
    """Returns (regenerating if needed) the group's primary invite link."""
    try:
        link = await bot.export_chat_invite_link(chat_id=chat_id)
        return f"Primary invite link: {link}"
    except Exception as e:
        logger.error(f"Error exporting invite link for chat {chat_id}: {e}")
        return f"Failed to export invite link: {e}"


async def approve_join_request(bot: Bot, chat_id: int, user_id: int) -> str:
    """Approves a pending join request."""
    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        return f"Join request from user {user_id} approved."
    except Exception as e:
        logger.error(f"Error approving join request for user {user_id} in chat {chat_id}: {e}")
        return f"Failed to approve join request: {e}"


async def decline_join_request(bot: Bot, chat_id: int, user_id: int) -> str:
    """Declines a pending join request."""
    try:
        await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        return f"Join request from user {user_id} declined."
    except Exception as e:
        logger.error(f"Error declining join request for user {user_id} in chat {chat_id}: {e}")
        return f"Failed to decline join request: {e}"


async def ban_channel(bot: Bot, chat_id: int, sender_chat_id: int) -> str:
    """Bans a channel's sender-chat identity (banChatSenderChat) — for channel-identity spam."""
    try:
        await bot.ban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id)
        return f"Channel {sender_chat_id} banned from posting as itself in this chat."
    except Exception as e:
        logger.error(f"Error banning channel {sender_chat_id} in chat {chat_id}: {e}")
        return f"Failed to ban channel: {e}"


async def unban_channel(bot: Bot, chat_id: int, sender_chat_id: int) -> str:
    """Reverses ban_channel."""
    try:
        await bot.unban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id)
        return f"Channel {sender_chat_id} unbanned."
    except Exception as e:
        logger.error(f"Error unbanning channel {sender_chat_id} in chat {chat_id}: {e}")
        return f"Failed to unban channel: {e}"


async def set_chat_photo(bot: Bot, chat_id: int, file_path: str) -> str:
    """Sets the group's photo from a local file (already validated/resolved by the caller)."""
    try:
        photo = types.FSInputFile(file_path)
        await bot.set_chat_photo(chat_id=chat_id, photo=photo)
        return "Chat photo updated."
    except Exception as e:
        logger.error(f"Error setting chat photo for chat {chat_id}: {e}")
        return f"Failed to set chat photo: {e}"


async def delete_chat_photo(bot: Bot, chat_id: int) -> str:
    """Removes the group's current photo."""
    try:
        await bot.delete_chat_photo(chat_id=chat_id)
        return "Chat photo removed."
    except Exception as e:
        logger.error(f"Error deleting chat photo for chat {chat_id}: {e}")
        return f"Failed to delete chat photo: {e}"


async def get_chat_info(bot: Bot, chat_id: int) -> str:
    """
    One read-only summary: title, type, description, member count, and the
    live admin list — getChat + getChatMemberCount + getChatAdministrators in
    a single tool call instead of three.
    """
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        logger.error(f"Error fetching chat info for {chat_id}: {e}")
        return f"Failed to get chat info: {e}"

    roster = await list_admins(bot, chat_id)
    lines = [
        f"title: {getattr(chat, 'title', None) or '(no title)'}",
        f"type: {getattr(chat, 'type', '?')}",
        f"id: {chat_id}",
    ]
    if getattr(chat, "description", None):
        lines.append(f"description: {chat.description}")
    if roster.get("member_count") is not None:
        lines.append(f"member count: {roster['member_count']}")
    if roster.get("ok") and roster["admins"]:
        admin_names = ", ".join(
            f"{a['full_name'] or a['user_id']}" + (f" (@{a['username']})" if a["username"] else "")
            for a in roster["admins"]
        )
        lines.append(f"admins: {admin_names}")
    return "\n".join(lines)


async def create_topic(bot: Bot, chat_id: int, name: str) -> str:
    """Creates a new forum topic. Only works in chats with forum mode enabled."""
    if not name or not name.strip():
        return "Need a name for the new topic — none was given."
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=name.strip())
        return f"Topic '{name}' created (message_thread_id: {topic.message_thread_id})."
    except Exception as e:
        logger.error(f"Error creating forum topic in chat {chat_id}: {e}")
        return f"Failed to create topic: {e}"


async def close_topic(bot: Bot, chat_id: int, message_thread_id: int) -> str:
    """Closes (locks) a forum topic without deleting it."""
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=message_thread_id)
        return f"Topic {message_thread_id} closed."
    except Exception as e:
        logger.error(f"Error closing topic {message_thread_id} in chat {chat_id}: {e}")
        return f"Failed to close topic: {e}"


async def reopen_topic(bot: Bot, chat_id: int, message_thread_id: int) -> str:
    """Reopens a previously closed forum topic."""
    try:
        await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=message_thread_id)
        return f"Topic {message_thread_id} reopened."
    except Exception as e:
        logger.error(f"Error reopening topic {message_thread_id} in chat {chat_id}: {e}")
        return f"Failed to reopen topic: {e}"


async def delete_topic(bot: Bot, chat_id: int, message_thread_id: int) -> str:
    """Deletes a forum topic and every message in it. Not reversible."""
    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=message_thread_id)
        return f"Topic {message_thread_id} deleted."
    except Exception as e:
        logger.error(f"Error deleting topic {message_thread_id} in chat {chat_id}: {e}")
        return f"Failed to delete topic: {e}"

