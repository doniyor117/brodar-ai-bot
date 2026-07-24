import logging
import asyncio
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, BaseFilter
from aiogram.types import Message, TelegramObject, CallbackQuery
from aiogram.utils.chat_action import ChatActionSender

import config
import cache
import agent
import permissions

logger = logging.getLogger(__name__)

# Router for all bot handlers
router = Router()

class AccessControlMiddleware(BaseMiddleware):
    """
    Enforces access control policies:
    1. Private chats (DMs) must be from allowed user IDs.
    2. Group chats must be active (is_active = True), except for /activate and /deactivate commands.
    3. Commands like /activate, /deactivate, /allow_user, /disallow_user must only be run by allowed user IDs.
    """
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not isinstance(event, Message):
            return await handler(event, data)

        message: Message = event
        user_id = message.from_user.id if message.from_user else None
        chat_id = message.chat.id
        chat_type = message.chat.type

        # 1. Private Chat Check (DMs)
        if chat_type == "private":
            if not user_id or not await cache.is_user_allowed(user_id):
                logger.info(f"Access restricted for DM user ID: {user_id}")
                try:
                    await message.reply(f"access restricted. user ID {user_id} is not in the DM allowlist. ask an admin to run /allow_user {user_id}.")
                except Exception as reply_err:
                    logger.error(f"Failed to send access restricted reply: {reply_err}")
                return
            return await handler(event, data)

        # 2. Group Chat Check
        text = message.text or message.caption or ""
        cmd = text.strip().split()[0].lower() if text.strip() else ""

        # Admin & system control commands allowed in inactive groups
        allowed_group_cmds = ["/activate", "/deactivate", "/allow_user", "/disallow_user", "/start", "/help", "/status"]
        if any(cmd.startswith(c) for c in allowed_group_cmds):
            if not user_id or not await cache.is_user_allowed(user_id):
                logger.info(f"Ignoring admin command '{text}' from unauthorized user {user_id} in group {chat_id}")
                return
            return await handler(event, data)

        # For any other message/command in group chats, check if group is active
        is_active = await cache.get_chat_active(chat_id)
        if not is_active:
            # Silently ignore general chatter in inactive group chats until /activate
            return

        return await handler(event, data)


# Register the outer middleware on the router
router.message.outer_middleware(AccessControlMiddleware())

# Global cache for the bot's own username to prevent redundant API calls
BOT_USERNAME = None

async def init_bot_info(bot: Bot):
    """Caches the bot's username on startup."""
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logger.info(f"Bot info loaded. Username: @{BOT_USERNAME}")
    except Exception as e:
        logger.error(f"Failed to fetch bot info: {e}")

class ShouldRespondFilter(BaseFilter):
    """
    Determines if the bot should process and respond to a message:
    1. Private Chat (DMs) -> Checks if user is in allowed user IDs.
    2. Group / Supergroup:
       - Checks if group is active (is_active). If not, ignores.
       - Responds if Mention-Only is disabled.
       - Responds if Mention-Only is enabled AND the bot is mentioned or replied to.
    """
    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.chat.type == "private":
            user_id = message.from_user.id if message.from_user else None
            return bool(user_id and await cache.is_user_allowed(user_id))

        chat_id = message.chat.id
        
        # Check if group is active first
        is_active = await cache.get_chat_active(chat_id)
        if not is_active:
            return False

        # Read setting from write-through cache
        mention_only = await cache.get_chat_setting(chat_id)

        if not mention_only:
            return True

        # Resolve bot username from cached value or fetch if missing
        global BOT_USERNAME
        if not BOT_USERNAME:
            await init_bot_info(bot)

        text = message.text or message.caption or ""
        
        # Check if username is mentioned in text
        if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
            return True

        # Check if the message is a reply to the bot itself
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == bot.id:
                return True

        return False

TELEGRAM_MAX_MESSAGE_LEN = 4096

# Spans we must NOT lowercase: fenced code, inline code, and URLs. Everything
# else in a conversational reply gets forced to lowercase to keep brodar in
# character even when the flash model slips.
import re as _re
_PROTECTED_SPAN_RE = _re.compile(r"(```.*?```|`[^`]*`|https?://\S+|www\.\S+)", _re.DOTALL)


def enforce_lowercase(text: str) -> str:
    """Lowercases conversational text while preserving URLs and code spans."""
    if not text:
        return text
    parts = _PROTECTED_SPAN_RE.split(text)
    # re.split with a capture group yields [plain, protected, plain, ...].
    return "".join(p if i % 2 else p.lower() for i, p in enumerate(parts))


async def send_long_reply(message: Message, text: str) -> None:
    """
    Replies to a message, splitting anything over Telegram's 4096-char hard limit
    into multiple chunks. A single over-long reply used to raise TelegramBadRequest
    and the user just saw the generic error message instead.
    """
    if not text:
        text = "..."
    # First chunk is a reply; the rest are follow-up sends to keep ordering.
    chunks = [text[i:i + TELEGRAM_MAX_MESSAGE_LEN] for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LEN)]
    first = True
    for chunk in chunks:
        if first:
            await message.reply(chunk)
            first = False
        else:
            await message.answer(chunk)


async def is_user_privileged(message: Message, bot: Bot) -> bool:
    """
    True if the sender may run state-changing commands / tools:
    DM-allowlisted users anywhere, or group administrators in their group.
    """
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return False
    if await cache.is_user_allowed(user_id):
        return True
    if message.chat.type != "private":
        return await is_sender_admin(message, bot)
    return False


async def is_sender_admin(message: Message, bot: Bot, user_id: int = None) -> bool:
    """Checks if the user who sent the command is an administrator in the chat."""
    if message.chat.type == "private":
        return True
    check_user_id = user_id or (message.from_user.id if message.from_user else None)
    if not check_user_id:
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, check_user_id)
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.warning(f"Error checking admin status for user {check_user_id} in chat {message.chat.id}: {e}")
        return False

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Start command handler. Casual, sarcastic and lowercase."""
    text = (
        "oh, hello. i'm brodar - an ai chat companion created by doniyor.\n"
        "i can reply to your text, search the web, use skills, or run safe shell tools.\n"
        "in groups, i must be /activate-d first by an authorized user.\n"
        "type /help to see what i can do."
    )
    await message.reply(text)

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Help command handler. Casual description of commands."""
    text = (
        "here is what you can do with me:\n"
        "- talk normally: type anything to chat.\n"
        "- /status: view bot status, system uptime, and settings.\n"
        "- /clear: reset chat context and history.\n"
        "- /skills: list available skill instructions.\n"
        "- /memory: view stored persistent facts from memory.md.\n"
        "- /activate (authorized users): enable bot in group.\n"
        "- /deactivate (authorized users): disable bot in group.\n"
        "- /allow_user <id> (authorized users): grant DM access to a user.\n"
        "- /disallow_user <id> (authorized users): revoke DM access.\n"
        "- /toggle_reply (group admins): toggle mention-only vs reply-all mode."
    )
    await message.reply(text)

@router.message(Command("toggle_reply"))
async def cmd_toggle_reply(message: Message, bot: Bot):
    """Toggles mention-only vs free-reply mode in group chats."""
    chat_id = message.chat.id
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    current = await cache.get_chat_setting(chat_id)
    cache.set_chat_setting(chat_id, not current)
    mode = "mention-only" if not current else "free-reply"
    await message.reply(f"reply mode switched to: {mode}.")

@router.message(Command("activate"))
async def cmd_activate(message: Message):
    """Activates the bot in the group. Casual and lowercase."""
    chat_id = message.chat.id
    if message.chat.type == "private":
        await message.reply("this is a private chat. i'm already active here.")
        return

    cache.set_chat_active(chat_id, True)
    await message.reply("system activated. i will now listen and respond to messages in this group.")

@router.message(Command("deactivate"))
async def cmd_deactivate(message: Message):
    """Deactivates the bot in the group. Casual and lowercase."""
    chat_id = message.chat.id
    if message.chat.type == "private":
        await message.reply("you can't deactivate me in private chats, buddy. just delete the chat or block me.")
        return

    cache.set_chat_active(chat_id, False)
    await message.reply("system deactivated. going dark in this group. bye.")

@router.message(Command("allow_user"))
async def cmd_allow_user(message: Message):
    """Dynamically adds a user ID to the DM allowlist."""
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /allow_user <telegram_user_id>")
        return

    target_id = int(parts[1])
    cache.add_allowed_user(target_id)
    await message.reply(f"user {target_id} added to the allowed users list.")

@router.message(Command("disallow_user"))
async def cmd_disallow_user(message: Message):
    """Dynamically removes a user ID from the DM allowlist."""
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /disallow_user <telegram_user_id>")
        return

    target_id = int(parts[1])
    cache.remove_allowed_user(target_id)
    await message.reply(f"user {target_id} removed from the allowed users list.")

@router.message(Command("status"))
async def cmd_status(message: Message):
    """Displays bot and system status."""
    chat_id = message.chat.id
    active = await cache.get_chat_active(chat_id) if message.chat.type != "private" else True
    mention_only = await cache.get_chat_setting(chat_id) if message.chat.type != "private" else False
    
    import tools
    # Run blocking subprocess calls off the event loop.
    uptime = (await asyncio.to_thread(tools.execute_shell_command, "uptime", "")).strip()
    ram = (await asyncio.to_thread(tools.execute_shell_command, "free", "-h")).strip()
    
    text = (
        f"brodar status report:\n"
        f"- chat type: {message.chat.type}\n"
        f"- bot active: {active}\n"
        f"- mention only: {mention_only}\n"
        f"- model: {config.MODEL_NAME}\n"
        f"- uptime: {uptime}\n"
        f"- ram: {ram}"
    )
    await message.reply(text)

@router.message(Command("clear"))
async def cmd_clear(message: Message, bot: Bot):
    """Clears conversation history for the chat."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can clear the chat context.")
        return
    chat_id = message.chat.id
    await cache.clear_chat_history(chat_id)
    await message.reply("cleared conversation context for this chat. fresh start.")

@router.message(Command("skills"))
async def cmd_skills(message: Message):
    """Lists available skill instructions."""
    import skills
    avail = skills.list_available_skills()
    if not avail:
        await message.reply("no skills found in skills/ directory.")
        return
        
    lines = ["available skill instructions:"]
    for s in avail:
        lines.append(f"- {s['name']}: {s['description']}")
    lines.append("\nuse tool calls or ask me to perform any of these skills.")
    await message.reply("\n".join(lines))

@router.message(Command("memory"))
async def cmd_memory(message: Message, bot: Bot):
    """Displays the stored MEMORY.md file contents. Admin-only (contains admin IDs)."""
    if not await is_user_privileged(message, bot):
        await message.reply("that's private. only authorized admins can view memory.")
        return
    import memory
    mem_content = memory.read_memory_md()
    if not mem_content.strip():
        await message.reply("memory.md is empty.")
        return
    await message.reply(f"stored memory.md:\n\n{mem_content[:3500]}")

@router.message(Command("stop"))
@router.message(Command("cancel"))
async def cmd_stop(message: Message):
    """Emergency Abort / Kill Switch handler."""
    chat_id = message.chat.id
    cancelled = await agent.cancel_running_task(chat_id)
    if cancelled:
        await message.reply("operation halted. agent execution cancelled.")
    else:
        await message.reply("no active agent tasks running in this chat.")

@router.message(Command("new_session"))
@router.message(Command("new"))
async def cmd_new_session(message: Message, bot: Bot):
    """Starts a new named conversation session."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can start a new session.")
        return
    chat_id = message.chat.id
    parts = (message.text or "").strip().split(maxsplit=1)
    title = parts[1] if len(parts) > 1 else "new session"
    import session_manager
    session = await session_manager.new_session(chat_id, title)
    await message.reply(f"started new session '{session.get('title')}' (id: {session.get('id')}). fresh memory.")

@router.message(Command("sessions"))
async def cmd_sessions(message: Message):
    """Lists all sessions for the chat."""
    chat_id = message.chat.id
    import session_manager
    sessions = await session_manager.list_sessions(chat_id)
    if not sessions:
        await message.reply("no active sessions found.")
        return
    lines = ["chat sessions:"]
    for s in sessions:
        status = "[active]" if s.get("is_active") else "[inactive]"
        lines.append(f"- id: {s['id']} | title: {s['title']} {status}")
    lines.append("\nuse /switch_session <id> to change active session.")
    await message.reply("\n".join(lines))

@router.message(Command("switch_session"))
async def cmd_switch_session(message: Message, bot: Bot):
    """Switches the active session for the chat."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can switch sessions.")
        return
    chat_id = message.chat.id
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /switch_session <session_id>")
        return
    target_id = int(parts[1])
    import session_manager
    success = await session_manager.switch_session(chat_id, target_id)
    if success:
        await message.reply(f"switched active session to id {target_id}.")
    else:
        await message.reply(f"session id {target_id} not found for this chat.")

@router.message(Command("compress"))
@router.message(Command("compact"))
async def cmd_compress(message: Message, bot: Bot):
    """Manually triggers LLM context compaction."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can compact context.")
        return
    chat_id = message.chat.id
    await message.reply("compacting conversation history into context checkpoint...")
    import session_manager
    result = await session_manager.compact_session_history(chat_id)
    await message.reply(result)

@router.message(Command("install_skill"))
async def cmd_install_skill(message: Message, bot: Bot):
    """Installs an external SKILL.md from a URL. Admin-only (writes into the prompt)."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can install skills.")
        return
    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 2:
        await message.reply("usage: /install_skill <url> [optional_name]")
        return
    url = parts[1]
    name = parts[2] if len(parts) > 2 else None
    import skills
    # Blocking network download — keep it off the event loop.
    result = await asyncio.to_thread(skills.install_skill_from_url, url, name)
    await message.reply(result)

@router.message(Command("enable_skill"))
async def cmd_enable_skill(message: Message, bot: Bot):
    """Enables a skill."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can toggle skills.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.reply("usage: /enable_skill <skill_name>")
        return
    skill_name = parts[1]
    import skills
    skills.set_skill_enabled(skill_name, True)
    await message.reply(f"skill '{skill_name}' enabled.")

@router.message(Command("disable_skill"))
async def cmd_disable_skill(message: Message, bot: Bot):
    """Disables a skill."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can toggle skills.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.reply("usage: /disable_skill <skill_name>")
        return
    skill_name = parts[1]
    import skills
    skills.set_skill_enabled(skill_name, False)
    await message.reply(f"skill '{skill_name}' disabled.")

@router.message(Command("uninstall_skill"))
async def cmd_uninstall_skill(message: Message, bot: Bot):
    """Uninstalls a skill."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can uninstall skills.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.reply("usage: /uninstall_skill <skill_name>")
        return
    skill_name = parts[1]
    import skills
    result = skills.uninstall_skill(skill_name)
    await message.reply(result)

@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot):
    """Group admin ban command."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /ban <user_id> [duration_seconds]")
        return
    user_id = int(parts[1])
    duration = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    import group_tools
    res = await group_tools.ban_member(bot, message.chat.id, user_id, duration)
    await message.reply(res)

@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot):
    """Group admin unban command."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /unban <user_id>")
        return
    user_id = int(parts[1])
    import group_tools
    res = await group_tools.unban_member(bot, message.chat.id, user_id)
    await message.reply(res)

@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot):
    """Group admin mute command."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /mute <user_id> [duration_seconds]")
        return
    user_id = int(parts[1])
    duration = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    import group_tools
    res = await group_tools.mute_member(bot, message.chat.id, user_id, duration)
    await message.reply(res)

@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot):
    """Group admin unmute command."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /unmute <user_id>")
        return
    user_id = int(parts[1])
    import group_tools
    res = await group_tools.unmute_member(bot, message.chat.id, user_id)
    await message.reply(res)

@router.message(Command("stop_all"))
@router.message(Command("cancel_all"))
async def cmd_stop_all(message: Message):
    """Global Emergency Abort handler across all chats."""
    user_id = message.from_user.id if message.from_user else 0
    if not await cache.is_user_allowed(user_id):
        await message.reply("nice try, but only authorized admins can run global stop.")
        return
    count = await agent.cancel_all_tasks()
    await message.reply(f"global emergency abort: halted {count} active agent task(s) across all chats.")

@router.message(Command("promote"))
async def cmd_promote(message: Message, bot: Bot):
    """Group admin promote member to admin."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /promote <user_id> [custom_title]")
        return
    target_uid = int(parts[1])
    title = parts[2] if len(parts) > 2 else "Admin"
    import group_tools
    res = await group_tools.promote_to_admin(bot, message.chat.id, target_uid, title)
    await message.reply(res)

@router.message(Command("demote"))
async def cmd_demote(message: Message, bot: Bot):
    """Group admin demote member from admin."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("usage: /demote <user_id>")
        return
    target_uid = int(parts[1])
    import group_tools
    res = await group_tools.demote_from_admin(bot, message.chat.id, target_uid)
    await message.reply(res)

# permissions and CallbackQuery imported at top of file

@router.callback_query(permissions.PermCallback.filter())
async def handle_permission_callback(callback: CallbackQuery, callback_data: permissions.PermCallback, bot: Bot):
    """Handles inline keyboard responses for permission approval prompts."""
    # callback.message can be None for very old messages.
    if not callback.message:
        await callback.answer("This request is no longer available.", show_alert=True)
        return

    chat = callback.message.chat
    user = callback.from_user

    # Access control: callback queries bypass the message middleware, so enforce
    # here. Group -> must be a group admin. DM -> must be DM-allowlisted.
    if chat.type != "private":
        if not await is_sender_admin(callback.message, bot, user_id=user.id):
            await callback.answer("Only group admins can approve or reject permission requests!", show_alert=True)
            return
    else:
        if not await cache.is_user_allowed(user.id):
            await callback.answer("You are not authorized.", show_alert=True)
            return

    req_id = callback_data.req_id
    action = callback_data.action
    future = permissions._pending_requests.get(req_id)

    if not future or future.done():
        await callback.answer("This request has already expired or been processed.", show_alert=True)
        return

    if action == "approve":
        future.set_result(True)
        await callback.answer("Permission Approved!")
        try:
            await callback.message.edit_text(f"✅ <b>Permission Approved</b> by @{user.username or user.id}.", parse_mode="HTML")
        except Exception:
            pass
    else:
        future.set_result(False)
        await callback.answer("Permission Rejected!")
        try:
            await callback.message.edit_text(f"❌ <b>Permission Rejected</b> by @{user.username or user.id}.", parse_mode="HTML")
        except Exception:
            pass

@router.message(Command("set_title"))
async def cmd_set_title(message: Message, bot: Bot):
    """Group admin set title command."""
    if message.chat.type == "private":
        await message.reply("this command only works in groups.")
        return
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not a group admin.")
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("usage: /set_title <new_title>")
        return
    new_title = parts[1]
    import group_tools
    res = await group_tools.set_group_title(bot, message.chat.id, new_title)
    await message.reply(res)

@router.message(ShouldRespondFilter())
async def handle_chat_message(message: Message, bot: Bot):
    """
    General message handler that handles conversational response generation.
    Appends messages to the history cache and calls the LLM agent.
    Registers task in agent._running_tasks to support emergency /stop cancellation.
    """
    chat_id = message.chat.id
    raw_text = message.text or message.caption or ""

    global BOT_USERNAME
    if not BOT_USERNAME:
        await init_bot_info(bot)

    cleaned_text = raw_text
    if BOT_USERNAME:
        cleaned_text = raw_text.replace(f"@{BOT_USERNAME}", "").strip()

    if not cleaned_text:
        return

    # Whether this sender may drive state-changing tools (persona/skill edits,
    # group moderation). Checked once here and passed into the agent.
    privileged = await is_user_privileged(message, bot)

    history = await cache.get_chat_history(chat_id)
    user_msg_entry = {"role": "user", "content": cleaned_text}
    temp_history = history + [user_msg_entry]

    current_task = asyncio.current_task()
    if current_task:
        agent.register_running_task(chat_id, current_task)

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            logger.info(f"Generating agent response for chat {chat_id}...")
            bot_reply = await agent.generate_response(
                temp_history,
                bot_instance=bot,
                chat_id=chat_id,
                requester_is_privileged=privileged,
            )

        if config.FORCE_LOWERCASE:
            bot_reply = enforce_lowercase(bot_reply)

        # Persist BEFORE sending. If sending fails (e.g. formatting), the turn is
        # still saved to history instead of being silently lost.
        cache.save_messages_async(chat_id, [
            {"role": "user", "content": cleaned_text},
            {"role": "assistant", "content": bot_reply},
        ])

        await send_long_reply(message, bot_reply)

    except asyncio.CancelledError:
        logger.info(f"Task execution for chat {chat_id} was cancelled by emergency stop.")
        raise
    except Exception as e:
        logger.error(f"Error handling user message in chat {chat_id}: {e}", exc_info=True)
        try:
            await message.reply("my circuits are fried or something. try again later.")
        except Exception as send_err:
            logger.error(f"Failed to send error message: {send_err}")
    finally:
        agent.unregister_running_task(chat_id, current_task)
