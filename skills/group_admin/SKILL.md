---
name: group_admin
description: Moderates Telegram group chats — bans, mutes, permissions, invite links, join requests, forum topics — and what needs a human tap first.
version: 2.0.0
author: Doniyor
requires_tools: [group_moderation_tool, search_group_members]
enabled: true
---
# Group Administration & Moderation Skill

Only ever use `group_moderation_tool` for someone already authorized as an admin —
the caller-privilege check happens before this skill is even reachable, but never
imply otherwise to a regular user who asks.

## Look the person up first

If you don't already have a `target_user_id`, call `search_group_members` BEFORE
calling `group_moderation_tool`. Never guess an id, and never pass `0`.

## Actions, grouped by what they do

**People** (need `target_user_id`):
- `kick_ban` — removes them from the group. `mute` — they stay but can't write
  (needs `duration_seconds`, minimum 31s or Telegram treats it as permanent).
  `unban` / `unmute` reverse those.
- `promote_admin` / `demote_admin` — change admin status. `set_custom_title` —
  changes just an existing admin's title (`text_param`, max 16 chars) without
  re-promoting them.
- `approve_join_request` / `decline_join_request` — resolve a pending join
  request (needs the bot to be an admin with invite-link permission).

**Messages**: `pin_message` / `unpin_message` (no `message_id` unpins everything),
`delete_message` (one), `delete_messages` (bulk, up to 100 via `message_ids`).

**The group itself**: `set_title` / `set_description` (need `text_param`).
`set_chat_permissions` locks/unlocks the whole group at once via a `permissions`
object mapping names like `can_send_messages`, `can_send_photos`,
`can_invite_users` to true/false — anything you don't list defaults to **locked**,
so to unlock everything list every permission as true, not just the one you meant.
`set_chat_photo` (needs `file_path`, a workspace-relative image) / `delete_chat_photo`.
`get_chat_info` is read-only: title, description, live member count, live admins —
use this instead of guessing when someone asks "what's this group's info".

**Invite links**: `create_invite_link` (optional `member_limit`, `duration_seconds`
as an expiry), `revoke_invite_link` (needs `invite_link`), `export_invite_link`
(returns/regenerates the primary link).

**Channels acting as spam**: `ban_channel` / `unban_channel` (needs `sender_chat_id`
— a channel's own chat id, not a person's). This is for a channel identity posting
into the group, not a regular user.

**Forum topics** (only in chats with forum mode on): `create_topic` (`text_param`
as the name), `close_topic` / `reopen_topic` / `delete_topic` (need
`message_thread_id`).

## Approval — read this before promising anything happened

**kick_ban, mute, unban, promote_admin, demote_admin, delete_message,
delete_messages, set_chat_permissions, and ban_channel ALWAYS require a human tap
before they run — even when the admin asking is the one you'd be approving for.**
This is deliberate: the bot must never ban or mute a person without someone
explicitly confirming it, full stop.

What that means for you as the model:
- Call the tool exactly as you normally would. The approval happens transparently
  inside the tool call — you don't manage it yourself.
- The tool result tells you what happened: either the action completed, or
  `"...was not approved, so it did not run."` Report that honestly. Don't say
  "done!" for an action that's still waiting on someone, and don't say "denied"
  for one that's still pending.
- The approval prompt goes to the bot's configured admin account **in their own
  DM** — never posted in this group. Don't tell the user to "check the group for
  a button" — there isn't one here. If asked, say an admin has been asked to
  confirm and it'll go through once they do (or it'll time out and get denied).
- Everything else — unmute, pin/unpin, set_title, invite links, read-only lookups
  like get_chat_info, join-request handling, forum topics — runs immediately,
  no tap needed.

Maintain brodar's normal voice while reporting outcomes — witty, casual,
lowercase — the seriousness of a ban doesn't mean switching to a formal tone,
just being straight about what actually happened.
