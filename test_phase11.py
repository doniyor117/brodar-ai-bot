"""
Phase 11 smoke checks — docs, commands, test harness.

    python3 test_phase11.py
"""
import os
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


def test_dead_debug_stub_removed():
    check("the 9-line test_react.py debug stub is gone",
          not os.path.exists("test_react.py"))


def test_consolidated_runner_exists():
    check("run_tests.py exists", os.path.isfile("run_tests.py"))
    src = test_support.read_code("run_tests.py")
    check("it discovers every test_phase*.py automatically",
          "test_phase*.py" in src)


def test_readme_reflects_current_architecture():
    with open("README.md", encoding="utf-8") as f:
        content = f.read()
    check("no more stale zai-sdk claim", "zai-sdk" not in content)
    check("no more dead antigravity-cli paths", "antigravity-cli" not in content)
    check("no more claimed Semaphore(1) (concurrency is LLM_CONCURRENCY, default 2)",
          "Semaphore(1)" not in content)
    check("mentions LiteLLM as the actual engine", "litellm" in content.lower())
    check("documents the approval-to-DM flow", "approval_recipient_id" in content)
    check("documents the member-lookup Cyrillic/Latin folding", "Cyrillic" in content)
    check("documents the extraction/conversation task mode", "response_mode" in content)
    check("the real chat_members schema is described, not a stale 4-table one",
          "chat_members" in content and "recent_visuals" in content)


def test_env_example_documents_main_account_id():
    with open(".env.example", encoding="utf-8") as f:
        content = f.read()
    check("MAIN_ACCOUNT_ID is documented — it's load-bearing for approvals since Phase 9",
          "MAIN_ACCOUNT_ID" in content)


def test_setmain_command_name_is_correct_everywhere():
    # An actual bug caught during this pass: config.py and bot.py both told the
    # operator to run "/setmain", but the real registered command is
    # /set_main_account. The wrong hint would have sent someone typing a
    # command that doesn't exist.
    config_src = test_support.read_code("config.py")
    bot_src = test_support.read_code("bot.py")
    check("config.py's startup warning names the real command",
          "/set_main_account" in config_src)
    check("...and does not still say the wrong one",
          "/setmain" not in config_src)
    check("bot.py's /status output names the real command",
          "/set_main_account" in bot_src)
    check("...and does not still say the wrong one",
          "/setmain" not in bot_src)


def test_start_command_is_advertised_to_botfather():
    src = test_support.read_code("bot.py")
    start_idx = src.index("private_commands = [")
    end_idx = src.index("]", start_idx)
    window = src[start_idx:end_idx]
    check("/start is in the private-chat BotFather menu (it wasn't before)",
          'command="start"' in window)


# ── /help actually lists what the bot can do ─────────────────────────────────
def test_help_lists_every_previously_missing_command():
    import asyncio
    import bot as bot_module

    class _FakeChat:
        def __init__(self, chat_type):
            self.type = chat_type

    class _FakeReply:
        def __init__(self, chat_type):
            self.chat = _FakeChat(chat_type)
            self.sent = None

        async def reply(self, text):
            self.sent = text

    async def get_help_text(chat_type):
        msg = _FakeReply(chat_type)
        await bot_module.cmd_help(msg)
        return msg.sent

    dm_text = asyncio.run(get_help_text("private"))
    group_text = asyncio.run(get_help_text("supergroup"))

    # These were all real, working commands that /help simply never mentioned —
    # the actual bug behind "the commands shown in telegram aren't full".
    previously_missing = [
        "/new", "/sessions", "/switch_session", "/compress", "/stop",
        "/install_skill", "/enable_skill", "/disable_skill", "/uninstall_skill",
        "/stop_all",
    ]
    for cmd in previously_missing:
        check(f"/help (dm) now mentions {cmd}", cmd in dm_text)

    group_only = ["/ban", "/unban", "/mute", "/unmute", "/promote", "/demote", "/set_title"]
    for cmd in group_only:
        check(f"/help (group) mentions {cmd}", cmd in group_text)
        check(f"/help (dm) correctly OMITS the group-only {cmd}", cmd not in dm_text)

    check("group /help flags which actions need an approval tap",
          "needs approval" in group_text or "needs an approval" in group_text)
    check("dm /help under Telegram's 4096-char message limit",
          len(dm_text) < 4096)
    check("group /help under Telegram's 4096-char message limit",
          len(group_text) < 4096)


# ── slash-command moderation goes through the same approval gate as the LLM ──
def test_moderation_slash_commands_route_through_approval_gate():
    src = test_support.read_code("bot.py")
    for fn, action in (
        ("cmd_ban", "kick_ban"), ("cmd_unban", "unban"),
        ("cmd_mute", "mute"), ("cmd_promote", "promote_admin"),
        ("cmd_demote", "demote_admin"),
    ):
        start = src.index(f"async def {fn}(")
        end = src.index("\n\n@router.message", start) if "\n\n@router.message" in src[start:] else start + 900
        window = src[start:start + 900]
        check(f"{fn} calls run_moderation_command (not group_tools directly) "
              f"so a slash command can't skip the {action} approval tap",
              "agent.run_moderation_command" in window)
        check(f"{fn} passes the '{action}' action",
              f'"{action}"' in window)

    # unmute/set_title are cosmetic — never approval-required — so they're
    # allowed to keep calling group_tools directly.
    unmute_start = src.index("async def cmd_unmute(")
    unmute_window = src[unmute_start:unmute_start + 700]
    check("cmd_unmute still calls group_tools directly (cosmetic, no approval needed)",
          "group_tools.unmute_member" in unmute_window)


def test_run_moderation_command_gates_by_action():
    import asyncio
    import agent

    class _FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            return None

    # A destructive action with no bot_instance-based approval reachable
    # (no MAIN_ACCOUNT_ID configured) must deny rather than silently execute.
    import config
    original_main, original_allowed = config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS
    config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS = None, []
    try:
        result = asyncio.run(agent.run_moderation_command(
            _FakeBot(), -100, "kick_ban", {"target_user_id": 5},
        ))
    finally:
        config.MAIN_ACCOUNT_ID, config.ALLOWED_DM_USER_IDS = original_main, original_allowed

    check("kick_ban via run_moderation_command is denied with nowhere to ask",
          "not approved" in result)

    # A cosmetic action runs straight through with no approval machinery at all.
    result2 = asyncio.run(agent.run_moderation_command(
        None, -100, "unmute", {"target_user_id": 5},
    ))
    check("unmute via run_moderation_command runs immediately (no bot_instance needed to deny)",
          "not approved" not in result2)


def main():
    print("phase 11 — docs, commands, test harness\n")
    for fn in (
        test_dead_debug_stub_removed,
        test_consolidated_runner_exists,
        test_readme_reflects_current_architecture,
        test_env_example_documents_main_account_id,
        test_setmain_command_name_is_correct_everywhere,
        test_start_command_is_advertised_to_botfather,
        test_help_lists_every_previously_missing_command,
        test_moderation_slash_commands_route_through_approval_gate,
        test_run_moderation_command_gates_by_action,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 11 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 11 checks passed")


if __name__ == "__main__":
    main()
