"""
Phase 1 smoke checks — pure-logic regressions for the freeze/security fixes.

Runs with no Telegram, no database and no API key:
    python3 test_phase1.py
"""
import os
import sys

import test_support
from test_support import FakeChat, FakeEntity, FakeMessage, FakeUser

test_support.install_stubs()

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# ── bot-loop detector ───────────────────────────────────────────────────────
def test_bot_loop():
    import bot

    # The old heuristic flagged this: _attribute() capitalizes every group
    # message, "Here's" is a marker, and >200 chars is a marker.
    human = [{"role": "user", "content": "Alex: Here's the thing, " + "x" * 300}] * 20
    check("long capitalized human chatter is not a bot loop",
          not bot._looks_like_bot_loop(human))

    bots = [{"role": "user", "content": "[BOT] Helper: beep"}] * 12
    check("12 consecutive bot messages is a bot loop", bot._looks_like_bot_loop(bots))

    check("a human reply breaks the streak",
          not bot._looks_like_bot_loop(bots[:6] + [{"role": "user", "content": "Alex: hey"}] + bots[:6]))

    with_replies = []
    for m in bots:
        with_replies.append(m)
        with_replies.append({"role": "assistant", "content": "ok"})
    check("our own replies don't break a bot streak", bot._looks_like_bot_loop(with_replies))

    check("short history is never a loop", not bot._looks_like_bot_loop(bots[:3]))


# ── mention / addressing ────────────────────────────────────────────────────
def test_addressing():
    import bot

    BOT_ID, BOT_NAME = 99, "BrodarAIBot"

    def addressed(msg):
        return bot.message_addresses_bot(msg, BOT_ID, BOT_NAME)

    check("@mention via entity",
          addressed(FakeMessage("@BrodarAIBot what do you think?",
                                entities=[FakeEntity("mention", 0, 12)])))
    check("@mention with different casing",
          addressed(FakeMessage("@brodaraibot what do you think?",
                                entities=[FakeEntity("mention", 0, 12)])))
    check("@mention when no entities are attached",
          addressed(FakeMessage("hey @BrodarAIBot")))
    check("rich text_mention entity (no @ in the text at all)",
          addressed(FakeMessage("brodar bot",
                                entities=[FakeEntity("text_mention", 0, 10,
                                                     user=FakeUser(BOT_ID, is_bot=True))])))
    check("reply to one of the bot's messages",
          addressed(FakeMessage("thanks",
                                reply_to_message=FakeMessage(
                                    "yo", from_user=FakeUser(BOT_ID, is_bot=True)))))

    check("alias 'brodar'", addressed(FakeMessage("brodar what's up")))
    check("alias 'simon bro'", addressed(FakeMessage("simon bro help me out")))
    check("alias 'samy'", addressed(FakeMessage("samy you there?")))
    check("aliases are case-insensitive", addressed(FakeMessage("SAMY you there?")))
    check("alias mid-sentence", addressed(FakeMessage("i think samy knows this one")))
    check("caption is checked as well as text",
          addressed(FakeMessage(caption="look at this brodar")))

    check("alias needs a word boundary (samyan)",
          not addressed(FakeMessage("samyan is a nice name")))
    check("alias needs a word boundary (brodark)",
          not addressed(FakeMessage("brodark is unrelated")))
    check("unrelated chatter is not addressing",
          not addressed(FakeMessage("hey everyone how's it going")))
    check("another bot's @mention is not ours",
          not addressed(FakeMessage("@SomeOtherBot hi",
                                    entities=[FakeEntity("mention", 0, 14)])))
    check("reply to a human is not addressing",
          not addressed(FakeMessage("yeah",
                                    reply_to_message=FakeMessage("hm", from_user=FakeUser(7)))))
    check("empty message is not addressing", not addressed(FakeMessage("")))


# ── send_file sandbox ───────────────────────────────────────────────────────
def test_send_file_sandbox():
    import agent
    import config

    ws = os.path.realpath(config.TOOL_WORKSPACE_DIR)
    src_tree = os.path.dirname(ws)

    check("relative path inside workspace resolves",
          agent._resolve_sendable_path("README.txt") == os.path.join(ws, "README.txt"))
    check("absolute path inside workspace resolves",
          agent._resolve_sendable_path(os.path.join(ws, "README.txt")) == os.path.join(ws, "README.txt"))
    check("nested path inside workspace resolves",
          agent._resolve_sendable_path("sub/dir/file.txt") == os.path.join(ws, "sub/dir/file.txt"))

    check("traversal to .env is blocked", agent._resolve_sendable_path("../.env") is None)
    check("absolute .env in the source tree is blocked",
          agent._resolve_sendable_path(os.path.join(src_tree, ".env")) is None)
    check("absolute path outside workspace is blocked",
          agent._resolve_sendable_path("/etc/passwd") is None)
    check("deep traversal is blocked",
          agent._resolve_sendable_path("a/../../../../etc/passwd") is None)
    check("empty path is blocked", agent._resolve_sendable_path("") is None)
    check("whitespace-only path is blocked", agent._resolve_sendable_path("   ") is None)


# ── privileged tool sets ────────────────────────────────────────────────────
def test_privileged_sets():
    import agent

    for t in ("send_file", "group_moderation_tool",
              "edit_env_file", "edit_persona_file", "install_skill_from_url"):
        check(f"'{t}' is privileged", t in agent._PRIVILEGED_TOOLS)

    # search_group_members is deliberately NOT wholesale-privileged since Phase
    # 8 — a scoped, non-empty search is open to everyone; the empty-query /
    # cross-chat cases are gated inline at the call site instead. See
    # test_phase8.py for that split.
    check("search_group_members is not wholesale-privileged (gated inline instead)",
          "search_group_members" not in agent._PRIVILEGED_TOOLS)

    check("moderation needs no approval tap (an admin already asked for it)",
          "group_moderation_tool" not in agent._APPROVAL_REQUIRED_TOOLS)
    check("env edits do need an approval tap",
          "edit_env_file" in agent._APPROVAL_REQUIRED_TOOLS)
    check("approval-required is a subset of privileged",
          agent._APPROVAL_REQUIRED_TOOLS <= agent._PRIVILEGED_TOOLS)


# ── approval fails closed ───────────────────────────────────────────────────
def test_approval_denies_on_timeout():
    import asyncio
    import agent
    import bot
    import config

    config.APPROVAL_TIMEOUT_SECONDS = 0.05

    class FakeBot:
        async def send_message(self, **kwargs):
            return None

    result = asyncio.run(
        agent._request_interactive_approval(FakeBot(), 123, "edit_env_file", {})
    )
    check("timeout returns exactly False, not a truthy string",
          result is False, f"got {result!r}")
    check("`if not approved` therefore blocks the tool", not result)
    check("the pending future is cleaned up", len(bot.pending_approvals) == 0)

    class BrokenBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("chat not found")

    check("a prompt that fails to send denies",
          asyncio.run(agent._request_interactive_approval(BrokenBot(), 123, "edit_env_file", {})) is False)
    check("no future leaks after a send failure", len(bot.pending_approvals) == 0)

    check("no bot_instance denies",
          asyncio.run(agent._request_interactive_approval(None, 123, "edit_env_file", {})) is False)


# ── timeouts are actually wired up ──────────────────────────────────────────
def test_timeouts_present():
    import agent
    import config
    import models

    kw = models.MODELS["glm-4.7-flash"].call_kwargs()
    check("acompletion is given a timeout", kw.get("timeout") == config.LLM_TIMEOUT_SECONDS)
    check("litellm's own retry loop is disabled", kw.get("num_retries") == 0)

    check("no unbounded pool.acquire() remains in db.py",
          "pool.acquire()" not in open("db.py").read())

    check("search has its own executor, not the shared default pool",
          "search" in getattr(agent._search_executor, "_thread_name_prefix", ""))
    check("the search pool is small and bounded",
          0 < agent._search_executor._max_workers <= 4)

    src = open("agent.py").read()
    check("skill installation no longer blocks the event loop",
          "asyncio.to_thread(\n                    skills.install_skill_from_url" in src
          or "asyncio.to_thread(skills.install_skill_from_url" in src)


if __name__ == "__main__":
    for fn in (test_bot_loop, test_addressing, test_send_file_sandbox,
               test_privileged_sets, test_approval_denies_on_timeout,
               test_timeouts_present):
        print(f"\n{fn.__name__}:")
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all phase 1 checks passed")
