---
name: telegram-directory
description: How to look up Telegram user/chat IDs, find people by name, and list a group's admins — and what's genuinely impossible to look up.
---

# Telegram Directory Lookups

Telegram's Bot API has **no method to list every member of a group**. Full stop —
there is no endpoint for it, no workaround, nothing this bot can do to fake it. Be
upfront about that instead of pretending a search came up empty when the real answer
is "that question can't be answered." The three things the API *can* answer:

| Question | Tool | Notes |
|---|---|---|
| "who are the admins here" | `search_group_members` scoped to this chat | Admins are fetched LIVE every time — always accurate |
| "how many people are in this group" | comes back automatically alongside the admin roster (`total members: N`) | Live count, not the local table's size |
| "who is @someone" / "find aziz" | `search_group_members` with a real query | Matches the bot's own memory of who it's seen, PLUS the live admin list |
| "list literally everyone in this group" | not possible | say so plainly — see below |

## Finding someone by name or @username

Call `search_group_members` with `query` set to their name, @username, or numeric id.
Partial names work, word order doesn't matter ("karimov aziz" matches "Aziz Karimov"),
and it doesn't matter whether the name was typed in Latin or Cyrillic script — Uzbek
and Russian names fold to the same representation either way, so "aziz" finds "азиз"
and vice versa.

Each result line has explicit labelled fields — read them directly, don't parse prose:
```
- name: Bob Smith | username: @bobsmith | user_id: 123456 | chat_id: -100123456789 [admin]
```
The `[admin]` / `[BOT]` tags are live status, not guesses.

**Scoping and permission**: leave `target_chat_id` out and it searches the CURRENT
chat — that's open to anyone, admin or not, as long as `query` is non-empty (this is
the same thing @userinfobot does, nothing sensitive about it). An empty `query`, or a
`target_chat_id` pointed at a *different* chat, requires the requester to be an admin
— that shape is closer to "dump everything the bot knows across every chat," and only
an admin gets that.

## Finding a group's chat ID

Same tool: search for a name you know is in that group and read the `chat_id:` field
off the matching line.

## What the bot actually knows

Two sources, merged automatically by `search_group_members` whenever it's scoped to
one chat:

1. **Live from Telegram, every time**: the current admin list and total member count
   (`group_tools.list_admins`, wrapping `getChatAdministrators` + `getChatMemberCount`).
   Always correct, costs nothing to ask for, and any admin found this way gets saved
   into the bot's own memory so future searches are faster.
2. **The bot's own memory** (Postgres `chat_members`, survives restarts): everyone who
   has ever sent a message, plus everyone the bot has seen join, leave, get promoted,
   or get demoted — Telegram delivers those as `chat_member` updates whenever the bot
   is an admin, independent of whether that person has ever said a word. Someone who
   joined silently and never spoke IS in this table, as long as it happened after the
   bot became an admin in that chat. A departed member's row is kept (marked left, not
   deleted), so "didn't aziz used to be in here?" still resolves.

What it genuinely does NOT know: anyone who joined and left before the bot was ever
an admin there, or a chat the bot isn't in at all.

## When the answer really is "I don't know"

If a search comes up empty and the live admin check didn't answer it either, say that
plainly instead of stalling or guessing an id. As a last resort you can point the
person at a dedicated ID-lookup bot:
1. Open Telegram and search for `@get_id_bot` or `@RawDataBot`.
2. Start it to get their own account id instantly.
3. Forward a message from any group/channel to it to get that chat's id.
