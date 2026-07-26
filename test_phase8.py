"""
Phase 8 smoke checks — member lookup that actually works.

    python3 test_phase8.py
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


# ── db.fold_name: Cyrillic <-> Latin folding ─────────────────────────────────
def test_fold_name_basic():
    import db

    check("lowercases", db.fold_name("ALEX") == "alex")
    check("strips punctuation", db.fold_name("bob-smith!") == "bob smith")
    check("collapses whitespace", db.fold_name("bob   smith") == "bob smith")
    check("empty string folds to empty", db.fold_name("") == "")
    check("None-ish falsy input folds to empty", db.fold_name(None) == "")


def test_fold_name_cyrillic_matches_latin():
    import db

    check("Russian Cyrillic 'азиз' folds toward the Latin spelling",
          db.fold_name("азиз") == db.fold_name("aziz"))
    check("Cyrillic full name folds to match its Latin transliteration",
          db.fold_name("Азиз Каримов") == db.fold_name("Aziz Karimov"))
    check("Uzbek-specific letter ў folds to u",
          "u" in db.fold_name("ўзбек"))
    check("Uzbek-specific letter қ folds to q",
          "q" in db.fold_name("қодир"))
    check("Uzbek-specific letter ғ folds to g",
          "g" in db.fold_name("Ғани"))
    check("Uzbek-specific letter ҳ folds to h",
          "h" in db.fold_name("Ҳасан"))


# ── db._member_match_score: ranking without a database ───────────────────────
def test_member_match_score_ranking():
    import db

    q = db.fold_name("aziz")
    tokens = [q]

    exact = {"username": "aziz", "full_name": None}
    prefix = {"username": "azizbek", "full_name": None}
    contains = {"username": None, "full_name": "some aziz guy"}
    no_match = {"username": "bob", "full_name": "Bob Smith"}

    s_exact = db._member_match_score(exact, q, tokens)
    s_prefix = db._member_match_score(prefix, q, tokens)
    s_contains = db._member_match_score(contains, q, tokens)
    s_none = db._member_match_score(no_match, q, tokens)

    check("an exact match outranks a prefix match", s_exact > s_prefix)
    check("a prefix match outranks a mid-string contains match", s_prefix > s_contains)
    check("a contains match outranks no match at all", s_contains > s_none)
    check("no match scores as all zeros", s_none == (0, 0, 0))


def test_member_match_score_surname_first_and_script():
    import db

    row = {"username": None, "full_name": "Aziz Karimov"}

    q1 = db.fold_name("karimov aziz")
    tokens1 = [t for t in q1.split(" ") if t]
    check("surname-first query still hits both tokens",
          db._member_match_score(row, q1, tokens1)[2] == 2)

    q2 = db.fold_name("каримов азиз")  # same word order as q1, Cyrillic script
    tokens2 = [t for t in q2.split(" ") if t]
    check("a Cyrillic-typed query scores the same Latin-stored row",
          db._member_match_score(row, q2, tokens2) == db._member_match_score(row, q1, tokens1))


# ── agent._member_search_own_scope_ok: the privilege split, in isolation ────
def test_own_scope_predicate():
    import agent

    check("a real query with no target_chat_id is allowed (defaults to own chat)",
          agent._member_search_own_scope_ok(-100, None, "bob"))
    check("a real query explicitly matching the caller's own chat is allowed",
          agent._member_search_own_scope_ok(-100, -100, "bob"))
    check("an empty query is refused even in the caller's own chat",
          not agent._member_search_own_scope_ok(-100, None, ""))
    check("a different chat's roster is refused",
          not agent._member_search_own_scope_ok(-100, -200, "bob"))
    check("whitespace-only query is refused",
          not agent._member_search_own_scope_ok(-100, None, "   "))


def test_dispatch_uses_the_predicate_and_privileged_bypasses_it():
    src = test_support.read_code("agent.py")
    start = src.index('elif tool_name == "search_group_members"')
    end = src.index('elif tool_name == "image_generate"')
    window = src[start:end]
    check("privileged requesters skip the scope predicate entirely",
          "if requester_is_privileged" in window)
    check("non-privileged requesters go through _member_search_own_scope_ok",
          "_member_search_own_scope_ok" in window)
    check("the non-privileged path force-overrides target_chat_id to the caller's own chat",
          "scoped_args = dict(args, target_chat_id=chat_id)" in window)

    import agent
    check("search_group_members is no longer wholesale-privileged",
          "search_group_members" not in agent._PRIVILEGED_TOOLS)


# ── group_tools.list_admins ──────────────────────────────────────────────────
class _FakeAdminUser:
    def __init__(self, id, username, full_name, is_bot=False):
        self.id = id
        self.username = username
        self.full_name = full_name
        self.is_bot = is_bot


class _FakeChatMember:
    def __init__(self, user, status, custom_title=None):
        self.user = user
        self.status = status
        self.custom_title = custom_title


class _FakeBotOk:
    async def get_chat_administrators(self, chat_id):
        return [
            _FakeChatMember(_FakeAdminUser(1, "owner", "The Owner"), "creator"),
            _FakeChatMember(_FakeAdminUser(2, "mod", "A Mod"), "administrator", custom_title="Mod"),
        ]

    async def get_chat_member_count(self, chat_id):
        return 42


class _FakeBotBroken:
    async def get_chat_administrators(self, chat_id):
        raise RuntimeError("Telegram says no")


def test_list_admins_happy_path():
    import group_tools

    result = asyncio.run(group_tools.list_admins(_FakeBotOk(), -100))
    check("reports ok=True", result["ok"] is True)
    check("returns both admins", len(result["admins"]) == 2)
    check("the creator's status is preserved", result["admins"][0]["status"] == "creator")
    check("a custom title is carried through", result["admins"][1]["custom_title"] == "Mod")
    check("member count comes through", result["member_count"] == 42)


def test_list_admins_fails_closed():
    import group_tools

    result = asyncio.run(group_tools.list_admins(_FakeBotBroken(), -100))
    check("a Telegram failure reports ok=False, not a crash", result["ok"] is False)
    check("...with an empty admin list", result["admins"] == [])


# ── agent._run_member_search: live admin merge ───────────────────────────────
def test_run_member_search_merges_live_admins():
    import agent
    import cache

    tracked = []
    original_track_user = cache.track_user
    original_search_users = cache.search_users
    cache.track_user = lambda *a, **k: tracked.append((a, k))

    async def fake_search_users(chat_id=None, query="", limit=25):
        return []

    cache.search_users = fake_search_users
    try:
        out = asyncio.run(agent._run_member_search(
            _FakeBotOk(), -100, {"query": "mod", "target_chat_id": -100}
        ))
    finally:
        cache.track_user = original_track_user
        cache.search_users = original_search_users

    check("the live admin roster is included in the reply",
          "Live admin roster" in out)
    check("the creator is tagged as such", "[creator]" in out)
    check("a titled admin shows their custom title", "Mod" in out)
    check("the live member count is reported", "total members: 42" in out)
    check("every admin found live gets upserted back into the member table",
          len(tracked) == 2)
    check("...marked as a confirmed admin, not a guess",
          all(k.get("is_admin") is True for _, k in tracked))


def test_run_member_search_no_bot_instance_skips_live_lookup():
    import agent
    import cache

    original_search_users = cache.search_users

    async def fake_search_users(chat_id=None, query="", limit=25):
        return []

    cache.search_users = fake_search_users
    try:
        out = asyncio.run(agent._run_member_search(None, -100, {"query": "ghost"}))
    finally:
        cache.search_users = original_search_users

    check("with no bot_instance, there's no live admin section",
          "Live admin roster" not in out)
    check("...just the honest empty-result explanation",
          "only knows people it has seen" in out)


# ── bot.py: chat_member / my_chat_member / chat_join_request handlers ───────
def test_chat_member_handlers_registered():
    src = test_support.read_code("bot.py")
    check("a chat_member handler is registered (drives allowed_updates)",
          "@router.chat_member()" in src)
    check("a my_chat_member handler is registered",
          "@router.my_chat_member()" in src)
    check("a chat_join_request handler is registered",
          "@router.chat_join_request()" in src)
    check("leaving/kicked marks the member left instead of deleting them",
          "cache.mark_member_left" in src)
    check("joins/promotions upsert with a confirmed is_admin flag",
          "is_admin=is_admin" in src)


def test_chat_member_handler_logic():
    import bot as bot_module
    import cache

    tracked = []
    left = []
    original_track_user = cache.track_user
    original_mark_left = cache.mark_member_left
    cache.track_user = lambda *a, **k: tracked.append((a, k))
    cache.mark_member_left = lambda *a, **k: left.append((a, k))

    class FakeUser:
        def __init__(self, id, username, full_name, is_bot=False):
            self.id, self.username, self.full_name, self.is_bot = id, username, full_name, is_bot

    class FakeMember:
        def __init__(self, user, status):
            self.user, self.status = user, status

    class FakeChat:
        def __init__(self, id, type="supergroup"):
            self.id, self.type = id, type

    class FakeUpdate:
        def __init__(self, chat, new_chat_member):
            self.chat, self.new_chat_member = chat, new_chat_member

    try:
        join_update = FakeUpdate(FakeChat(-100), FakeMember(FakeUser(7, "newbie", "New Bie"), "member"))
        asyncio.run(bot_module.handle_chat_member_update(join_update))
        check("a join upserts the member", len(tracked) == 1)
        check("a plain member is not marked admin",
              tracked[0][1].get("is_admin") is False)

        promo_update = FakeUpdate(FakeChat(-100), FakeMember(FakeUser(7, "newbie", "New Bie"), "administrator"))
        asyncio.run(bot_module.handle_chat_member_update(promo_update))
        check("a promotion upserts with is_admin=True", tracked[1][1].get("is_admin") is True)

        leave_update = FakeUpdate(FakeChat(-100), FakeMember(FakeUser(7, "newbie", "New Bie"), "left"))
        asyncio.run(bot_module.handle_chat_member_update(leave_update))
        check("leaving calls mark_member_left, not a delete", len(left) == 1)

        dm_update = FakeUpdate(FakeChat(555, type="private"), FakeMember(FakeUser(7, "x", "X"), "member"))
        asyncio.run(bot_module.handle_chat_member_update(dm_update))
        check("private-chat member updates are ignored (not a group)",
              len(tracked) == 2)
    finally:
        cache.track_user = original_track_user
        cache.mark_member_left = original_mark_left


# ── skills ────────────────────────────────────────────────────────────────
def test_telegram_directory_skill_exists():
    import os

    check("the old find-telegram-id directory is gone",
          not os.path.isdir("skills/find-telegram-id"))
    check("telegram-directory replaces it",
          os.path.isfile("skills/telegram-directory/SKILL.md"))
    with open("skills/telegram-directory/SKILL.md", encoding="utf-8") as f:
        content = f.read()
    check("it states plainly that listing every member is impossible",
          "no method to list every member" in content)
    check("it documents the live-admin-list capability",
          "getChatAdministrators" in content)
    check("it documents the open-to-everyone own-chat scoping rule",
          "open to anyone" in content)


def test_bot_architecture_claim_updated():
    with open("skills/bot-architecture/SKILL.md", encoding="utf-8") as f:
        content = f.read()
    check("the stale 'only from messages' framing is gone",
          "populated from every incoming message (commands and DMs included). The DATABASE"
          not in content)
    check("it now also documents chat_member-driven tracking",
          "chat_member` updates" in content)
    check("...and the live admin lookup",
          "getChatAdministrators" in content)


def main():
    print("phase 8 — member lookup that actually works\n")
    for fn in (
        test_fold_name_basic,
        test_fold_name_cyrillic_matches_latin,
        test_member_match_score_ranking,
        test_member_match_score_surname_first_and_script,
        test_own_scope_predicate,
        test_dispatch_uses_the_predicate_and_privileged_bypasses_it,
        test_list_admins_happy_path,
        test_list_admins_fails_closed,
        test_run_member_search_merges_live_admins,
        test_run_member_search_no_bot_instance_skips_live_lookup,
        test_chat_member_handlers_registered,
        test_chat_member_handler_logic,
        test_telegram_directory_skill_exists,
        test_bot_architecture_claim_updated,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 8 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 8 checks passed")


if __name__ == "__main__":
    main()
