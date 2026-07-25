---
name: find-telegram-id
description: How to find Telegram User IDs, Account IDs, and Group/Chat IDs.
---

# Finding Telegram IDs

Telegram User IDs and Chat IDs are internal numerical identifiers used for automation and moderation. 

When the Master Admin or a user asks you to find a User ID (Account ID) or a Group Chat ID, follow these instructions:

## Finding a Group Chat ID
If you are asked to find the ID of a specific group (e.g., "What is the ID of the Brodar group?"):
1. Use the `search_group_members` tool and **omit** the `target_chat_id` parameter.
2. Provide a name you know is in that group as the `query` (or leave it empty to see a sample of all known chats).
3. The tool will return results across all known groups in the format: `User Name (in chat -100123456789) (ID: 123456)`.
4. Extract the Chat ID (e.g., `-100123456789`) and provide it to the user.

## Finding a User's Account ID
If you are asked to find a specific user's ID:
1. Use the `search_group_members` tool, **omit** `target_chat_id` to search globally, and pass their name/username in the `query`.
2. The tool will return their User ID.

## Third-Party Bot Fallback
If you cannot find the user or group in your memory cache (e.g., they haven't spoken recently or it's a cold start), you can inform the user that they can easily find IDs by using dedicated Telegram bots.
Tell them to:
1. Open Telegram and search for `@get_id_bot` or `@RawDataBot`.
2. Start the bot to instantly get their own Account ID.
3. Forward a message from any group/channel to the bot to get that group's Chat ID.
