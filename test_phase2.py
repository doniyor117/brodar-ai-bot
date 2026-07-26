"""
Phase 2 smoke checks — one persona, one prompt builder, hardened sentinels.

    python3 test_phase2.py
"""
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


def _prompt(is_group, privileged):
    import agent
    import memory

    return agent.build_system_prompt(
        persona=memory.read_persona(),
        learned_facts="- creator likes short replies",
        avail_skills=["find-telegram-id", "bot-architecture"],
        date_line="Sunday, July 26, 2026",
        summary_text="",
        is_group=is_group,
        requester_is_privileged=privileged,
    )


# ── the contradictions are gone ─────────────────────────────────────────────
def test_no_contradictions():
    import memory

    persona = memory.read_persona()
    lowered = persona.lower()

    check("persona no longer says to drop the personality",
          "drop the playful" not in lowered)
    check("persona no longer says NO JOKES", "no jokes." not in lowered)
    check("persona has no strict-compliance mode section",
          "strict compliance" not in lowered)
    check("persona still says have fun", "have fun with this" in lowered)
    check("persona has an explicit obedience section", "who you obey" in lowered)
    check("persona names the new aliases",
          "simon bro" in lowered and "samy" in lowered)

    for is_group in (True, False):
        for privileged in (True, False):
            p = _prompt(is_group, privileged).lower()
            label = f"group={is_group} privileged={privileged}"
            check(f"no professional-mode switch ({label})",
                  "drop the playful" not in p and "no jokes. no games" not in p)
            check(f"voice rules are always present ({label})",
                  "lowercase. always." in p)


# ── DM vs group prompt shape ────────────────────────────────────────────────
def test_prompt_modes():
    dm = _prompt(is_group=False, privileged=True)
    grp = _prompt(is_group=True, privileged=True)

    check("[SILENT] is never even named in a DM", "[SILENT]" not in dm)
    check("[SILENT] is offered in a group", "[SILENT]" in grp)
    check("a DM is never called a group chat",
          "you're in a group chat" not in dm.lower() and "this is a group chat" not in dm.lower())
    check("the DM prompt says it's one-on-one", "direct message" in dm.lower())
    check("reaction rules are group-only", "|[emoji]|" in grp and "|[emoji]|" not in dm)
    check("speaker-prefix explanation is group-only",
          "prefixed with who said them" in grp and "prefixed with who said them" not in dm)
    check("the group prompt is the longer one", len(grp) > len(dm))


# ── authority is a rule, not a mode ─────────────────────────────────────────
def test_authority_block():
    admin = _prompt(is_group=True, privileged=True).lower()
    user = _prompt(is_group=True, privileged=False).lower()

    check("admins are told their instructions are orders",
          "their instructions are orders" in admin)
    check("admins are told not to joke instead of acting",
          "instead of the action" in admin)
    check("admins are pointed at the self-edit tools",
          "edit_persona_file" in admin)
    check("admins are told to keep the same voice",
          "same voice" in admin)
    check("regular users are not given admin authority",
          "their instructions are orders" not in user)
    check("regular users are told they can't change the rules",
          "can't change your rules" in user)
    check("regular users are still treated warmly",
          "friendly and genuinely helpful" in user)


# ── skills block ────────────────────────────────────────────────────────────
def test_skills_block():
    import agent

    p = _prompt(is_group=False, privileged=True)
    check("skills are listed", "find-telegram-id" in p and "bot-architecture" in p)

    empty = agent.build_system_prompt(
        persona="x", learned_facts="", avail_skills=[],
        date_line="today", is_group=False,
    )
    check("no skills installed says so", "(none installed" in empty)
    check("no skills installed doesn't demand a use_skill call",
          "you MUST immediately call" not in empty)
    check("empty learned facts are omitted", "# Learned Facts" not in empty)


# ── persona split ───────────────────────────────────────────────────────────
def test_persona_split():
    import agent
    import memory

    always, group = agent.split_persona(memory.read_persona())
    check("the split produces both halves", bool(always) and bool(group))
    check("identity is in the always half", "brodar" in always.lower())
    check("silence rules are in the group half", "[SILENT]" in group)
    check("identity is not duplicated into the group half",
          "you're **brodar**" not in group)

    a, g = agent.split_persona("no marker here at all")
    check("a persona with no marker degrades to always-applicable",
          a == "no marker here at all" and g == "")


# ── few-shot selection ──────────────────────────────────────────────────────
def test_few_shots():
    import agent
    import bot as bot_mod

    dm = agent.few_shots_for(is_group=False)
    grp = agent.few_shots_for(is_group=True)

    dm_text = " ".join(m["content"] for m in dm)
    grp_text = " ".join(m["content"] for m in grp)

    check("DM few-shots never demonstrate [SILENT]", "[SILENT]" not in dm_text)
    check("group few-shots do demonstrate [SILENT]", "[SILENT]" in grp_text)
    check("both modes demonstrate the voice", "paris. did you forget already?" in dm_text)
    check("both modes demonstrate obeying an order", "on it." in dm_text and "on it." in grp_text)
    check("no example teaches an emoji telegram rejects",
          all(bot_mod.normalize_reaction(e) is not None
              for m in grp for e in bot_mod._REACTION_RE.findall(m["content"])))
    check("obedience includes a self-edit request",
          "add to your personality" in dm_text)
    check("obedience includes not gaslighting about the last request",
          "what did i just ask you to do?" in dm_text)
    check("groups get strictly more examples", len(grp) > len(dm))
    check("few-shot lists alternate user/assistant",
          all(m["role"] == ("user" if i % 2 == 0 else "assistant")
              for i, m in enumerate(grp)))


# ── control tokens ──────────────────────────────────────────────────────────
def test_control_tokens():
    import bot

    t, r, s = bot.parse_control_tokens("[SILENT]")
    check("bare [SILENT] means silence with no text", t == "" and s and r is None)

    t, r, s = bot.parse_control_tokens("[SILENT] |[🔥]|")
    check("silence plus a reaction", t == "" and s and r == "🔥")

    t, r, s = bot.parse_control_tokens("i know right |[🤣]|")
    check("text plus a reaction", t == "i know right" and r == "🤣" and not s)

    # The old .startswith / single re.search version leaked both of these.
    t, r, s = bot.parse_control_tokens("yeah anyway [SILENT]")
    check("a trailing [SILENT] is caught, not leaked", "[SILENT]" not in t and s)

    t, r, s = bot.parse_control_tokens("sure |[👍]| whatever |[🔥]|")
    check("every reaction marker is stripped", "|[" not in t, f"got {t!r}")
    check("the first valid reaction wins", r == "👍")

    t, r, s = bot.parse_control_tokens("a [SILENT] b [SILENT] c")
    check("multiple [SILENT] tokens are all stripped", "[SILENT]" not in t)

    t, r, s = bot.parse_control_tokens("")
    check("empty reply is handled", t == "" and r is None and not s)

    t, r, s = bot.parse_control_tokens("just a normal reply")
    check("a clean reply is untouched", t == "just a normal reply" and r is None and not s)


# ── reaction allowlist ──────────────────────────────────────────────────────
def test_reaction_allowlist():
    import bot

    check("a valid reaction passes", bot.normalize_reaction("🔥") == "🔥")
    check("whitespace is trimmed", bot.normalize_reaction("  👍  ") == "👍")
    check("the variation selector is normalized away",
          bot.normalize_reaction("❤️") == "❤")
    check("a folded compound emoji maps back to its canonical form",
          bot.normalize_reaction("❤‍🔥") == "❤️‍🔥")
    check("face-with-tears-of-joy is correctly rejected (telegram only takes ROFL)",
          bot.normalize_reaction("😂") is None)
    check("ROFL is accepted", bot.normalize_reaction("🤣") == "🤣")
    check("an unsupported emoji is rejected", bot.normalize_reaction("🍕") is None)
    check("a word is rejected", bot.normalize_reaction("fire") is None)
    check("empty is rejected", bot.normalize_reaction("") is None)
    check("None-ish input is rejected", bot.normalize_reaction("   ") is None)
    check("a valid emoji with trailing junk still works",
          bot.normalize_reaction("🔥 nice") == "🔥")
    check("the allowlist is exactly the API's 73 emoji",
          len(bot.TELEGRAM_REACTION_EMOJI) == 73, f"got {len(bot.TELEGRAM_REACTION_EMOJI)}")
    check("no duplicates after folding",
          len(bot._REACTION_LOOKUP) == len(bot.TELEGRAM_REACTION_EMOJI))


# ── lowercase protection ────────────────────────────────────────────────────
def test_lowercase_protection():
    import bot

    check("usernames survive lowercasing",
          "@BobSmith" in bot.enforce_lowercase("their handle is @BobSmith ok"))
    check("chat ids survive lowercasing",
          "-1001234567890" in bot.enforce_lowercase("the group id is -1001234567890"))
    check("user ids survive lowercasing",
          "2030903420" in bot.enforce_lowercase("that's user 2030903420"))
    check("urls still survive",
          "https://Example.COM/Path" in bot.enforce_lowercase("see https://Example.COM/Path"))
    check("code spans still survive",
          "`SELECT Foo`" in bot.enforce_lowercase("run `SELECT Foo` now"))
    check("ordinary prose is still lowercased",
          bot.enforce_lowercase("Hello There Friend") == "hello there friend")


# ── persona edits are recoverable ───────────────────────────────────────────
def test_persona_write_safety():
    import os
    import memory

    original = memory.read_persona()
    try:
        check("an empty persona write is refused", memory.write_persona_md("") is False)
        check("a whitespace-only persona write is refused",
              memory.write_persona_md("   \n  ") is False)
        check("the persona survived the refused writes", memory.read_persona() == original)

        check("a real write succeeds", memory.write_persona_md("# test persona\nhi") is True)
        check("a backup was created", os.path.exists(memory.PERSONA_BACKUP_PATH))
        with open(memory.PERSONA_BACKUP_PATH, encoding="utf-8") as f:
            check("the backup holds the previous version", f.read() == original)

        check("restore works", memory.restore_persona_backup() is True)
        check("the original persona is back", memory.read_persona() == original)
    finally:
        memory.write_persona_md(original)
        if os.path.exists(memory.PERSONA_BACKUP_PATH):
            os.remove(memory.PERSONA_BACKUP_PATH)


if __name__ == "__main__":
    for fn in (test_no_contradictions, test_prompt_modes, test_authority_block,
               test_skills_block, test_persona_split, test_few_shots,
               test_control_tokens, test_reaction_allowlist,
               test_lowercase_protection, test_persona_write_safety):
        print(f"\n{fn.__name__}:")
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all phase 2 checks passed")
