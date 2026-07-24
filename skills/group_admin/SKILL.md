---
name: group_admin
description: Moderates Telegram group chats (ban, mute, unmute, set title, description, pin messages).
version: 1.0.0
author: Doniyor
requires_tools: [group_moderation_tool]
enabled: true
---
# Group Administration & Moderation Skill

Use this skill when managing a Telegram group chat:
1. **Security Constraint**: Only execute group moderation actions if requested by an authorized Group Administrator.
2. **Available Actions**:
   - `ban`: Ban a user permanently or for a duration.
   - `mute` / `unmute`: Restrict or restore user chat permissions.
   - `set_title`: Change group name/title.
   - `set_description`: Update group description.
   - `pin` / `unpin`: Pin or unpin important messages.
3. Maintain Brodar's witty, casual, lowercase persona when confirming moderation actions.
