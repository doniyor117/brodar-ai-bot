import uuid
import asyncio
import logging
from typing import Dict
from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

logger = logging.getLogger(__name__)

class PermCallback(CallbackData, prefix="perm"):
    req_id: str
    action: str  # "approve" or "reject"

# Dict mapping req_id -> asyncio.Future[bool]
_pending_requests: Dict[str, asyncio.Future] = {}

async def request_permission_prompt(bot: Bot, chat_id: int, tool_name: str, details: str) -> bool:
    """
    Sends a beautifully formatted Inline Keyboard message asking for user/admin permission.
    Suspends execution until an admin clicks Approve or Reject (or times out in 120s).
    """
    req_id = str(uuid.uuid4())[:8]
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _pending_requests[req_id] = future

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=PermCallback(req_id=req_id, action="approve").pack()),
        InlineKeyboardButton(text="❌ Reject", callback_data=PermCallback(req_id=req_id, action="reject").pack())
    ]])

    text = (
        f"⚠️ <b>PERMISSION APPROVAL REQUIRED</b>\n\n"
        f"• <b>Tool</b>: <code>{tool_name}</code>\n"
        f"• <b>Action/Details</b>: <code>{details}</code>\n\n"
        f"<i>Only group administrators (or authorized DM users) can approve or reject this request.</i>"
    )

    try:
        msg: Message = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        approved = await asyncio.wait_for(future, timeout=120.0)
        return approved
    except asyncio.TimeoutError:
        logger.warning(f"Permission request {req_id} timed out.")
        try:
            await bot.send_message(chat_id, f"⚠️ Permission request for '{tool_name}' expired (timeout).")
        except Exception:
            pass
        return False
    except asyncio.CancelledError:
        logger.info(f"Permission request {req_id} cancelled.")
        return False
    except Exception as e:
        logger.error(f"Error executing permission prompt: {e}")
        return False
    finally:
        _pending_requests.pop(req_id, None)
