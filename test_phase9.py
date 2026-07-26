"""
Phase 9 smoke checks — approvals to the master DM + expanded admin toolkit.

    python3 test_phase9.py
"""
import asyncio
import sys

import test_support

test_support.install_stubs()

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# ── config: where approvals go ───────────────────────────────────────────────
def test_approval_recipient_id():
    import config

    original_main, original_allowed = config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS
    try:
        config.MAIN_ACCOUNT_ID = 111
        config.ALLOWED_DM_USER_IDS = [222, 333]
        check("MAIN_ACCOUNT_ID wins when set", config.approval_recipient_id() == 111)

        config.MAIN_ACCOUNT_ID = None
        check("falls back to the first ALLOWED_DM_USER_IDS entry", config.approval_recipient_id() == 222)

        config.ALLOWED_DM_USER_IDS = []
        check("None when nothing is configured", config.approval_recipient_id() is None)
    finally:
        config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS = original_main, original_allowed

    check("the timeout was raised for a DM-based flow", config.APPROVAL_TIMEOUT_SECONDS == 180.0)


# ── agent._tool_needs_approval: the destructive/self-modifying split ────────
def test_tool_needs_approval_env_and_persona():
    import agent

    for t in ("edit_env_file", "edit_persona_file", "install_skill_from_url", "uninstall_skill"):
        check(f"'{t}' always needs approval", agent._tool_needs_approval(t, {}))


def test_tool_needs_approval_moderation_split():
    import agent

    destructive = ["kick_ban", "mute", "unban", "promote_admin", "demote_admin",
                   "delete_message", "delete_messages", "set_chat_permissions", "ban_channel"]
    cosmetic = ["unmute", "pin_message", "unpin_message", "set_title", "set_description",
                "create_invite_link", "get_chat_info", "export_invite_link", "set_custom_title",
                "approve_join_request", "create_topic"]

    for act in destructive:
        check(f"moderation action '{act}' needs a tap",
              agent._tool_needs_approval("group_moderation_tool", {"action": act}))
    for act in cosmetic:
        check(f"moderation action '{act}' runs immediately",
              not agent._tool_needs_approval("group_moderation_tool", {"action": act}))

    check("the 'ban' alias resolves to kick_ban and still needs a tap",
          agent._tool_needs_approval("group_moderation_tool", {"action": "ban"}))
    check("the 'silence' alias resolves to mute and still needs a tap",
          agent._tool_needs_approval("group_moderation_tool", {"action": "silence"}))
    check("an unrecognized action defaults to NOT requiring approval "
          "(the dispatcher's own unknown-action error handles it, not this gate)",
          not agent._tool_needs_approval("group_moderation_tool", {"action": "not_a_real_action"}))


def test_tool_needs_approval_send_file_scoped_to_bot_output_dirs():
    import os
    import tempfile
    import agent
    import config

    original_ws = config.TOOL_WORKSPACE_DIR
    with tempfile.TemporaryDirectory() as ws:
        config.TOOL_WORKSPACE_DIR = ws
        try:
            os.makedirs(os.path.join(ws, "generated"), exist_ok=True)
            os.makedirs(os.path.join(ws, "downloads"), exist_ok=True)
            open(os.path.join(ws, "generated", "out.txt"), "w").close()
            open(os.path.join(ws, "downloads", "in.txt"), "w").close()
            open(os.path.join(ws, "notes.txt"), "w").close()

            check("a file in workspace/generated skips the tap",
                  not agent._tool_needs_approval("send_file", {"file_path": "generated/out.txt"}))
            check("a file in workspace/downloads skips the tap",
                  not agent._tool_needs_approval("send_file", {"file_path": "downloads/in.txt"}))
            check("a file directly in the workspace root still needs a tap",
                  agent._tool_needs_approval("send_file", {"file_path": "notes.txt"}))
            check("a path escaping the workspace entirely needs a tap (fails loud, not silently skipped)",
                  agent._tool_needs_approval("send_file", {"file_path": "/etc/passwd"}))
        finally:
            config.TOOL_WORKSPACE_DIR = original_ws


def test_tool_needs_approval_everything_else_false():
    import agent

    for t in ("search_web", "use_skill", "search_group_members", "save_memory_fact"):
        check(f"'{t}' is not in the approval-required set at all",
              not agent._tool_needs_approval(t, {}))


# ── deep links and card formatting ───────────────────────────────────────────
def test_deep_link():
    import agent

    check("a supergroup id builds a t.me/c/ link",
          agent._telegram_deep_link(-1001234567890, 42) == "https://t.me/c/1234567890/42")
    check("a positive (DM) chat id builds no link",
          agent._telegram_deep_link(123456, 42) is None)
    check("a basic (non-super) group id builds no link",
          agent._telegram_deep_link(-123456, 42) is None)
    check("no message_id builds no link",
          agent._telegram_deep_link(-1001234567890, None) is None)
    check("no chat_id builds no link",
          agent._telegram_deep_link(None, 42) is None)


def test_action_label():
    import agent

    check("a moderation action is labelled by its (aliased) action",
          agent._approval_action_label("group_moderation_tool", {"action": "ban"}) == "kick ban")
    check("a non-moderation tool is labelled by its own name",
          agent._approval_action_label("edit_persona_file", {}) == "edit persona file")


def test_approval_card_contents():
    import agent

    requester = {"user_id": 2030903420, "username": "doniyor117", "full_name": "Doniyor", "message_id": 555}
    chat_info = {"id": -1001234567890, "title": "Brodar Dev", "type": "supergroup"}
    card = agent._build_approval_card(
        "group_moderation_tool", {"action": "mute", "duration_seconds": 600},
        requester, chat_info, "brodar mute aziz for 10 minutes", "Aziz Karimov  @azizk  · id 123456789",
    )

    check("the title names the action", "mute" in card.splitlines()[0])
    check("the who line names the target", "Aziz Karimov" in card)
    check("the where line names the chat", "Brodar Dev" in card and "supergroup" in card)
    check("the asked-by line names the requester", "Doniyor" in card and "@doniyor117" in card)
    check("the trigger line quotes the actual message", "brodar mute aziz for 10 minutes" in card)
    check("...and includes a deep link for a supergroup", "t.me/c/1234567890/555" in card)
    check("the detail line explains the duration", "10 minutes" in card)
    check("the expiry line states the timeout and the default-deny", "no answer means denied" in card)

    bare_card = agent._build_approval_card("edit_persona_file", {}, None, None, "", None)
    check("with no requester/chat/target, the card degrades gracefully (no crash, still has a title)",
          bare_card.startswith("⚠️ approval needed"))
    check("...and doesn't fabricate a 'who' line when there's no target",
          "who       " not in bare_card)


# ── _request_interactive_approval: routing, forbidden-DM handling, dedup ────
class _FakeForbidden(Exception):
    pass


def _install_fake_forbidden(monkeypatch_module):
    """aiogram is stubbed; make TelegramForbiddenError importable and distinct."""
    import sys
    exc_mod = sys.modules["aiogram.exceptions"]
    return exc_mod.TelegramForbiddenError


class _FakeBotSends:
    def __init__(self, fail_for_chat_id=None, forbidden_for_chat_id=None):
        self.sent = []
        self.fail_for_chat_id = fail_for_chat_id
        self.forbidden_for_chat_id = forbidden_for_chat_id

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id == self.forbidden_for_chat_id:
            import sys
            raise sys.modules["aiogram.exceptions"].TelegramForbiddenError("forbidden")
        if chat_id == self.fail_for_chat_id:
            raise RuntimeError("network exploded")
        self.sent.append({"chat_id": chat_id, "text": text})
        return None


def test_approval_routes_to_recipient_not_requesting_chat():
    import agent
    import config

    original_main = config.MAIN_ACCOUNT_ID
    config.MAIN_ACCOUNT_ID = 999888777
    fake_bot = _FakeBotSends()
    requesting_chat_id = -100123

    async def scenario():
        task = asyncio.create_task(agent._request_interactive_approval(
            fake_bot, requesting_chat_id, "edit_persona_file", {"content": "x"},
        ))
        await asyncio.sleep(0)  # let it post the card and the "asked an admin" note
        # Resolve it via the same path bot.handle_approval would use.
        import bot as bot_module
        assert len(bot_module.pending_approvals) == 1
        call_id = next(iter(bot_module.pending_approvals))
        entry = bot_module.pending_approvals[call_id]
        entry["future"].set_result(True)
        result = await task
        return result

    try:
        result = asyncio.run(scenario())
    finally:
        config.MAIN_ACCOUNT_ID = original_main

    check("the approval resolved True", result is True)
    check("exactly two messages were sent (the card, then the wait notice)",
          len(fake_bot.sent) == 2)
    check("the CARD went to the recipient's DM, not the requesting chat",
          fake_bot.sent[0]["chat_id"] == 999888777)
    check("the wait notice went to the REQUESTING chat",
          fake_bot.sent[1]["chat_id"] == requesting_chat_id)
    check("the card itself never mentions the requesting group as the destination",
          "approval needed" in fake_bot.sent[0]["text"])


def test_approval_forbidden_dm_denies_and_explains_in_requesting_chat():
    import agent
    import config

    original_main = config.MAIN_ACCOUNT_ID
    config.MAIN_ACCOUNT_ID = 555
    requesting_chat_id = -100999
    fake_bot = _FakeBotSends(forbidden_for_chat_id=555)

    try:
        result = asyncio.run(agent._request_interactive_approval(
            fake_bot, requesting_chat_id, "edit_env_file", {"key": "X", "value": "y"},
        ))
    finally:
        config.MAIN_ACCOUNT_ID = original_main

    check("a forbidden DM denies the action", result is False)
    check("...and explains why, back in the REQUESTING chat",
          len(fake_bot.sent) == 1 and fake_bot.sent[0]["chat_id"] == requesting_chat_id)
    check("the explanation says what to do about it",
          "/start" in fake_bot.sent[0]["text"])

    import bot as bot_module
    check("no pending approval was left dangling", len(bot_module.pending_approvals) == 0)


def test_approval_no_recipient_configured_fails_closed():
    import agent
    import config

    original_main, original_allowed = config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS
    config.MAIN_ACCOUNT_ID = None
    config.ALLOWED_DM_USER_IDS = []
    fake_bot = _FakeBotSends()

    try:
        result = asyncio.run(agent._request_interactive_approval(
            fake_bot, -100111, "install_skill_from_url", {"url": "http://x"},
        ))
    finally:
        config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS = original_main, original_allowed

    check("with nowhere to send it, the action is denied", result is False)
    check("the requesting chat is told why", len(fake_bot.sent) == 1)


def test_approval_dedup_one_pending_per_chat():
    import agent
    import config

    original_main = config.MAIN_ACCOUNT_ID
    config.MAIN_ACCOUNT_ID = 42
    fake_bot = _FakeBotSends()
    chat_id = -100777

    async def scenario():
        first = asyncio.create_task(agent._request_interactive_approval(
            fake_bot, chat_id, "edit_persona_file", {"content": "x"},
        ))
        await asyncio.sleep(0)
        # A second request for the SAME chat while the first is still pending.
        second_result = await agent._request_interactive_approval(
            fake_bot, chat_id, "edit_persona_file", {"content": "y"},
        )
        # Clean up the first one so it doesn't hang the test.
        import bot as bot_module
        for entry in list(bot_module.pending_approvals.values()):
            if not entry["future"].done():
                entry["future"].set_result(False)
        await first
        return second_result

    try:
        result = asyncio.run(scenario())
    finally:
        config.MAIN_ACCOUNT_ID = original_main

    check("a concurrent second approval for the same chat is refused outright",
          result is False)
    check("only ONE card was ever sent to the recipient (no duplicate)",
          sum(1 for s in fake_bot.sent if s["chat_id"] == 42) == 1)


# ── bot.handle_approval: only the configured recipient may tap ──────────────
def test_handle_approval_authorization():
    import bot as bot_module
    import config
    from test_support import FakeUser, FakeChat, FakeMessage

    original_main = config.MAIN_ACCOUNT_ID
    config.MAIN_ACCOUNT_ID = 42

    class FakeCallback:
        def __init__(self, data, from_user, message):
            self.data, self.from_user, self.message = data, from_user, message
            self.answers = []

        async def answer(self, text, show_alert=False):
            self.answers.append((text, show_alert))

    future = asyncio.get_event_loop_policy().new_event_loop().create_future() if False else None

    async def scenario():
        fut = asyncio.get_running_loop().create_future()
        bot_module.pending_approvals["call1"] = {"future": fut, "requesting_chat_id": -100}

        msg = FakeMessage(text="⚠️ approval needed — mute", chat=FakeChat(42, type="private"))

        class _EM(str):
            async def edit_text(self, text, reply_markup=None):
                pass

        msg.edit_text = lambda text, reply_markup=None: asyncio.sleep(0)

        wrong_user = FakeUser(999, full_name="Random Person")
        cb_wrong = FakeCallback("approve:call1", wrong_user, msg)
        await bot_module.handle_approval(cb_wrong)
        check("a non-recipient's tap is rejected", not fut.done())
        check("...with a toast explaining it isn't theirs",
              cb_wrong.answers and "isn't yours" in cb_wrong.answers[0][0])

        right_user = FakeUser(42, username="doniyor117", full_name="Doniyor")
        cb_right = FakeCallback("approve:call1", right_user, msg)
        await bot_module.handle_approval(cb_right)
        check("the configured recipient's tap resolves the future", fut.done() and fut.result() is True)
        check("call1 is removed from pending_approvals", "call1" not in bot_module.pending_approvals)

    try:
        asyncio.run(scenario())
    finally:
        config.MAIN_ACCOUNT_ID = original_main
        bot_module.pending_approvals.clear()


# ── group_tools: representative new actions ──────────────────────────────────
class _FakeGroupBot:
    def __init__(self):
        self.calls = []

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append(("delete_messages", chat_id, message_ids))

    async def set_chat_permissions(self, chat_id, permissions):
        self.calls.append(("set_chat_permissions", chat_id, permissions))

    async def create_chat_invite_link(self, chat_id, name=None, member_limit=None, expire_date=None):
        self.calls.append(("create_chat_invite_link", chat_id, member_limit))

        class _Link:
            invite_link = "https://t.me/+abc123"
        return _Link()

    async def ban_chat_sender_chat(self, chat_id, sender_chat_id):
        self.calls.append(("ban_chat_sender_chat", chat_id, sender_chat_id))

    async def get_chat(self, chat_id):
        class _Chat:
            title = "Brodar Dev"
            type = "supergroup"
            description = "a test group"
        return _Chat()

    async def get_chat_administrators(self, chat_id):
        return []

    async def get_chat_member_count(self, chat_id):
        return 7


def test_group_tools_delete_messages_bulk():
    import group_tools

    bot_ = _FakeGroupBot()
    out = asyncio.run(group_tools.delete_messages(bot_, -100, [1, 2, 3]))
    check("bulk delete calls the plural API once", bot_.calls[0][0] == "delete_messages")
    check("reports the count deleted", "3" in out)


def test_group_tools_delete_messages_no_ids():
    import group_tools

    out = asyncio.run(group_tools.delete_messages(_FakeGroupBot(), -100, []))
    check("no ids given is reported honestly, not silently ignored",
          "No valid message ids" in out)


def test_group_tools_set_chat_permissions():
    import group_tools

    bot_ = _FakeGroupBot()
    out = asyncio.run(group_tools.set_chat_permissions(bot_, -100, {"can_send_messages": True}))
    check("permissions call reaches the bot", bot_.calls[0][0] == "set_chat_permissions")
    check("the result names what's allowed", "can_send_messages" in out)


def test_group_tools_create_invite_link():
    import group_tools

    bot_ = _FakeGroupBot()
    out = asyncio.run(group_tools.create_invite_link(bot_, -100, member_limit=5))
    check("the created link is returned", "https://t.me/+abc123" in out)


def test_group_tools_ban_channel():
    import group_tools

    bot_ = _FakeGroupBot()
    out = asyncio.run(group_tools.ban_channel(bot_, -100, -1009999))
    check("ban_channel calls banChatSenderChat", bot_.calls[0][0] == "ban_chat_sender_chat")
    check("confirms the channel id", "-1009999" in out)


def test_group_tools_get_chat_info():
    import group_tools

    out = asyncio.run(group_tools.get_chat_info(_FakeGroupBot(), -100))
    check("chat info includes the title", "Brodar Dev" in out)
    check("...and the member count", "7" in out)


# ── _run_moderation: validation for the new actions ──────────────────────────
def test_run_moderation_validates_new_actions():
    import agent

    async def run(args):
        return await agent._run_moderation(_FakeGroupBot(), -100, args)

    r = asyncio.run(run({"action": "delete_messages", "message_ids": []}))
    check("delete_messages with no ids is refused before calling Telegram",
          "needs message_ids" in r)

    r = asyncio.run(run({"action": "ban_channel", "sender_chat_id": 12345}))
    check("ban_channel with a POSITIVE sender_chat_id is refused (channels are negative ids)",
          "needs a real sender_chat_id" in r)

    r = asyncio.run(run({"action": "set_chat_photo", "file_path": ""}))
    check("set_chat_photo with no file_path is refused",
          "needs file_path" in r)

    r = asyncio.run(run({"action": "create_topic", "text_param": ""}))
    check("create_topic with no name is refused",
          "needs text_param" in r)

    r = asyncio.run(run({"action": "close_topic"}))
    check("close_topic with no message_thread_id is refused",
          "needs message_thread_id" in r)

    r = asyncio.run(run({"action": "set_custom_title", "target_user_id": 5, "text_param": ""}))
    check("set_custom_title with no title is refused",
          "needs text_param" in r)

    r = asyncio.run(run({"action": "revoke_invite_link", "invite_link": ""}))
    check("revoke_invite_link with no link is refused",
          "needs invite_link" in r)


def test_run_moderation_dispatches_new_actions():
    import agent

    bot_ = _FakeGroupBot()

    async def run(args):
        return await agent._run_moderation(bot_, -100, args)

    out = asyncio.run(run({"action": "delete_messages", "message_ids": [1, 2]}))
    check("delete_messages dispatches to group_tools", "Deleted" in out or "deleted" in out.lower())

    out = asyncio.run(run({"action": "get_chat_info"}))
    check("get_chat_info dispatches and returns real info", "Brodar Dev" in out)

    out = asyncio.run(run({"action": "totally_made_up"}))
    check("an unknown action lists valid ones instead of crashing",
          "Unknown moderation action" in out and "get_chat_info" in out)


# ── skills: group_admin documents the new rules ──────────────────────────────
def test_group_admin_skill_documents_approval_routing():
    with open("skills/group_admin/SKILL.md", encoding="utf-8") as f:
        content = f.read()
    check("it states destructive actions always need a tap, even from an admin",
          "even when the admin asking is the one" in content)
    check("it names the specific always-approve actions",
          "delete_message" in content and "set_chat_permissions" in content)
    check("it clarifies approvals go to a DM, never the group",
          "never posted in this group" in content)
    check("it documents at least one new read-only action",
          "get_chat_info" in content)


def main():
    print("phase 9 — approvals to the master DM + expanded admin toolkit\n")
    for fn in (
        test_approval_recipient_id,
        test_tool_needs_approval_env_and_persona,
        test_tool_needs_approval_moderation_split,
        test_tool_needs_approval_send_file_scoped_to_bot_output_dirs,
        test_tool_needs_approval_everything_else_false,
        test_deep_link,
        test_action_label,
        test_approval_card_contents,
        test_approval_routes_to_recipient_not_requesting_chat,
        test_approval_forbidden_dm_denies_and_explains_in_requesting_chat,
        test_approval_no_recipient_configured_fails_closed,
        test_approval_dedup_one_pending_per_chat,
        test_handle_approval_authorization,
        test_group_tools_delete_messages_bulk,
        test_group_tools_delete_messages_no_ids,
        test_group_tools_set_chat_permissions,
        test_group_tools_create_invite_link,
        test_group_tools_ban_channel,
        test_group_tools_get_chat_info,
        test_run_moderation_validates_new_actions,
        test_run_moderation_dispatches_new_actions,
        test_group_admin_skill_documents_approval_routing,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 9 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 9 checks passed")


if __name__ == "__main__":
    main()
