"""
Phase 4 smoke checks — group admin, member lookup, approvals.

    python3 test_phase4.py
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


# ── duration validation: the "mute went rogue" bug ──────────────────────────
def test_duration_validation():
    import group_tools as gt

    check("a 10-second mute is refused, not silently made permanent",
          gt.validate_duration(10, "mute") is not None)
    check("...and the message explains why",
          "PERMANENT" in gt.validate_duration(10, "mute"))
    check("30s is still under Telegram's threshold",
          gt.validate_duration(30, "mute") is not None)
    check("31s is accepted", gt.validate_duration(31, "mute") is None)
    check("10 minutes is accepted", gt.validate_duration(600, "mute") is None)
    check("0 means a deliberate permanent action", gt.validate_duration(0, "ban") is None)
    check("over 366 days is refused",
          gt.validate_duration(400 * 24 * 3600, "ban") is not None)
    check("exactly 366 days is accepted",
          gt.validate_duration(366 * 24 * 3600, "ban") is None)


def test_until_is_utc_aware():
    from datetime import datetime, timezone
    import group_tools as gt

    check("permanent restrictions have no expiry", gt._until(0) is None)
    check("negative durations are permanent", gt._until(-5) is None)

    until = gt._until(600)
    check("the expiry is timezone-aware", until.tzinfo is not None)
    check("...and it is UTC", until.utcoffset().total_seconds() == 0)

    delta = (until - datetime.now(timezone.utc)).total_seconds()
    check("the expiry is 10 minutes out regardless of host timezone",
          595 < delta < 605, f"got {delta:.0f}s")


def test_duration_formatting():
    import group_tools as gt

    check("0 reads as permanent", gt._fmt_duration(0) == "permanently")
    check("seconds read as seconds", gt._fmt_duration(45) == "for 45 seconds")
    check("minutes read as minutes", gt._fmt_duration(600) == "for 10 minutes")
    check("hours read as hours", gt._fmt_duration(7200) == "for 2 hours")
    check("days read as days", gt._fmt_duration(172800) == "for 2 days")


# ── moderation argument validation ──────────────────────────────────────────
def test_moderation_validation():
    import agent

    class FakeBot:
        pass

    def run(args):
        return asyncio.run(agent._run_moderation(FakeBot(), -100123, args))

    r = run({"action": "kick_ban"})
    check("banning with no target is refused before calling Telegram",
          "needs a real target_user_id" in r)
    check("...and the model is told to look them up first",
          "search_group_members" in r)
    check("...and told explicitly not to claim success",
          "NOT report this action as done" in r)

    check("target_user_id 0 is refused",
          "needs a real target_user_id" in run({"action": "mute", "target_user_id": 0}))
    check("a negative target_user_id is refused",
          "needs a real target_user_id" in run({"action": "mute", "target_user_id": -1}))

    check("pinning with no message_id is refused",
          "needs a message_id" in run({"action": "pin_message"}))
    check("set_title with no text is refused",
          "needs text_param" in run({"action": "set_title"}))
    check("an unknown action lists the valid ones",
          "Valid actions" in run({"action": "teleport", "target_user_id": 5}))
    check("no bot instance is reported, not crashed",
          "isn't available" in asyncio.run(agent._run_moderation(None, 1, {"action": "mute"})))


def test_action_aliases():
    import agent

    check("'ban' still maps to kick_ban", agent._ACTION_ALIASES["ban"] == "kick_ban")
    check("'silence' maps to mute, not a ban", agent._ACTION_ALIASES["silence"] == "mute")
    check("'restrict' maps to mute", agent._ACTION_ALIASES["restrict"] == "mute")
    check("'kick' maps to kick_ban", agent._ACTION_ALIASES["kick"] == "kick_ban")
    check("'unpin' maps to unpin_message", agent._ACTION_ALIASES["unpin"] == "unpin_message")

    check("every alias target is a real action",
          all(v in agent._ACTIONS_NEEDING_USER | agent._ACTIONS_NEEDING_MESSAGE
              | {"set_title", "set_description", "unpin_message"}
              for v in agent._ACTION_ALIASES.values()))

    schema = next(t for t in agent.TOOLS_SCHEMA
                  if t["function"]["name"] == "group_moderation_tool")
    enum = schema["function"]["parameters"]["properties"]["action"]["enum"]
    check("the schema uses the unambiguous kick_ban name", "kick_ban" in enum)
    check("the ambiguous bare 'ban' is gone from the schema", "ban" not in enum)
    check("unpin_message is now reachable", "unpin_message" in enum)
    check("pin_message has its own message_id parameter",
          "message_id" in schema["function"]["parameters"]["properties"])


def test_safe_int():
    import agent

    check("an int passes through", agent._safe_int(5) == 5)
    check("a numeric string is converted", agent._safe_int("42") == 42)
    check("None yields the default", agent._safe_int(None, 7) == 7)
    check("garbage yields the default", agent._safe_int("abc", 7) == 7)
    check("None with no default is None", agent._safe_int(None) is None)
    check("negatives are preserved", agent._safe_int("-100123") == -100123)


# ── member lookup ───────────────────────────────────────────────────────────
def test_member_search_output():
    import agent
    import cache

    rows = [
        {"chat_id": -100123, "user_id": 555, "username": "bobsmith",
         "full_name": "Bob Smith", "is_bot": False},
        {"chat_id": -100999, "user_id": 555, "username": "bobsmith",
         "full_name": "Bob Smith", "is_bot": False},
    ]

    async def fake_search(chat_id=None, query="", limit=25):
        return rows

    original = cache.search_users
    cache.search_users = fake_search
    try:
        out = asyncio.run(agent._run_member_search(-100123, {"query": "bob"}))
    finally:
        cache.search_users = original

    check("the user id is an explicit labelled field", "user_id: 555" in out)
    check("the chat id is an explicit labelled field", "chat_id: -100123" in out)
    check("the same person in two groups is listed twice, not collapsed",
          out.count("user_id: 555") == 2)
    check("the second group is distinguishable", "chat_id: -100999" in out)
    check("the username is included", "@bobsmith" in out)

    async def no_results(chat_id=None, query="", limit=25):
        return []

    cache.search_users = no_results
    try:
        out = asyncio.run(agent._run_member_search(None, {"query": "ghost"}))
    finally:
        cache.search_users = original

    check("an empty result explains the limitation", "only knows people it has seen" in out)
    check("...and suggests a way forward", "forward" in out)


def test_member_tracking():
    bot_src = open("bot.py").read()
    check("tracking happens in the access middleware, before any gate",
          "cache.track_user(" in bot_src.split("# 1. Private Chat Check")[0])
    check("the username is recorded, not just the display name",
          "username=u.username" in bot_src)
    check("bot accounts are flagged", "is_bot=bool(u.is_bot)" in bot_src)

    cache_src = open("cache.py").read()
    check("track_user writes through to the database",
          "db.upsert_chat_member" in cache_src)
    check("search_users is async now (it hits the DB)",
          "async def search_users" in cache_src)
    check("there is an in-process fallback if the DB is down",
          "_member_cache" in cache_src)
    check("the in-process cache is bounded", "_MEMBER_CACHE_CHAT_LIMIT" in cache_src)

    db_src = open("db.py").read()
    check("chat_members is a real table", "CREATE TABLE IF NOT EXISTS chat_members" in db_src)
    check("it is keyed per (chat, user)", "PRIMARY KEY (chat_id, user_id)" in db_src)
    check("upserts don't erase a known username with a null",
          "COALESCE(EXCLUDED.username" in db_src)
    check("username lookups are indexed", "lower(username)" in db_src)


# ── approvals: exactly one system ───────────────────────────────────────────
def test_single_approval_system():
    import os

    check("the dead duplicate approval module is gone",
          not os.path.exists("permissions.py"))

    bot_src = open("bot.py").read()
    check("nothing imports it any more", "import permissions" not in bot_src)
    check("its unreachable callback handler is gone", "PermCallback" not in bot_src)
    check("the live approval handler survives", "async def handle_approval" in bot_src)
    check("and it is authorized", "is_user_privileged(callback.message" in bot_src)

    import agent
    check("moderation is not gated behind an approval tap",
          "group_moderation_tool" not in agent._APPROVAL_REQUIRED_TOOLS)
    check("but it is still admin-only",
          "group_moderation_tool" in agent._PRIVILEGED_TOOLS)


# ── commands ────────────────────────────────────────────────────────────────
def test_unknown_commands():
    import bot
    from test_support import FakeMessage

    check("an unknown command is detected",
          bot.unknown_command(FakeMessage("/dance"), "BrodarAIBot") == "dance")
    check("a known command is not flagged",
          bot.unknown_command(FakeMessage("/help"), "BrodarAIBot") is None)
    check("a known command with a bot suffix is not flagged",
          bot.unknown_command(FakeMessage("/help@BrodarAIBot"), "BrodarAIBot") is None)
    check("another bot's command is left alone",
          bot.unknown_command(FakeMessage("/dance@OtherBot"), "BrodarAIBot") is None)
    check("an unknown command addressed to us IS flagged",
          bot.unknown_command(FakeMessage("/dance@BrodarAIBot"), "BrodarAIBot") == "dance")
    check("ordinary chat is not a command",
          bot.unknown_command(FakeMessage("hey what's up"), "BrodarAIBot") is None)
    check("a bare slash is not a command",
          bot.unknown_command(FakeMessage("/"), "BrodarAIBot") is None)
    check("a mid-sentence slash is not a command",
          bot.unknown_command(FakeMessage("use and/or here"), "BrodarAIBot") is None)
    check("commands with arguments are recognised",
          bot.unknown_command(FakeMessage("/dance now please"), "BrodarAIBot") == "dance")

    # Every command advertised to users must actually exist.
    import re
    src = open("bot.py").read()
    advertised = set(re.findall(r'BotCommand\(command="([a-z_]+)"', src))
    missing = advertised - bot.KNOWN_COMMANDS
    check("every advertised command is in KNOWN_COMMANDS", not missing, f"missing: {missing}")

    registered = set(re.findall(r'Command\("([a-z_]+)"', src))
    check("KNOWN_COMMANDS matches the registered handlers",
          bot.KNOWN_COMMANDS == registered,
          f"diff: {bot.KNOWN_COMMANDS ^ registered}")


def test_public_group_commands():
    src = open("bot.py").read()
    code = test_support.read_code("bot.py")
    check("harmless commands are let through for everyone",
          "public_group_cmds" in src)
    check("/help is one of them", '"/help", "/status"' in src or "'/help'" in src)
    check("commands are matched exactly, not by prefix",
          "cmd.startswith(c)" not in code)
    check("the @botusername suffix is stripped before matching",
          'cmd.split("@", 1)[0]' in src)
    check("a blocked admin command says so instead of vanishing",
          "that one's admin-only" in src)


# ── honest failure reporting ────────────────────────────────────────────────
def test_honest_reporting():
    src = open("group_tools.py").read()
    code = test_support.read_code("group_tools.py")
    check("a failed custom title is reported, not swallowed",
          "could not be set" in src)
    check("the bare except:pass around it is gone",
          "except Exception:\n            pass" not in src)
    check("every mutating call reports its own failure",
          src.count("Failed to") >= 8)
    check("naive local time is gone", "datetime.now ( )" not in code
          and "datetime.now()" not in code)
    check("expiries are computed in UTC", "timezone.utc" in code)


if __name__ == "__main__":
    for fn in (test_duration_validation, test_until_is_utc_aware,
               test_duration_formatting, test_moderation_validation,
               test_action_aliases, test_safe_int, test_member_search_output,
               test_member_tracking, test_single_approval_system,
               test_unknown_commands, test_public_group_commands,
               test_honest_reporting):
        print(f"\n{fn.__name__}:")
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all phase 4 checks passed")
