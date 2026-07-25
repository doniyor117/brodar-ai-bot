import io
import base64
import logging
import asyncio
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    Message, TelegramObject, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.utils.chat_action import ChatActionSender

import config
import cache
import agent
import models
import permissions

logger = logging.getLogger(__name__)

# Router for all bot handlers
router = Router()


class ModelCallback(CallbackData, prefix="model"):
    """Inline-button payload for the /model picker."""
    key: str


def _build_model_keyboard(current_key: str) -> InlineKeyboardMarkup:
    """Inline keyboard listing every registered model, ticking the active one."""
    rows = []
    for spec in models.all_models():
        tick = "✅ " if spec.key == current_key else ""
        lock = "" if spec.is_available else " 🔒"
        rows.append([InlineKeyboardButton(
            text=f"{tick}{spec.label}{lock}",
            callback_data=ModelCallback(key=spec.key).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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

# Strong refs to background maintenance tasks (auto-compaction) so the GC can't
# cancel them mid-run.
_maintenance_tasks: set = set()


def _spawn_maintenance(coro) -> None:
    """Fire-and-forget a background maintenance task, keeping a strong reference."""
    task = asyncio.create_task(coro)
    _maintenance_tasks.add(task)
    task.add_done_callback(_maintenance_tasks.discard)

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

# ── [SILENT] sentinel token ─────────────────────────────────────────────────
# When the model outputs this token, it has decided not to speak. The Telegram
# sending engine catches it and sends nothing. The user's message is still saved
# to history so the bot retains context.
SILENT_TOKEN = "[SILENT]"

# ── Bot-to-bot loop detection ──────────────────────────────────────────────
# Server-side safety net: if recent history looks like two bots talking to each
# other in AI-formal tone, skip the LLM call entirely to save tokens.
BOT_LOOP_THRESHOLD = 4  # consecutive bot-looking user messages before auto-silence

def _looks_like_bot_loop(history: list, threshold: int = BOT_LOOP_THRESHOLD) -> bool:
    """Detect if recent history looks like a bot-to-bot infinite conversation."""
    if len(history) < threshold * 2:
        return False

    recent = history[-(threshold * 2):]  # last N pairs

    bot_indicators = 0
    for msg in recent:
        if msg["role"] == "user":
            text = msg.get("content", "")
            # Heuristics for AI-generated text in a casual chat context:
            # - Starts with capital letter (brodar and humans in groups rarely do)
            # - Contains assistant-like filler phrases
            # - Very long for a chat message (>200 chars)
            # - Contains bullet points or numbered lists
            ai_markers = [
                bool(text) and text[0].isupper(),
                any(p in text.lower() for p in [
                    "certainly", "i'd be happy to", "as an ai",
                    "here's", "here is", "let me help",
                    "is there anything else", "i can help",
                ]),
                len(text) > 200,
                bool(_re.search(r"^\s*[\-\*\d]+[\.\)]\s", text, _re.MULTILINE)),
            ]
            if sum(ai_markers) >= 2:
                bot_indicators += 1

    return bot_indicators >= threshold - 1

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
        "- /toggle_reply (group admins): toggle mention-only vs reply-all mode.\n"
        "- /toggle_tools (admins): show or hide the tool-activity clues.\n"
        "- /model (admins): switch the ai model (some can see images)."
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

@router.message(Command("toggle_tools"))
async def cmd_toggle_tools(message: Message, bot: Bot):
    """Toggles whether the bot shows '🔍 searching...' tool-activity notes in this chat."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can change this.")
        return
    chat_id = message.chat.id
    current = await cache.get_chat_tool_notes(chat_id)
    cache.set_chat_tool_notes(chat_id, not current)
    if current:
        await message.reply("tool clues hidden. i'll work quietly from now on.")
    else:
        await message.reply("tool clues on. i'll show you what i'm doing (🔍 ⚙️ 🧠).")


@router.message(Command("model"))
async def cmd_model(message: Message, bot: Bot):
    """Shows an inline picker to switch the global active model."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can change the model.")
        return
    current = await cache.get_active_model()
    spec = models.get_spec(current)
    label = spec.label if spec else current
    await message.reply(
        f"current model: {label}\n\npick one below. 🔒 = api key not set yet.",
        reply_markup=_build_model_keyboard(current),
    )


@router.callback_query(ModelCallback.filter())
async def handle_model_callback(callback: CallbackQuery, callback_data: ModelCallback, bot: Bot):
    """Applies a model selection from the /model inline keyboard."""
    if not callback.message:
        await callback.answer("this picker expired.", show_alert=True)
        return
    # Authorize the actual clicker (callback.from_user), not the message author
    # (which is the bot). DM-allowlisted users anywhere, or group admins.
    clicker = callback.from_user
    allowed = await cache.is_user_allowed(clicker.id)
    if not allowed and callback.message.chat.type != "private":
        allowed = await is_sender_admin(callback.message, bot, user_id=clicker.id)
    if not allowed:
        await callback.answer("only authorized admins can change the model.", show_alert=True)
        return

    spec = models.get_spec(callback_data.key)
    if not spec:
        await callback.answer("unknown model.", show_alert=True)
        return

    cache.set_active_model(spec.key)
    await callback.answer(f"switched to {spec.label}")
    note = "" if spec.is_available else f"\n\n⚠️ heads up: {spec.api_key_env} isn't set, so this will fail until you add it."
    vision = " it can see images now." if spec.supports_vision else " (text only — it can't see images.)"
    try:
        # Drop the keyboard (reply_markup omitted) so the buttons disappear, and
        # leave a body that states the selected model.
        await callback.message.edit_text(f"✅ model set to: {spec.label}.{vision}{note}")
    except Exception:
        pass


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
    show_tool_notes = await cache.get_chat_tool_notes(chat_id)
    active_model_key = await cache.get_active_model()
    active_spec = models.get_spec(active_model_key)
    model_label = active_spec.label if active_spec else active_model_key

    import tools
    # Run blocking subprocess calls off the event loop.
    uptime = (await asyncio.to_thread(tools.execute_shell_command, "uptime", "")).strip()
    ram = (await asyncio.to_thread(tools.execute_shell_command, "free", "-h")).strip()
    
    text = (
        f"brodar status report:\n"
        f"- chat type: {message.chat.type}\n"
        f"- bot active: {active}\n"
        f"- mention only: {mention_only}\n"
        f"- tool clues: {show_tool_notes}\n"
        f"- model: {model_label}\n"
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

def _speaker_name(message: Message) -> str:
    """A short display name for a group speaker."""
    u = message.from_user
    if not u:
        return "someone"
    
    name = u.full_name or (f"@{u.username}" if u.username else str(u.id))
    if u.is_bot:
        name = f"[BOT] {name}"
    return name


def _attribute(message: Message, text: str) -> str:
    """
    In group chats, prefix a message with who said it ("alex: hey") so the model
    can follow a multi-person conversation. DMs are left as-is (1:1, no ambiguity).
    """
    if message.chat.type in ("group", "supergroup"):
        return f"{_speaker_name(message)}: {text}"
    return text


MAX_IMAGE_BYTES = 4 * 1024 * 1024   # skip still images larger than 4MB
MAX_ANIM_BYTES = 12 * 1024 * 1024   # skip animations/gifs larger than 12MB
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # skip audio/video larger than 20MB

def _message_has_media(message: Message) -> bool:
    """True if a message carries something a vision or audio model could process."""
    if message.photo or message.animation or message.sticker:
        return True
    if message.voice or message.audio or message.video_note or message.video:
        return True
    doc = message.document
    if doc and (doc.mime_type or "").startswith(("image/", "video/", "audio/")):
        return True
    return False


async def _download_file(bot: Bot, file_id: str, max_bytes: int) -> bytes:
    """Downloads a Telegram file to bytes, or returns b'' if too big / on error."""
    try:
        f = await bot.get_file(file_id)
        if f.file_size and f.file_size > max_bytes:
            logger.info(f"Skipping file {file_id}: {f.file_size} bytes over limit.")
            return b""
        buf = io.BytesIO()
        await bot.download_file(f.file_path, destination=buf)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"Failed to download file {file_id}: {e}")
        return b""


def _to_data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def _extract_multimodal_data_urls(message: Message, bot: Bot) -> tuple:
    """
    Collects images and audio from a message as base64 data URLs.
    Returns (urls, is_gif) where urls can contain both image and audio data URLs.
    """
    urls = []
    is_gif = False

    # 1. Still images (photo or image document)
    if message.photo:
        raw = await _download_file(bot, message.photo[-1].file_id, MAX_IMAGE_BYTES)
        if raw:
            urls.append(_to_data_url(raw))
    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        raw = await _download_file(bot, doc.file_id, MAX_IMAGE_BYTES)
        if raw:
            urls.append(_to_data_url(raw, doc.mime_type))

    # 2. Animation / GIF / Video / Video Note -> extract frames AND audio
    anim = message.animation or message.video or message.video_note or (doc if (doc and (doc.mime_type or "").startswith("video/")) else None)
    
    is_video_sticker = message.sticker and getattr(message.sticker, 'is_video', False)
    if is_video_sticker:
        anim = message.sticker

    if anim:
        is_gif = True
        raw = await _download_file(bot, anim.file_id, MAX_AUDIO_BYTES)
        frames = []
        audio_bytes = b""
        if raw:
            import media
            # Frame extraction
            mode = "video" if (getattr(anim, 'duration', 0) > 10 and not message.animation and not is_video_sticker) else "loop"
            frames = await asyncio.to_thread(media.extract_video_frames, raw, mode, 10)
            # Audio extraction
            audio_bytes = await asyncio.to_thread(media.extract_audio, raw)

        if frames:
            urls.extend(_to_data_url(fr) for fr in frames)
        elif getattr(anim, "thumbnail", None):
            # Fallback for .tgs (Lottie JSON) animated stickers or failed extraction
            thumb = await _download_file(bot, anim.thumbnail.file_id, MAX_IMAGE_BYTES)
            if thumb:
                urls.append(_to_data_url(thumb))
        
        if audio_bytes:
            urls.append(_to_data_url(audio_bytes, "audio/mp3"))

    # 3. Static Stickers (Regular .webp)
    if message.sticker and not is_video_sticker:
        st = message.sticker
        if not getattr(st, 'is_animated', False):
            raw = await _download_file(bot, st.file_id, MAX_IMAGE_BYTES)
            if raw:
                urls.append(_to_data_url(raw, "image/webp"))
        elif getattr(st, "thumbnail", None):
            thumb = await _download_file(bot, st.thumbnail.file_id, MAX_IMAGE_BYTES)
            if thumb:
                urls.append(_to_data_url(thumb))
                
    # 4. Pure Audio (Voice / Audio document)
    audio_obj = message.voice or message.audio or (doc if (doc and (doc.mime_type or "").startswith("audio/")) else None)
    if audio_obj:
        raw = await _download_file(bot, audio_obj.file_id, MAX_AUDIO_BYTES)
        if raw:
            import media
            audio_bytes = await asyncio.to_thread(media.extract_audio, raw)
            if audio_bytes:
                urls.append(_to_data_url(audio_bytes, "audio/mp3"))

    return urls, is_gif


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

    has_media = _message_has_media(message)

    # Nothing to do if there's neither text nor a visual.
    if not cleaned_text and not has_media:
        return

    # Whether this sender may drive state-changing tools (persona/skill edits,
    # group moderation). Checked once here and passed into the agent.
    privileged = await is_user_privileged(message, bot)
    show_tool_notes = await cache.get_chat_tool_notes(chat_id)

    # Vision handling. Only bother if the active model can actually see images.
    image_urls = []            # current turn's images (attached to this message)
    context_image_urls = []    # prior turns' images (chronological context block)
    is_gif = False
    had_prior_media = False
    vision_on = False
    if has_media or config.VISUAL_MEMORY_TURNS > 0:
        vision_on = models.resolve_spec(await cache.get_active_model()).supports_vision

    if vision_on:
        # Advance the per-chat turn counter (drives visual aging). Returns the last
        # turn that carried a visual, so we know whether to look for retained images.
        turn, last_visual_turn = await cache.bump_chat_turn(chat_id)

        current_urls = []
        if has_media:
            current_urls, is_gif = await _extract_multimodal_data_urls(message, bot)
            if current_urls:
                await cache.remember_visuals(
                    chat_id, turn,
                    [{"data_url": u, "is_gif": is_gif} for u in current_urls],
                )
                last_visual_turn = turn

        if config.VISUAL_MEMORY_TURNS > 0:
            min_turn = turn - config.VISUAL_MEMORY_TURNS + 1
            # Only touch the visuals table if something is actually in the window.
            if last_visual_turn >= min_turn:
                retained = await cache.recall_visuals(chat_id, min_turn, config.VISUAL_MEMORY_MAX_IMAGES)
                # Keep chronological order; split "this turn" from earlier turns.
                image_urls = [r["data_url"] for r in retained if r["turn"] >= turn]
                context_image_urls = [r["data_url"] for r in retained if r["turn"] < turn]
                is_gif = is_gif or any(r.get("is_gif") for r in retained)
                had_prior_media = bool(context_image_urls)
            else:
                image_urls = current_urls
        else:
            image_urls = current_urls

    # What we store/show as the user's text (visuals aren't persisted in history).
    # In groups it's prefixed with the speaker's name for multi-person context.
    if cleaned_text:
        text_for_model = cleaned_text
    elif message.sticker:
        text_for_model = "[sent a sticker]"
    elif message.voice or message.audio:
        text_for_model = "[sent an audio message]"
    elif message.video_note or message.video:
        text_for_model = "[sent a video]"
    elif is_gif:
        text_for_model = "[sent a gif]"
    elif has_media:
        text_for_model = "[sent media]"
    else:
        text_for_model = ""

    notes = []
    if has_media and not vision_on:
        notes.append(
            "the user sent media (image/video/audio) but the current model can't process it — "
            "tell them to switch with /model to a multimodal model."
        )
    if is_gif and image_urls:
        notes.append("some attached images are still frames extracted from a video/gif.")
    if had_prior_media:
        notes.append(
            "some attached media is from the last few messages, kept so you can answer "
            "follow-ups about them; the newest belong to the current message."
        )
    if notes:
        text_for_model += "\n\n(note: " + " ".join(notes) + ")"
    attributed_text = _attribute(message, text_for_model)

    history = await cache.get_chat_history(chat_id)
    user_msg_entry = {"role": "user", "content": attributed_text}
    temp_history = history + [user_msg_entry]

    is_group = message.chat.type in ("group", "supergroup")

    # Server-side bot-loop safety net. If recent history looks like a
    # bot-to-bot infinite conversation, skip the LLM call entirely.
    if is_group and _looks_like_bot_loop(history):
        logger.info(f"Bot-loop detected in chat {chat_id}, auto-silencing.")
        cache.save_messages_async(chat_id, [
            {"role": "user", "content": attributed_text},
        ])
        return

    current_task = asyncio.current_task()
    if current_task:
        agent.register_running_task(chat_id, current_task)

    import time
    start_time = time.time()

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            logger.info(f"Generating agent response for chat {chat_id}...")
            bot_reply = await agent.generate_response(
                temp_history,
                bot_instance=bot,
                chat_id=chat_id,
                requester_is_privileged=privileged,
                show_tool_notes=show_tool_notes,
                image_urls=image_urls or None,
                context_image_urls=context_image_urls or None,
            )

        # ── Reactions parsing ──────────────────────────────────────
        reaction_match = _re.search(r'\|\[(.*?)\]\|', bot_reply)
        emoji_reaction = None
        if reaction_match:
            emoji_reaction = reaction_match.group(1).strip()
            bot_reply = bot_reply.replace(reaction_match.group(0), "").strip()

        if emoji_reaction:
            try:
                from aiogram.types import ReactionTypeEmoji
                await message.react(reaction=[ReactionTypeEmoji(type="emoji", emoji=emoji_reaction)])
                logger.info(f"Bot reacted with {emoji_reaction} in chat {chat_id}")
            except Exception as e:
                logger.error(f"Failed to react to message in chat {chat_id}: {e}")

        # ── [SILENT] interception ──────────────────────────────────────
        if is_group and bot_reply.strip().startswith(SILENT_TOKEN):
            # Model chose to stay silent. Save only the user's message
            # to history (so the bot retains context) but send nothing.
            logger.info(f"[SILENT] Model chose silence in chat {chat_id}")
            cache.save_messages_async(chat_id, [
                {"role": "user", "content": attributed_text},
            ])
            return

        # Strip any accidental [SILENT] prefix in DMs (should never happen,
        # but if it does, just remove it and send the rest).
        if bot_reply.strip().startswith(SILENT_TOKEN):
            bot_reply = bot_reply.strip()[len(SILENT_TOKEN):].strip()
            if not bot_reply and not emoji_reaction:
                bot_reply = "hmm?"

        if config.FORCE_LOWERCASE:
            bot_reply = enforce_lowercase(bot_reply)

        # If the model chose silence and only wanted to react, we can just return here
        # (This is for DMs mostly since group silences are caught earlier, but just in case)
        if not bot_reply:
            logger.info(f"No text to send (only reaction) in chat {chat_id}")
            return

        # Natural typing delay (Fast human: ~25 chars/sec)
        generation_time = time.time() - start_time
        chars_per_sec = 25.0
        expected_typing_time = len(bot_reply) / chars_per_sec
        
        # Bound the delay: minimum 0.5s for realism, max 5s so we don't stall
        expected_typing_time = max(0.5, min(expected_typing_time, 5.0))
        
        remaining_delay = expected_typing_time - generation_time
        if remaining_delay > 0:
            # We exit the ChatActionSender context block earlier, but the action
            # stays active for a short while on the client. Let's explicitly trigger
            # it again if the remaining delay is noticeable.
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                await asyncio.sleep(remaining_delay)

        # Persist BEFORE sending. If sending fails (e.g. formatting), the turn is
        # still saved to history instead of being silently lost. Images aren't
        # stored (they'd bloat history); a placeholder marks that one was sent.
        cache.save_messages_async(chat_id, [
            {"role": "user", "content": attributed_text},
            {"role": "assistant", "content": bot_reply},
        ])

        await send_long_reply(message, bot_reply)

        # After replying, check if the context grew past the token threshold and
        # compact in the background (it makes its own LLM call — don't block).
        import session_manager
        _spawn_maintenance(session_manager.maybe_auto_compact(chat_id))

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


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_passive(message: Message, bot: Bot):
    """
    Passively records group messages the bot did NOT reply to (e.g. mention-only
    mode, non-mention chatter) into history, so when it IS mentioned it has the
    surrounding conversation as context. Registered AFTER handle_chat_message, so
    it only ever sees the fall-through messages. It never replies.
    """
    chat_id = message.chat.id
    # Only build context for groups the bot has been activated in.
    if not await cache.get_chat_active(chat_id):
        return

    text = (message.text or message.caption or "").strip()
    has_media = _message_has_media(message)
    if not text and not has_media:
        return

    # Capture images posted WITHOUT mentioning the bot, so when it's later
    # @-mentioned it can still see them (the mention-only blind spot). Only when a
    # vision model is active — otherwise there's nothing that could use them.
    if has_media:
        spec = models.resolve_spec(await cache.get_active_model())
        if spec.supports_vision:
            turn, _ = await cache.bump_chat_turn(chat_id)
            urls, is_gif = await _extract_multimodal_data_urls(message, bot)
            if urls:
                await cache.remember_visuals(
                    chat_id, turn, [{"data_url": u, "is_gif": is_gif} for u in urls]
                )

    # Log the message (attributed) so the transcript reflects it — including a
    # placeholder for image-only posts.
    if text:
        logged = text
    elif message.sticker:
        logged = "[sent a sticker]"
    elif message.voice or message.audio:
        logged = "[sent an audio message]"
    elif message.video_note or message.video:
        logged = "[sent a video]"
    elif message.animation:
        logged = "[sent a gif]"
    else:
        logged = "[sent media]"
        
    await cache.get_chat_history(chat_id)  # prime cache so we append, not overwrite
    cache.save_messages_async(chat_id, [{"role": "user", "content": _attribute(message, logged)}])

    # Keep group context from growing unbounded.
    import session_manager
    _spawn_maintenance(session_manager.maybe_auto_compact(chat_id))
