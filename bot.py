import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, BaseFilter
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

import config
import cache
import agent

logger = logging.getLogger(__name__)

# Router for all bot handlers
router = Router()

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
    1. Private Chat (DMs) -> Always responds.
    2. Group / Supergroup:
       - Responds if Mention-Only is disabled.
       - Responds if Mention-Only is enabled AND the bot is mentioned or replied to.
    """
    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.chat.type == "private":
            return True

        chat_id = message.chat.id
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

async def is_sender_admin(message: Message, bot: Bot) -> bool:
    """Checks if the user who sent the command is an administrator in the chat."""
    if message.chat.type == "private":
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.warning(f"Error checking admin status for user {message.from_user.id} in chat {message.chat.id}: {e}")
        return False

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Start command handler. Casual, sarcastic and lowercase."""
    text = (
        "oh, hello. i'm an ai assistant bot.\n"
        "i can reply to your text, search the web, or run safe shell utilities.\n"
        "in groups, i only reply when mentioned by default. admins can use /toggle_reply to change that.\n"
        "ask me anything. or don't. i don't really mind."
    )
    await message.reply(text)

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Help command handler. Casual description of commands."""
    text = (
        "here is what you can do with me:\n"
        "- type anything to talk. i'll respond using an llm.\n"
        "- i might use web search or shell tools in the background if you ask for it.\n"
        "- /toggle_reply (admins only): toggle whether i reply to all messages in groups or only mentions."
    )
    await message.reply(text)

@router.message(Command("toggle_reply"))
async def cmd_toggle_reply(message: Message, bot: Bot):
    """
    Toggles the Reply Mode (Mention-Only vs Free-Reply) for group chats.
    Restricted to group administrators.
    """
    if message.chat.type == "private":
        await message.reply("this is a private chat. i always reply to everything here. no need to toggle.")
        return

    # Check if sender is admin
    if not await is_sender_admin(message, bot):
        await message.reply("nice try, but you're not an admin. i only listen to the bosses.")
        return

    chat_id = message.chat.id
    current_mention_only = await cache.get_chat_setting(chat_id)
    new_mention_only = not current_mention_only

    # Write-through to cache and background DB write
    cache.set_chat_setting(chat_id, new_mention_only)

    if new_mention_only:
        await message.reply("got it. i'll only reply when mentioned from now on. peace.")
    else:
        await message.reply("fine. i'll respond to every message in this chat. prepare for spam.")

@router.message(ShouldRespondFilter())
async def handle_chat_message(message: Message, bot: Bot):
    """
    General message handler that handles conversational response generation.
    Appends messages to the history cache and calls the LLM agent.
    Uses an async typing indicator to show that the bot is thinking.
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else "user"
    raw_text = message.text or message.caption or ""

    # Clean the bot's username mention from the text so it doesn't clutter the LLM context
    global BOT_USERNAME
    if not BOT_USERNAME:
        await init_bot_info(bot)
        
    cleaned_text = raw_text
    if BOT_USERNAME:
        cleaned_text = raw_text.replace(f"@{BOT_USERNAME}", "").strip()

    # If the message became empty after cleaning, don't generate response
    if not cleaned_text:
        return

    # 1. Fetch current conversation history from cache (or DB on miss)
    history = await cache.get_chat_history(chat_id)
    
    # 2. Append the current user message to context list for LLM call
    user_msg_entry = {"role": "user", "content": cleaned_text}
    temp_history = history + [user_msg_entry]

    # 3. Request LLM generation while showing typing indicator
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            logger.info(f"Generating agent response for chat {chat_id}...")
            bot_reply = await agent.generate_response(temp_history)
        
        # 4. Send response to Telegram
        sent_message = await message.reply(bot_reply)

        # 5. Update cache instantly and trigger async DB writes
        cache_user_msg = {"role": "user", "content": cleaned_text}
        cache_bot_msg = {"role": "assistant", "content": bot_reply}
        
        # Write-through to cache & async background DB write
        cache.save_messages_async(chat_id, [cache_user_msg, cache_bot_msg])

    except Exception as e:
        logger.error(f"Error handling user message in chat {chat_id}: {e}", exc_info=True)
        try:
            await message.reply("my circuits are fried or something. try again later.")
        except Exception as send_err:
            logger.error(f"Failed to send error message: {send_err}")
