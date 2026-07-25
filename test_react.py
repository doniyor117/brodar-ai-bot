import asyncio
from aiogram import Bot
from aiogram.types import Message
import inspect

print(hasattr(Message, "react"))
if hasattr(Message, "react"):
    print(inspect.signature(Message.react))
print(hasattr(Bot, "set_message_reaction"))
