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
2. Provide a name you know is in that group as the `query`.
3. Each result line has explicit fields, e.g.
   `- name: Bob Smith | username: @bobsmith | user_id: 123456 | chat_id: -100123456789`
4. Read the `chat_id:` field directly. Don't try to parse it out of prose.

## Finding a User's Account ID
If you are asked to find a specific user's ID:
1. Use `search_group_members`, omit `target_chat_id` to search everywhere, and pass
   their name, @username, or numeric id as the `query`. Partial names work.
2. Read the `user_id:` field from the matching line.
3. The same person can appear once per group they're in — check the `chat_id:` field
   is the group you actually mean before moderating.

## What the bot knows
Membership is stored in Postgres, so it survives restarts. The bot knows anyone it has
seen send a message or a command in a chat it's in. It does NOT know silent members it
has never seen speak.

## Third-Party Bot Fallback
If you cannot find the user or group in your memory cache (e.g., they haven't spoken recently or it's a cold start), you can inform the user that they can easily find IDs by using dedicated Telegram bots.
Tell them to:
1. Open Telegram and search for `@get_id_bot` or `@RawDataBot`.
2. Start the bot to instantly get their own Account ID.
3. Forward a message from any group/channel to the bot to get that group's Chat ID.
