import io
import os
import base64
import logging
import asyncio
from typing import Optional
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    Message, TelegramObject, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, ChatJoinRequest,
)
from aiogram.utils.chat_action import ChatActionSender
import re as _re

import config
import cache
import agent
import models
import tools
import response_mode

logger = logging.getLogger(__name__)

# Router for all bot handlers
router = Router()


pending_approvals = {}

@router.callback_query(F.data.startswith("approve:") | F.data.startswith("deny:"))
async def handle_approval(callback: CallbackQuery):
    """
    Resolves a privileged-tool approval prompt.

    AccessControlMiddleware is registered on router.message only, so callback
    queries reach this handler with no access check whatsoever — this is the
    only gate. The prompt itself now only ever lands in config.approval_
    recipient_id()'s own DM (see agent._request_interactive_approval), so ONLY
    that specific account may resolve it — not "any admin", which used to let
    a group admin approve a card meant for doniyor specifically.
    """
    action, call_id = callback.data.split(":", 1)

    if callback.message is None:
        # Too old for Telegram to send back, so we can't tell which chat it was
        # in and therefore can't authorize the tapper. Fail closed.
        await callback.answer("this request is too old to confirm.", show_alert=True)
        return

    tapper_id = callback.from_user.id if callback.from_user else None
    recipient_id = config.approval_recipient_id()
    if not recipient_id or tapper_id != recipient_id:
        logger.warning(
            f"Rejected approval tap from unauthorized user {tapper_id} "
            f"(expected {recipient_id}) for call {call_id}"
        )
        await callback.answer("this approval isn't yours to give.", show_alert=True)
        return

    entry = pending_approvals.pop(call_id, None)
    if entry is None:
        await callback.answer("that request already expired.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    future = entry["future"] if isinstance(entry, dict) else entry
    if not future.done():
        future.set_result(action == "approve")

    from datetime import datetime, timezone
    status_text = "✅ approved" if action == "approve" else "❌ denied"
    who = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    when = datetime.now(timezone.utc).strftime("%H:%M UTC")
    try:
        # Edited in place rather than replaced: the DM becomes a readable audit
        # log of every approval ever asked for, not a pile of stale buttons.
        await callback.message.edit_text(
            f"{callback.message.text}\n\n{status_text} by {who} at {when}", reply_markup=None
        )
    except Exception as e:
        logger.warning(f"Failed to update approval prompt: {e}")
    await callback.answer(f"{action.capitalize()}d")

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

        # Record who this is BEFORE any access decision, so member lookup works
        # for everyone the bot has seen — including people who only ever send
        # commands, and DM users, both of which the old call site (buried inside
        # the group-only _speaker_name) never recorded at all.
        if message.from_user:
            u = message.from_user
            cache.track_user(
                chat_id, u.id,
                u.full_name or (f"@{u.username}" if u.username else str(u.id)),
                username=u.username,
                is_bot=bool(u.is_bot),
            )

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

        # Strip any @botusername suffix ("/help@BrodarAIBot") before matching.
        cmd = cmd.split("@", 1)[0]

        # Harmless informational commands. These are ADVERTISED to every group
        # member by set_bot_commands, so silently dropping them for non-admins
        # meant a member tapped /help and got nothing at all.
        public_group_cmds = {"/start", "/help", "/status"}
        if cmd in public_group_cmds:
            return await handler(event, data)

        # Admin & system control commands, allowed even in an inactive group.
        # Matched exactly: the old `cmd.startswith(c)` also swallowed anything
        # sharing a prefix, so /helpme was treated as /help.
        admin_group_cmds = {
            "/activate", "/deactivate", "/allow_user", "/disallow_user",
            "/set_main_account",
        }
        if cmd in admin_group_cmds:
            if not user_id or not await cache.is_user_allowed(user_id):
                logger.info(f"Ignoring admin command '{text}' from unauthorized user {user_id} in group {chat_id}")
                try:
                    await message.reply("that one's admin-only.")
                except Exception:
                    pass
                return
            return await handler(event, data)

        # For any other message/command in group chats, check if group is active
        is_active = await cache.get_chat_active(chat_id)
        if not is_active:
            # Silently ignore general chatter in inactive group chats until /activate
            return

        return await handler(event, data)


class AlbumMiddleware(BaseMiddleware):
    """
    Collapse a Telegram album into a single handler call.

    An album (several photos sent at once) is delivered as N independent
    updates that share a `media_group_id`. Handled one at a time, five photos
    became five turns, five debounce cycles, five history entries and five
    reply attempts — and one album consumed the entire visual-memory window.

    The first message of a group waits a short beat for its siblings, then runs
    the handler once with `album` in the handler data; the others are dropped.
    This is the standard aiogram-3 pattern (cf. aiogram-media-group).
    """
    def __init__(self, window: float = None):
        self.window = window if window is not None else config.MEDIA_ALBUM_WINDOW
        self._groups: dict = {}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        # media_group_id only exists on a Message, so anything else falls
        # straight through — no isinstance check needed.
        group_id = getattr(event, "media_group_id", None)
        if not group_id:
            return await handler(event, data)

        message = event

        bucket = self._groups.get(group_id)
        if bucket is not None:
            # A sibling is already collecting; hand it this message and stop.
            bucket.append(message)
            return

        bucket = [message]
        self._groups[group_id] = bucket
        try:
            await asyncio.sleep(self.window)
            # Caption can be on any member of the album; run the handler on the
            # one that carries it so the user's text isn't lost.
            album = list(bucket)
            lead = next((m for m in album if (m.text or m.caption)), album[0])
            data["album"] = album
            return await handler(lead, data)
        finally:
            self._groups.pop(group_id, None)


# Register the outer middlewares on the router. Album batching runs first so a
# whole album reaches the access check (and the handler) as one event.
router.message.outer_middleware(AlbumMiddleware())
router.message.outer_middleware(AccessControlMiddleware())

# Global cache for the bot's own username to prevent redundant API calls
BOT_USERNAME = None


@router.chat_member()
async def handle_chat_member_update(update: ChatMemberUpdated) -> None:
    """
    Tracks joins, leaves, and promotion/demotion — the only source of member
    data the bot doesn't have to wait for someone to talk to get.

    This handler's mere existence is what makes chat_member updates arrive at
    all. Telegram excludes them from allowed_updates by default, and
    dp.resolve_used_update_types() (what main.py's set_webhook call passes)
    only requests update types that have at least one registered handler —
    with none registered, joins/leaves/promotions were never delivered and the
    member table only ever learned about people who happened to talk.
    """
    chat = update.chat
    new = update.new_chat_member
    if chat.type not in ("group", "supergroup") or new is None or new.user is None:
        return

    user = new.user
    if new.status in ("left", "kicked"):
        cache.mark_member_left(chat.id, user.id)
        return

    is_admin = new.status in ("administrator", "creator")
    cache.track_user(
        chat.id, user.id, user.full_name or (f"@{user.username}" if user.username else str(user.id)),
        username=user.username, is_bot=bool(user.is_bot), is_admin=is_admin,
    )


@router.my_chat_member()
async def handle_my_chat_member_update(update: ChatMemberUpdated) -> None:
    """Logs the bot's own membership/admin-status changes in a chat."""
    old = update.old_chat_member
    new = update.new_chat_member
    logger.info(
        f"Bot membership in chat {update.chat.id} changed: "
        f"{getattr(old, 'status', '?')} -> {getattr(new, 'status', '?')}"
    )


@router.chat_join_request()
async def handle_chat_join_request(update: ChatJoinRequest) -> None:
    """
    Logs pending join requests.

    Registered now so the update type is requested via allowed_updates (see
    handle_chat_member_update's docstring); approve/decline moderation tools
    land in a later phase.
    """
    user = update.from_user
    logger.info(
        f"Join request in chat {update.chat.id} from user {user.id if user else '?'} "
        f"(@{user.username if user and user.username else 'no username'})."
    )

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
        await set_bot_commands(bot)
    except Exception as e:
        logger.error(f"Failed to fetch bot info: {e}")

async def set_bot_commands(bot: Bot):
    from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators

    # Private chats (DMs) commands
    private_commands = [
        BotCommand(command="help", description="Show what the bot can do"),
        BotCommand(command="status", description="View bot status and uptime"),
        BotCommand(command="clear", description="Reset chat context and history"),
        BotCommand(command="skills", description="List available skill instructions"),
        BotCommand(command="memory", description="View stored persistent facts"),
        BotCommand(command="stop", description="Halt active agent tasks for this chat"),
        BotCommand(command="new", description="Start a fresh session"),
        BotCommand(command="sessions", description="List active sessions"),
        BotCommand(command="switch_session", description="Switch between sessions"),
        BotCommand(command="compress", description="Compact current session history"),
        BotCommand(command="set_main_account", description="(Admin) Set this account as the main master account"),
        BotCommand(command="allow_user", description="(Admin) Grant DM access to a user"),
        BotCommand(command="disallow_user", description="(Admin) Revoke DM access"),
        BotCommand(command="toggle_tools", description="(Admin) Show or hide tool-activity clues"),
        BotCommand(command="model", description="(Admin) Switch the active AI model"),
        BotCommand(command="install_skill", description="(Admin) Install a new skill from URL"),
        BotCommand(command="enable_skill", description="(Admin) Enable an installed skill"),
        BotCommand(command="disable_skill", description="(Admin) Disable a skill"),
        BotCommand(command="uninstall_skill", description="(Admin) Delete a skill"),
        BotCommand(command="stop_all", description="(Admin) Global emergency kill switch"),
    ]

    # Group chats (regular users)
    group_commands = [
        BotCommand(command="help", description="Show what the bot can do"),
        BotCommand(command="status", description="View bot status and uptime"),
        BotCommand(command="clear", description="Reset chat context and history"),
        BotCommand(command="stop", description="Halt active agent tasks for this chat"),
        BotCommand(command="new", description="Start a fresh session"),
    ]

    # Group chats (Administrators)
    group_admin_commands = group_commands + [
        BotCommand(command="activate", description="(Admin) Enable bot in group"),
        BotCommand(command="deactivate", description="(Admin) Disable bot in group"),
        BotCommand(command="toggle_reply", description="(Group Admin) Toggle mention-only vs reply-all mode"),
        BotCommand(command="promote", description="(Group Admin) Promote user to admin"),
        BotCommand(command="demote", description="(Group Admin) Demote user"),
        BotCommand(command="ban", description="(Group Admin) Ban user"),
        BotCommand(command="unban", description="(Group Admin) Unban user"),
        BotCommand(command="mute", description="(Group Admin) Mute user"),
        BotCommand(command="unmute", description="(Group Admin) Unmute user"),
        BotCommand(command="set_title", description="(Group Admin) Set group title"),
    ]

    await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(group_admin_commands, scope=BotCommandScopeAllChatAdministrators())
    logger.info("Bot commands configured successfully with separate visibilities.")

# Word-boundary matcher for the bot's spoken-name aliases ("brodar", "simon bro",
# "samy", …). Built once; \b keeps "brodark" or "samya" from counting as a call.
_ALIAS_RE = _re.compile(
    r"\b(?:" + "|".join(_re.escape(a) for a in config.BOT_ALIASES) + r")\b",
    _re.IGNORECASE,
) if config.BOT_ALIASES else None


def message_addresses_bot(message: Message, bot_id: int, bot_username: str) -> bool:
    """
    True if this message is calling the bot.

    Four independent ways to be addressed, all of which must work:
      1. a reply to one of the bot's own messages
      2. an @username mention — matched via Telegram's *entities* as well as the
         raw text, because a mention typed with different casing, or inserted by
         a client as a rich text_mention (which carries no "@" in the text at
         all), never appears as a literal "@username" substring
      3. a text_mention entity pointing at the bot's user id
      4. any spoken alias from config.BOT_ALIASES

    Only (4) and an exactly-cased (2) used to work, which is why @-mentioning the
    bot in a mention-only group appeared to be ignored while typing "brodar" worked.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_id:
            return True

    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
            return True
        if ent.type == "mention" and bot_username:
            mentioned = text[ent.offset:ent.offset + ent.length]
            if mentioned.lstrip("@").lower() == bot_username.lower():
                return True

    if not text:
        return False

    if bot_username and f"@{bot_username.lower()}" in text.lower():
        return True

    return bool(_ALIAS_RE and _ALIAS_RE.search(text))


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

        return message_addresses_bot(message, bot.id, BOT_USERNAME)

TELEGRAM_MAX_MESSAGE_LEN = 4096

# ── [SILENT] sentinel token ─────────────────────────────────────────────────
# When the model outputs this token, it has decided not to speak. The Telegram
# sending engine catches it and sends nothing. The user's message is still saved
# to history so the bot retains context.
SILENT_TOKEN = "[SILENT]"

# The exact emoji Telegram accepts for message reactions, verbatim from the Bot
# API's ReactionTypeEmoji (73 of them). Anything else is rejected server-side,
# which is why reactions felt random: the model would pick a plausible emoji, the
# API would refuse it, and the failure was only visible in the logs. Note 😂 is
# NOT accepted — only 🤣 — and 😂 was exactly what the old examples taught.
TELEGRAM_REACTION_EMOJI = (
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯",
    "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌",
    "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭",
    "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓",
    "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻",
    "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡",
    "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉",
    "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂️", "🤷", "🤷‍♀️",
    "😡",
)

_VARIATION_SELECTOR = "️"


def _fold_emoji(s: str) -> str:
    """Strip U+FE0F so "❤️" and "❤" compare equal."""
    return s.replace(_VARIATION_SELECTOR, "")

# Folded form -> the canonical spelling Telegram expects back. A model will
# happily emit "❤️" where the API wants "❤" (and "❤‍🔥" where it wants the
# VS16-bearing "❤️‍🔥"): visually identical, different bytes, silently rejected.
# Matching is done folded; what we send is always the canonical string.
_REACTION_LOOKUP = {_fold_emoji(e): e for e in TELEGRAM_REACTION_EMOJI}

# All |[emoji]| markers, anywhere in the reply — not just the first.
_REACTION_RE = _re.compile(r"\|\[(.*?)\]\|", _re.DOTALL)


def normalize_reaction(raw: str):
    """
    Validate a model-proposed reaction against Telegram's fixed set.

    Returns the canonical emoji string to send, or None if Telegram wouldn't
    accept it — in which case we skip the reaction rather than let the API call
    fail.
    """
    if not raw:
        return None
    candidate = _fold_emoji(raw.strip())
    if not candidate:
        return None
    if candidate in _REACTION_LOOKUP:
        return _REACTION_LOOKUP[candidate]
    # The model sometimes appends a word, e.g. "|[🔥 nice]|". Accept the leading
    # emoji on its own if that alone is valid.
    for width in (3, 2, 1):
        if len(candidate) >= width and candidate[:width] in _REACTION_LOOKUP:
            return _REACTION_LOOKUP[candidate[:width]]
    logger.info(f"Discarding reaction {raw!r}: not in Telegram's accepted set.")
    return None


def parse_control_tokens(reply: str) -> tuple:
    """
    Strip brodar's control tokens out of a reply.

    Returns (clean_text, reaction_emoji_or_None, wants_silence).

    Both tokens used to be matched positionally — the reaction with a single
    `re.search` (first occurrence only) and [SILENT] with `.startswith`. A model
    that emitted either one anywhere else leaked it to the user as literal text,
    e.g. a reply ending in "...anyway [SILENT]". Every occurrence is removed
    here, wherever it appears.
    """
    if not reply:
        return "", None, False

    reaction = None
    for match in _REACTION_RE.finditer(reply):
        if reaction is None:
            reaction = normalize_reaction(match.group(1))
    reply = _REACTION_RE.sub("", reply)

    wants_silence = SILENT_TOKEN in reply
    if wants_silence:
        reply = reply.replace(SILENT_TOKEN, "")

    # Collapse the whitespace the removed tokens left behind.
    reply = _re.sub(r"[ \t]{2,}", " ", reply)
    reply = _re.sub(r"\n{3,}", "\n\n", reply).strip()

    return reply, reaction, wants_silence

# ── Bot-to-bot loop detection ──────────────────────────────────────────────
# Server-side safety net: if recent history looks like two bots talking to each
# other in AI-formal tone, skip the LLM call entirely to save tokens.
BOT_LOOP_THRESHOLD = 10  # consecutive bot-looking user messages before auto-silence

def _looks_like_bot_loop(history: list, threshold: int = BOT_LOOP_THRESHOLD) -> bool:
    """
    Detect a bot-to-bot ping-pong: `threshold` incoming messages in a row, all
    from other bots and none from a human.

    Deliberately keyed on the `[BOT] ` tag alone. The tag comes from Telegram's
    own is_bot flag via _speaker_name(), so it is exact. The prose heuristics
    this replaces were catastrophically wrong: one of their four markers was
    `text[0].isupper()`, but _attribute() prefixes every group message with a
    capitalized display name, so that marker was true for 100% of messages —
    any message over 200 chars or containing "here's" then hit the 2-marker
    threshold. Enough normal human chatter tripped it that groups went silent
    and stayed silent for the whole 250-message history window.

    Requiring *consecutive* bot messages also means one human reply immediately
    clears the condition, so the worst case is now a brief pause, not a mute.
    """
    if len(history) < threshold:
        return False

    streak = 0
    for msg in reversed(history):
        if msg.get("role") != "user":
            # One of our own replies sits between two incoming messages; it
            # doesn't break a bot streak, but it doesn't extend it either.
            continue
        if (msg.get("content") or "").startswith("[BOT] "):
            streak += 1
            if streak >= threshold:
                return True
        else:
            return False
    return False

# Spans we must NOT lowercase: fenced code, inline code, URLs, @usernames and
# Telegram chat/user IDs. Everything else in a conversational reply gets forced
# to lowercase to keep brodar in character even when the flash model slips.
#
# The identifiers matter because the bot is routinely asked to look up a user or
# group id and hand it back — lowercasing "@BobSmith" produced a handle that
# doesn't resolve, so the answer looked right and was useless.
import re as _re
_PROTECTED_SPAN_RE = _re.compile(
    r"(```.*?```"          # fenced code block
    r"|`[^`]*`"            # inline code
    r"|https?://\S+"       # url
    r"|www\.\S+"           # bare www url
    r"|@[A-Za-z0-9_]{4,}"  # @username
    r"|-?\d{6,})",         # chat id / user id
    _re.DOTALL,
)


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

    # There used to be a "rich message" fast path here guarded by
    # `hasattr(message.bot, 'send_rich_message')`. aiogram 3.x has no such
    # method, so the guard was always False — which was the only thing hiding a
    # NameError: `InputRichMessage` was referenced but never imported. Deleted
    # rather than fixed: chunking below is the real, working path.

    # First chunk is a reply; the rest are follow-up sends to keep ordering.
    chunks = [text[i:i + TELEGRAM_MAX_MESSAGE_LEN] for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LEN)]
    first = True
    for chunk in chunks:
        if first:
            await message.reply(chunk)
            first = False
        else:
            await message.answer(chunk)


async def send_extraction_result(message: Message, text: str) -> None:
    """
    Delivers an extraction-mode reply (transcript/translation/OCR/subtitles).

    Short results go through send_long_reply as normal. Past
    EXTRACTION_CHUNK_OVERFLOW chunks (~3 Telegram messages), pasting the whole
    thing into chat is worse than useless — it's a wall of text split across
    several messages with no way to skim it — so it's written to a file and
    delivered as a document instead, with a one-line note in chat.
    """
    if not text:
        await send_long_reply(message, text)
        return

    chunk_count = -(-len(text) // TELEGRAM_MAX_MESSAGE_LEN)  # ceil div
    if chunk_count <= config.EXTRACTION_CHUNK_OVERFLOW:
        await send_long_reply(message, text)
        return

    try:
        gen_dir = os.path.join(config.TOOL_WORKSPACE_DIR, "generated")
        os.makedirs(gen_dir, exist_ok=True)
        ts = int(time.time())
        file_path = os.path.join(gen_dir, f"transcript-{ts}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)

        from aiogram.types import FSInputFile
        first_line = text.split("\n", 1)[0][:120]
        await message.reply_document(
            FSInputFile(file_path),
            caption=f"{first_line}\n({chunk_count} messages worth — sending as a file instead)",
        )
    except Exception as e:
        logger.error(f"Failed to deliver extraction overflow as a file: {e}")
        await send_long_reply(message, text)


async def is_user_privileged(message: Message, bot: Bot, user=None) -> bool:
    """
    True if the sender may run state-changing commands / tools:
    DM-allowlisted users anywhere, or group administrators in their group.

    `user` overrides message.from_user. Callback queries need it: the message
    carrying the inline keyboard was sent by the bot, so its from_user is the
    bot, not the person who tapped the button.
    """
    actor = user or message.from_user
    user_id = actor.id if actor else None
    if not user_id:
        return False
    if await cache.is_user_allowed(user_id):
        return True
    if message.chat.type != "private":
        return await is_sender_admin(message, bot, user_id=user_id)
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
        "i can reply to your text, see images, watch videos, listen to audio, search the web, use skills, or run safe shell tools.\n"
        "in groups, i must be /activate-d first by an authorized user.\n"
        "type /help to see what i can do."
    )
    await message.reply(text)

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Help command handler. Casual description of commands."""
    text = (
        "here is what you can do with me:\n"
        "- talk normally: type text, send images/video/voice, or forward messages (i'll wait a few seconds if you want to type a follow-up command).\n"
        "- /status: view bot status, system uptime, and settings.\n"
        "- /clear: reset chat context and history.\n"
        "- /skills: list available skill instructions.\n"
        "- /memory: view stored persistent facts from memory.md.\n"
        "- /activate (authorized users): enable bot in group.\n"
        "- /deactivate (authorized users): disable bot in group.\n"
        "- /allow_user <id> (authorized users): grant DM access to a user.\n"
        "- /disallow_user <id> (authorized users): revoke DM access.\n"
        "- /set_main_account (authorized users): set this current account as the main master account.\n"
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

@router.message(Command("set_main_account"))
async def cmd_set_main_account(message: Message, bot: Bot):
    """Dynamically sets the sender's account as the main master account."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can run this.")
        return

    user_id = message.from_user.id
    config.MAIN_ACCOUNT_ID = user_id
    agent._edit_env_file("MAIN_ACCOUNT_ID", str(user_id))
    await message.reply(f"this account ({user_id}) is now set as the main master account. interactive approvals will be routed here.")

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

    recipient = config.approval_recipient_id()
    if config.MAIN_ACCOUNT_ID:
        approval_line = f"- approvals go to: {config.MAIN_ACCOUNT_ID} (MAIN_ACCOUNT_ID)"
    elif recipient:
        approval_line = f"- approvals go to: {recipient} (fallback — MAIN_ACCOUNT_ID unset, run /setmain)"
    else:
        approval_line = "- approvals go to: NOWHERE — MAIN_ACCOUNT_ID and ALLOWED_DM_USER_IDS both empty, every privileged action will fail closed"

    text = (
        f"brodar status report:\n"
        f"- chat type: {message.chat.type}\n"
        f"- bot active: {active}\n"
        f"- mention only: {mention_only}\n"
        f"- tool clues: {show_tool_notes}\n"
        f"- model: {model_label}\n"
        f"{approval_line}\n"
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
    # Retained media and the turn counters are part of the context too. Clearing
    # only the text meant a "fresh start" immediately re-attached images from
    # before the clear, and the bot kept answering about them.
    await cache.clear_visuals(chat_id)
    # Skill bodies loaded this session are inlined into the system prompt, and
    # the shell tool keeps a persistent cwd. Neither is conversation history, but
    # both are context — leaving them behind made "fresh start" a half-truth.
    agent.clear_loaded_skills(chat_id)
    tools.reset_session(chat_id)
    await message.reply("cleared conversation context for this chat. fresh start.")

@router.message(Command("skills"))
async def cmd_skills(message: Message, bot: Bot):
    """Lists available skill instructions. Admin-only: skills describe internals."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can list skills.")
        return
    import skills
    avail = skills.list_available_skills()
    if not avail:
        await message.reply("no skills found in skills/ directory.")
        return

    # The slug is the name every other command accepts. Listing the display name
    # here is what made `/disable_skill "Brodar Bot Architecture"` report "not found".
    lines = ["available skill instructions:"]
    for s in avail:
        lines.append(f"- {s['slug']}: {s['description']}")
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
async def cmd_stop(message: Message, bot: Bot):
    """Emergency Abort / Kill Switch handler. Gated: it cancels other people's turns."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can halt running tasks.")
        return
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
async def cmd_sessions(message: Message, bot: Bot):
    """Lists all sessions for the chat. Gated: session titles leak conversation topics."""
    if not await is_user_privileged(message, bot):
        await message.reply("only authorized admins can list sessions.")
        return
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
    import config
    u = message.from_user
    if not u:
        return "someone"

    name = u.full_name or (f"@{u.username}" if u.username else str(u.id))

    if u.is_bot:
        name = f"[BOT] {name}"
        
    if config.MAIN_ACCOUNT_ID and u.id == config.MAIN_ACCOUNT_ID:
        name = f"[Master Admin] {name}"
        
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


async def _download_file(bot: Bot, file_id: str, max_bytes: int) -> tuple:
    """
    Download a Telegram file to bytes.

    Returns (data, reason) where reason is a short user-facing explanation when
    data is empty. The old version returned b"" for both "too big" and "network
    error", so an oversized file produced total silence with no way for the user
    to know why.

    Note the size guard now fails CLOSED: Telegram omits file_size for some
    types, and treating "unknown" as "fine" meant the limit didn't apply to
    exactly the files most likely to be huge. We download, then check.
    """
    try:
        f = await bot.get_file(file_id)
        if f.file_size and f.file_size > max_bytes:
            logger.info(f"Skipping file {file_id}: {f.file_size} bytes over the {max_bytes} limit.")
            return b"", f"that file's too big ({f.file_size // (1024 * 1024)}MB)"

        buf = io.BytesIO()
        await bot.download_file(f.file_path, destination=buf)
        data = buf.getvalue()

        if len(data) > max_bytes:
            logger.info(f"Discarding file {file_id} after download: {len(data)} bytes over limit.")
            return b"", f"that file's too big ({len(data) // (1024 * 1024)}MB)"
        return data, ""
    except Exception as e:
        logger.warning(f"Failed to download file {file_id}: {e}")
        return b"", "couldn't download that one"


def _to_data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def _extract_from_video(
    bot: Bot, obj, result, *, loop_style: bool, label: str
) -> None:
    """
    Pull frames (and audio, if there is any) out of one video-ish object.

    `loop_style` marks short looping clips — GIFs, video stickers, video notes —
    which get a handful of frames rather than one every few seconds.

    Everything is probed first. That single probe replaces two guesses the old
    code made and got wrong: it used getattr(obj, "duration", 0), which is
    always 0 on a Document, so an hour-long mp4 sent as a file got exactly 3
    frames; and it ran a full audio extraction pass on every GIF and video
    sticker, which are always silent, burning a temp dir and a process to
    produce nothing every single time.
    """
    import media

    raw, reason = await _download_file(bot, obj.file_id, MAX_AUDIO_BYTES)
    if not raw:
        if reason:
            result.notes.append(reason)
        # Static thumbnail is better than nothing (also covers .tgs stickers).
        await _fallback_to_thumbnail(bot, obj, result)
        return

    path = await media.write_temp(raw, suffix=".bin")
    try:
        info = await media.probe(path)

        if info.has_video or not info.ok:
            if loop_style:
                count = config.MEDIA_LOOP_FRAMES
            else:
                count = media.frame_count_for(
                    info.duration, config.MEDIA_MAX_FRAMES, config.MEDIA_SECONDS_PER_FRAME
                )
            frames = await media.extract_frames(
                path, count,
                max_dim=config.MEDIA_FRAME_MAX_DIM,
                duration=info.duration,
            )
            for fr in frames:
                result.items.append(media.MediaItem(
                    data_url=_to_data_url(fr), kind="image", is_gif=True,
                ))
            if not frames:
                logger.info(f"No frames extracted from {label}; falling back to thumbnail.")
                await _fallback_to_thumbnail(bot, obj, result)

        # Only touch audio if the probe actually saw an audio stream.
        if info.has_audio:
            audio, audio_mime = await media.extract_audio(path, config.MEDIA_MAX_AUDIO_SECONDS)
            if audio:
                result.items.append(media.MediaItem(
                    data_url=_to_data_url(audio, audio_mime), kind="audio",
                ))
            if info.duration > config.MEDIA_MAX_AUDIO_SECONDS:
                result.notes.append(
                    f"that's {int(info.duration // 60)} min of audio, i heard "
                    f"the first {int(config.MEDIA_MAX_AUDIO_SECONDS // 60)} min of it"
                )
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


async def _fallback_to_thumbnail(bot: Bot, obj, result) -> None:
    """Use an object's static thumbnail when real extraction produced nothing."""
    import media

    thumb = getattr(obj, "thumbnail", None)
    if not thumb:
        return
    raw, _ = await _download_file(bot, thumb.file_id, MAX_IMAGE_BYTES)
    if raw:
        result.items.append(media.MediaItem(
            data_url=_to_data_url(raw), kind="image", is_gif=True,
        ))


async def extract_media(messages, bot: Bot):
    """
    Collect every piece of media from one turn's message(s) as MediaItems.

    Takes a LIST because a Telegram album arrives as N separate updates that all
    belong to one user action; the album middleware batches them so five photos
    become one turn with five images instead of five turns each burning a slot
    of the retention window.

    Each item is extracted independently inside its own try, so one corrupt file
    can no longer take down the whole batch (and with it the reply).
    """
    import media

    result = media.ExtractionResult()

    for message in messages:
        try:
            await _extract_one_message(message, bot, result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Media extraction failed for one message: {e}", exc_info=True)
            result.notes.append("one of those files broke on the way in")

    _apply_media_budget(result)
    return result


async def _extract_one_message(message: Message, bot: Bot, result) -> None:
    """Extract every piece of media attached to a single message."""
    import media

    doc = message.document
    doc_mime = (doc.mime_type or "") if doc else ""

    # 1. Still images (photo, or an image sent as a document).
    if message.photo:
        raw, reason = await _download_file(bot, message.photo[-1].file_id, MAX_IMAGE_BYTES)
        if raw:
            result.items.append(media.MediaItem(data_url=_to_data_url(raw), kind="image"))
        elif reason:
            result.notes.append(reason)

    if doc and doc_mime.startswith("image/"):
        raw, reason = await _download_file(bot, doc.file_id, MAX_IMAGE_BYTES)
        if raw:
            result.items.append(media.MediaItem(data_url=_to_data_url(raw, doc_mime), kind="image"))
        elif reason:
            result.notes.append(reason)

    # 2. Video-ish: animation/GIF, video, video note, video sticker, video doc.
    is_video_sticker = bool(message.sticker and getattr(message.sticker, "is_video", False))
    video_obj = (
        message.animation
        or message.video
        or message.video_note
        or (doc if doc and doc_mime.startswith("video/") else None)
    )
    if is_video_sticker:
        video_obj = message.sticker

    if video_obj is not None:
        # Animations, video notes and stickers are short loops; a real video or a
        # video document is sampled across its length.
        loop_style = bool(message.animation or message.video_note or is_video_sticker)
        await _extract_from_video(
            bot, video_obj, result,
            loop_style=loop_style,
            label="animation" if loop_style else "video",
        )

    # 3. Static stickers (.webp), and animated .tgs via their thumbnail.
    if message.sticker and not is_video_sticker:
        st = message.sticker
        if not getattr(st, "is_animated", False):
            raw, _ = await _download_file(bot, st.file_id, MAX_IMAGE_BYTES)
            if raw:
                result.items.append(media.MediaItem(
                    data_url=_to_data_url(raw, "image/webp"), kind="image",
                ))
        else:
            await _fallback_to_thumbnail(bot, st, result)

    # 4. Pure audio: voice note, music file, or an audio document.
    audio_obj = (
        message.voice
        or message.audio
        or (doc if doc and doc_mime.startswith("audio/") else None)
    )
    if audio_obj is not None:
        raw, reason = await _download_file(bot, audio_obj.file_id, MAX_AUDIO_BYTES)
        if not raw:
            if reason:
                result.notes.append(reason)
        else:
            path = await media.write_temp(raw, suffix=".bin")
            try:
                converted, converted_mime = await media.extract_audio(path, config.MEDIA_MAX_AUDIO_SECONDS)
                if converted:
                    result.items.append(media.MediaItem(
                        data_url=_to_data_url(converted, converted_mime), kind="audio",
                    ))
                    # Voice/Audio objects carry their own duration from Telegram —
                    # no probe() needed to know if the clip got cut short.
                    src_duration = getattr(audio_obj, "duration", 0) or 0
                    if src_duration > config.MEDIA_MAX_AUDIO_SECONDS:
                        result.notes.append(
                            f"that's {int(src_duration // 60)} min of audio, i heard "
                            f"the first {int(config.MEDIA_MAX_AUDIO_SECONDS // 60)} min of it"
                        )
                else:
                    result.notes.append("couldn't read that audio")
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass


def _apply_media_budget(result) -> None:
    """
    Enforce the per-turn item and byte budgets, keeping audio first.

    Audio survives truncation ahead of frames because it carries the words —
    losing a frame costs a glimpse, losing the audio costs the entire message.
    Truncation is reported so the model can say so instead of confidently
    answering about a video it only half saw.
    """
    max_items = config.MEDIA_MAX_ITEMS_PER_TURN
    max_bytes = config.MEDIA_MAX_TURN_BYTES

    audio = [i for i in result.items if i.kind == "audio"]
    images = [i for i in result.items if i.kind != "audio"]

    kept, total, dropped = [], 0, 0
    for item in audio + images:
        if len(kept) >= max_items or total + item.size() > max_bytes:
            dropped += 1
            continue
        kept.append(item)
        total += item.size()

    if dropped:
        logger.info(f"Media budget dropped {dropped} of {len(result.items)} items.")
        result.notes.append("that was a lot of media, so only part of it came through")

    # Restore the original order among what survived, so frames stay sequential.
    order = {id(i): n for n, i in enumerate(result.items)}
    result.items = sorted(kept, key=lambda i: order[id(i)])


# Debounce bookkeeping, keyed (chat_id, user_id) -> last message timestamp.
_chat_last_msg_time: dict = {}
_DEBOUNCE_KEY_LIMIT = 2000


def _prune_debounce_keys() -> None:
    """Drop the oldest debounce entries so the dict can't grow without bound."""
    if len(_chat_last_msg_time) <= _DEBOUNCE_KEY_LIMIT:
        return
    for key, _ in sorted(_chat_last_msg_time.items(), key=lambda kv: kv[1])[:len(_chat_last_msg_time) // 2]:
        _chat_last_msg_time.pop(key, None)


# Every command the bot actually implements, derived from the handlers above.
# Used to tell "a command I don't have" apart from ordinary chat that happens to
# begin with a slash, so an unknown /command gets a straight answer instead of
# being handed to the LLM to hallucinate a response about.
KNOWN_COMMANDS = {
    "activate", "allow_user", "ban", "cancel", "cancel_all", "clear", "compact",
    "compress", "deactivate", "demote", "disable_skill", "disallow_user",
    "enable_skill", "help", "install_skill", "memory", "model", "mute", "new",
    "new_session", "promote", "sessions", "set_main_account", "set_title",
    "skills", "start", "status", "stop", "stop_all", "switch_session",
    "toggle_reply", "toggle_tools", "unban", "uninstall_skill", "unmute",
}

_COMMAND_RE = _re.compile(r"^/([A-Za-z0-9_]+)(?:@(\S+))?\s*")


def unknown_command(message: Message, bot_username: str) -> Optional[str]:
    """
    The command name if this message is a slash-command the bot doesn't have.

    Returns None for known commands, for commands addressed to a *different*
    bot in the group, and for anything that isn't a command.
    """
    text = (message.text or message.caption or "").strip()
    m = _COMMAND_RE.match(text)
    if not m:
        return None

    name, addressed_to = m.group(1), m.group(2)
    if addressed_to and bot_username and addressed_to.lower() != bot_username.lower():
        return None  # someone else's bot
    if name.lower() in KNOWN_COMMANDS:
        return None
    return name


@router.message(ShouldRespondFilter())
async def handle_chat_message(message: Message, bot: Bot, album: list = None):
    """
    General message handler that handles conversational response generation.
    Appends messages to the history cache and calls the LLM agent.
    Registers task in agent._running_tasks to support emergency /stop cancellation.

    `album` is injected by AlbumMiddleware when this message is part of a
    multi-photo album; `message` is then the member carrying the caption and
    `album` holds every member, so the whole album is one turn.
    """
    chat_id = message.chat.id
    batch = album or [message]
    raw_text = message.text or message.caption or ""

    global BOT_USERNAME
    if not BOT_USERNAME:
        await init_bot_info(bot)

    # Swap the raw @handle for the bot's spoken name rather than deleting it.
    # Deleting it erased the only evidence that the message was addressed to the
    # bot at all, so in a group the model saw a bare "what do you think?" and
    # applied its stay-out-of-other-people's-conversations rule — the bot got
    # @-mentioned and answered with silence.
    cleaned_text = raw_text
    if BOT_USERNAME:
        spoken_name = config.BOT_ALIASES[0] if config.BOT_ALIASES else "brodar"
        cleaned_text = _re.sub(
            _re.escape(f"@{BOT_USERNAME}"), spoken_name, raw_text, flags=_re.IGNORECASE
        ).strip()

    # An unknown /command used to fall through to the LLM, which would
    # confidently improvise an answer about a feature that doesn't exist.
    unknown = unknown_command(message, BOT_USERNAME)
    if unknown:
        await message.reply(f"i don't have a /{unknown}. try /help.")
        return

    has_media = any(_message_has_media(m) for m in batch)

    # Nothing to do if there's neither text nor a visual.
    if not cleaned_text and not has_media:
        return

    # Register for /stop cancellation before any awaiting work, so a stuck
    # download or debounce sleep can still be killed.
    current_task = asyncio.current_task()
    if current_task:
        agent.register_running_task(chat_id, current_task)

    import time
    start_time = time.time()

    # Everything from here down is inside the try. Media download, ffmpeg
    # extraction and the history write used to sit ABOVE it, so any failure
    # there propagated to main.py's catch-all, which logs and swallows it:
    # no reply, no error message, the bot simply appeared dead.
    try:
        # Claim the debounce slot BEFORE any slow work. Extracting a two-minute
        # video takes real time, and the timestamp used to be stamped after it —
        # so a follow-up text would replace this message in the slot, reply
        # first, and then this handler would wake up and reply a second time to
        # the same conversation. Stamping here means the newer message wins.
        debounce_key = (chat_id, message.from_user.id if message.from_user else 0)
        msg_time = time.time()
        _chat_last_msg_time[debounce_key] = msg_time
        _prune_debounce_keys()

        # Whether this sender may drive state-changing tools (persona/skill edits,
        # group moderation). Checked once here and passed into the agent.
        privileged = await is_user_privileged(message, bot)
        show_tool_notes = await cache.get_chat_tool_notes(chat_id)

        # ── Media ──────────────────────────────────────────────────────
        # The whole batch (an album is several messages, one turn) is extracted
        # once, tagged by kind, stored, and then read back together with anything
        # retained from recent turns.
        media_items = []           # this turn's media, attached to this message
        context_media_items = []   # earlier turns' media, as a context block
        extraction_notes = []
        this_turn_is_gif = False
        had_prior_media = False

        spec = models.resolve_spec(await cache.get_active_model())
        can_take_media = spec.supports_vision or spec.supports_audio

        if can_take_media:
            # Advance the per-chat turn counter (drives media aging). Returns the
            # last turn that carried media, so we know whether to look for more.
            turn, last_visual_turn = await cache.bump_chat_turn(chat_id)

            current = []
            if has_media:
                # Show typing during extraction too — downloading and decoding a
                # video takes seconds, and without this the chat looks dead for
                # all of them.
                async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                    result = await extract_media(batch, bot)
                current = [
                    {"data_url": i.data_url, "kind": i.kind, "is_gif": i.is_gif}
                    for i in result.items
                ]
                extraction_notes = list(result.notes)
                this_turn_is_gif = any(i.is_gif for i in result.items)
                if current:
                    await cache.remember_visuals(chat_id, turn, current)
                    last_visual_turn = turn

            # This turn's own media is NEVER subject to the retention cap — that
            # cap is "keep the newest N rows", so applying it here threw away the
            # start of a video the user had literally just sent.
            media_items = current

            if config.VISUAL_MEMORY_TURNS > 0:
                min_turn = turn - config.VISUAL_MEMORY_TURNS + 1
                if last_visual_turn >= min_turn:
                    retained = await cache.recall_visuals(
                        chat_id, min_turn, config.VISUAL_MEMORY_MAX_IMAGES + len(current)
                    )
                    context_media_items = [r for r in retained if r["turn"] < turn]
                    context_media_items = context_media_items[-config.VISUAL_MEMORY_MAX_IMAGES:]
                    had_prior_media = bool(context_media_items)
                else:
                    # Nothing in the window, but stale rows may still be sitting
                    # in Postgres — recall_visuals is the only thing that prunes,
                    # and we just skipped it.
                    await cache.prune_visuals(chat_id, min_turn)

        # What we store/show as the user's text (media isn't persisted in history).
        # In groups it's prefixed with the speaker's name for multi-person context.
        if cleaned_text:
            text_for_model = cleaned_text
        elif message.sticker:
            text_for_model = "[sent a sticker]"
        elif message.voice or message.audio:
            text_for_model = "[sent an audio message]"
        elif message.video_note or message.video:
            text_for_model = "[sent a video]"
        elif message.animation:
            text_for_model = "[sent a gif]"
        elif len(batch) > 1:
            text_for_model = f"[sent {len(batch)} photos]"
        elif has_media:
            text_for_model = "[sent media]"
        else:
            text_for_model = ""

        notes = list(extraction_notes)
        if has_media and not can_take_media:
            notes.append(
                "the user sent media but the current model can't process it — "
                "tell them to switch with /model to a multimodal model."
            )
        # Scoped to THIS turn's extraction. It used to be OR-ed with the retained
        # rows' flag, so a plain photo got labelled "extracted from a video/gif"
        # for as long as an old GIF stayed in the window.
        if this_turn_is_gif:
            notes.append("some attached images are still frames from a video/gif.")
        if had_prior_media:
            notes.append(
                "some attached media is from the last few messages, kept so you can answer "
                "follow-ups about them; the newest belong to the current message."
            )
        if notes:
            text_for_model += "\n\n(note: " + " ".join(notes) + ")"

        if message.forward_origin:
            text_for_model = f"[Forwarded message]\n{text_for_model}"

        if message.reply_to_message:
            r_msg = message.reply_to_message
            r_speaker = _speaker_name(r_msg)
            r_text = r_msg.text or r_msg.caption or "[media]"
            r_text = r_text[:200] + ("..." if len(r_text) > 200 else "")
            text_for_model = f"[Replying to {r_speaker}: '{r_text}']\n{text_for_model}"

        attributed_text = _attribute(message, text_for_model)

        # Prime the cache from DB BEFORE appending the new message.
        # Without this, a cold-start save creates an empty deque that
        # shadows the database, erasing all prior context.
        await cache.get_chat_history(chat_id)

        # Save user message to history IMMEDIATELY so follow-up messages see it in context.
        cache.save_messages_async(chat_id, [{"role": "user", "content": attributed_text}])

        # ── Rapid-fire & Forwarded Debounce ───────────────────────────
        # Batch a burst from ONE person into a single reply: wait a moment, and
        # if that same person sent something newer meanwhile, let their newer
        # handler answer for the whole burst. The slot was claimed above; this is
        # just the wait.
        #
        # Keyed per (chat, sender), not per chat. With a single per-chat slot,
        # anyone else typing cancelled the pending reply to the person the bot
        # was actually answering — in a busy group the bot could be starved
        # indefinitely and never reply to anyone.
        #
        # Another bot's messages get a longer pause so humans can keep up, but
        # 6s was long enough to read as the bot being broken.
        is_other_bot = getattr(message.from_user, "is_bot", False) if message.from_user else False
        if message.forward_origin:
            delay = 3.0
        elif is_other_bot:
            delay = 2.5
        else:
            delay = 1.5

        await asyncio.sleep(delay)

        # If a newer message from the same sender arrived while we slept, its
        # handler updated the timestamp. Abort and let it answer the combined
        # history.
        if _chat_last_msg_time.get(debounce_key) != msg_time:
            logger.info(f"Skipping generation in chat {chat_id} because a newer message arrived.")
            return

        # Now fetch the history (which includes our immediately-saved message, plus any others).
        history = await cache.get_chat_history(chat_id)

        is_group = message.chat.type in ("group", "supergroup")

        # Server-side bot-loop safety net. If recent history looks like a
        # bot-to-bot infinite conversation, skip the LLM call entirely.
        if is_group and _looks_like_bot_loop(history):
            # Warning, not info: if this ever fires wrongly the bot goes quiet,
            # and the last version of this check fired wrongly all the time. It
            # needs to be visible in the Render logs.
            logger.warning(
                f"Bot-loop detected in chat {chat_id} "
                f"({BOT_LOOP_THRESHOLD} consecutive [BOT] messages); staying silent."
            )
            return

        # Decided once, in code, before the call — not left to the model to
        # judge its own mode while also trying to stay in character. Requires
        # BOTH a trigger phrase ("transcribe", "tarjima qil", ...) AND media
        # this turn or carried over from a recent one.
        turn_mode = response_mode.classify(cleaned_text, has_media or had_prior_media)

        # Who's actually asking, and where — needed for the approval card's
        # provenance fields (Phase 9). Not the same thing as `privileged`: this
        # is identity, not authority.
        requester_ctx = None
        if message.from_user:
            requester_ctx = {
                "user_id": message.from_user.id,
                "username": message.from_user.username,
                "full_name": message.from_user.full_name,
                "message_id": message.message_id,
            }
        chat_ctx = {
            "id": message.chat.id,
            "title": getattr(message.chat, "title", None),
            "type": message.chat.type,
        }

        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            logger.info(f"Generating agent response for chat {chat_id} (mode={turn_mode})...")
            bot_reply = await agent.generate_response(
                history,
                bot_instance=bot,
                chat_id=chat_id,
                requester_is_privileged=privileged,
                show_tool_notes=show_tool_notes,
                media_items=media_items or None,
                context_media_items=context_media_items or None,
                is_group=is_group,
                mode=turn_mode,
                requester=requester_ctx,
                chat_info=chat_ctx,
            )

        if turn_mode == "extraction":
            # No control-token parsing, no [SILENT], no reactions, no forced
            # lowercase — those are all conversational-persona machinery, and
            # applying any of them to a transcript is exactly the "it clowns
            # around" bug this mode exists to fix.
            bot_reply = (bot_reply or "").strip()
            if not bot_reply:
                logger.info(f"No text to send (empty extraction reply) in chat {chat_id}")
                return
        else:
            # ── Control-token parsing ──────────────────────────────────
            bot_reply, emoji_reaction, wants_silence = parse_control_tokens(bot_reply)

            if emoji_reaction:
                try:
                    from aiogram.types import ReactionTypeEmoji
                    await message.react(reaction=[ReactionTypeEmoji(type="emoji", emoji=emoji_reaction)])
                    logger.info(f"Bot reacted with {emoji_reaction} in chat {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to react to message in chat {chat_id}: {e}")

            # ── [SILENT] interception ───────────────────────────────────
            if wants_silence and is_group:
                # Model chose to stay silent. User message is already in history.
                logger.info(f"[SILENT] Model chose silence in chat {chat_id}")
                return

            if wants_silence and not bot_reply:
                # A DM: silence isn't an option here, and the token stripped the
                # whole reply. Ask rather than send nothing — but if it also
                # reacted, the reaction alone is a complete answer.
                if not emoji_reaction:
                    bot_reply = "hmm?"

            if config.FORCE_LOWERCASE:
                bot_reply = enforce_lowercase(bot_reply)

            # Reaction-only turn: nothing left to send.
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
        # still saved to history instead of being silently lost. User message was
        # already saved at the top, so we only save the assistant's reply.
        cache.save_messages_async(chat_id, [
            {"role": "assistant", "content": bot_reply},
        ])

        if turn_mode == "extraction":
            await send_extraction_result(message, bot_reply)
        else:
            await send_long_reply(message, bot_reply)

        # After replying, check if the context grew past the token threshold and
        # compact in the background (it makes its own LLM call — don't block).
        import session_manager
        _spawn_maintenance(session_manager.maybe_auto_compact(chat_id))

    except asyncio.CancelledError:
        logger.info(f"Task execution for chat {chat_id} was cancelled by emergency stop.")
        raise
    except TimeoutError as e:
        # A bounded failure, not a crash — say which so it doesn't read as the
        # generic "everything is broken" message.
        logger.error(f"Timed out handling message in chat {chat_id}: {e}")
        try:
            await message.reply("the model's taking way too long. try again in a sec.")
        except Exception as send_err:
            logger.error(f"Failed to send timeout message: {send_err}")
    except Exception as e:
        logger.error(f"Error handling user message in chat {chat_id}: {e}", exc_info=True)
        try:
            await message.reply("my circuits are fried or something. try again later.")
        except Exception as send_err:
            logger.error(f"Failed to send error message: {send_err}")
    finally:
        agent.unregister_running_task(chat_id, current_task)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_passive(message: Message, bot: Bot, album: list = None):
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

    batch = album or [message]
    text = (message.text or message.caption or "").strip()
    has_media = any(_message_has_media(m) for m in batch)
    if not text and not has_media:
        return

    # Capture media posted WITHOUT mentioning the bot, so when it's later
    # @-mentioned it can still see it (the mention-only blind spot). Only when a
    # model that can use it is active — otherwise there's nothing to gain.
    spec = models.resolve_spec(await cache.get_active_model())
    if spec.supports_vision or spec.supports_audio:
        turn, _ = await cache.bump_chat_turn(chat_id)

        if has_media:
            try:
                result = await extract_media(batch, bot)
                if result.items:
                    await cache.remember_visuals(chat_id, turn, [
                        {"data_url": i.data_url, "kind": i.kind, "is_gif": i.is_gif}
                        for i in result.items
                    ])
            except Exception as e:
                logger.error(f"Passive media capture failed in chat {chat_id}: {e}")

        # Prune here too. Pruning only ever happened inside recall_visuals, which
        # this handler never calls — so in a mention-only group multi-megabyte
        # base64 rows piled up in Postgres indefinitely.
        if config.VISUAL_MEMORY_TURNS > 0:
            await cache.prune_visuals(chat_id, turn - config.VISUAL_MEMORY_TURNS + 1)

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
