"""
Phase 5 smoke checks — skills & tools hygiene.

    python3 test_phase5.py
"""
import asyncio
import os
import sys
import tempfile

import test_support

test_support.install_stubs()

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# ── skills: one canonical identity ──────────────────────────────────────────
def test_skill_slug_identity():
    import skills

    check("slugify normalizes spaces and case",
          skills.slugify("Brodar Bot Architecture") == "brodar-bot-architecture")
    check("slugify strips punctuation", skills.slugify("web_research!") == "web_research")
    check("slugify of nothing is empty", skills.slugify(None) == "")

    avail = skills.list_available_skills()
    check("skills are discovered at all", len(avail) > 0, f"found {len(avail)}")
    check("every entry carries a canonical slug",
          all(s.get("slug") for s in avail))
    check("every entry carries a description for the prompt",
          all(s.get("description") for s in avail))
    check("bodies are not shipped in the listing (they go in the prompt block)",
          all("body" not in s for s in avail))

    slugs = [s["slug"] for s in avail]
    if "bot-architecture" in slugs:
        # The exact case the old code got wrong: advertised under the frontmatter
        # name, loadable only under the directory name.
        check("a skill resolves by its directory slug",
              skills._resolve_slug("bot-architecture") == "bot-architecture")
        check("...and by its display name",
              skills._resolve_slug("Bot Architecture & Internals") == "bot-architecture")
        check("...and load returns a real body either way",
              len(skills.load_skill_instruction("Bot Architecture & Internals") or "") > 50)

    check("an unknown skill names the real alternatives",
          "not found" in skills.load_skill_instruction("does-not-exist").lower())


def test_skill_index_is_cached():
    import skills

    skills._invalidate_index()
    first = skills._get_index()
    second = skills._get_index()
    check("the parsed skill index is reused when nothing changed",
          first is second)


# ── loaded-skill tracking: the real fix for re-reading every turn ───────────
def test_loaded_skill_tracking():
    import agent

    agent.clear_loaded_skills(999)
    check("a fresh chat has no loaded skills", agent.get_loaded_skills(999) == [])

    agent.note_skill_loaded(999, "web_research")
    agent.note_skill_loaded(999, "web_research")
    check("loading the same skill twice records it once",
          agent.get_loaded_skills(999) == ["web_research"])

    agent.note_skill_loaded(999, "send-media")
    check("a second skill is tracked too",
          set(agent.get_loaded_skills(999)) == {"web_research", "send-media"})

    check("tracking is per chat", agent.get_loaded_skills(1000) == [])

    agent.clear_loaded_skills(999)
    check("/clear drops them", agent.get_loaded_skills(999) == [])


def test_loaded_skills_reach_the_prompt():
    import agent
    import skills

    avail = skills.list_available_skills()
    if not avail:
        return
    slug = avail[0]["slug"]

    plain = agent.build_system_prompt(
        persona="p", learned_facts="", avail_skills=avail, date_line="2026-07-26",
    )
    check("the skills block lists descriptions, not bare names",
          avail[0]["description"][:20] in plain)

    loaded = agent.build_system_prompt(
        persona="p", learned_facts="", avail_skills=avail, date_line="2026-07-26",
        loaded_skills=[slug],
    )
    check("an already-loaded skill is named as loaded",
          "ALREADY loaded" in loaded)
    body = skills.get_skill_body(slug) or ""
    check("...and its body is inlined so use_skill is genuinely unnecessary",
          bool(body) and body[:60] in loaded)
    check("the loaded prompt is strictly longer than the plain one",
          len(loaded) > len(plain))


# ── shell sandbox: cwd-relative validation ──────────────────────────────────
def test_path_validation_uses_session_cwd():
    import tools

    with tempfile.TemporaryDirectory() as ws:
        sub = os.path.join(ws, "sub")
        os.makedirs(sub)
        open(os.path.join(sub, "notes.txt"), "w").close()

        check("a file in the cwd validates when cwd is the subdir",
              tools._path_args_are_safe(["notes.txt"], ws, cwd=sub))
        check("cd .. from a subdir is allowed — it lands back in the sandbox",
              tools._path_args_are_safe([".."], ws, cwd=sub))
        check("cd .. from the ROOT is still refused",
              not tools._path_args_are_safe([".."], ws, cwd=ws))
        check("an absolute path outside the sandbox is refused",
              not tools._path_args_are_safe(["/etc/passwd"], ws, cwd=sub))
        check("../../.env is refused from any depth",
              not tools._path_args_are_safe(["../../.env"], ws, cwd=sub))
        check("flags are not treated as paths",
              tools._path_args_are_safe(["-la"], ws, cwd=sub))
        check("a cwd outside the workspace fails closed",
              not tools._path_args_are_safe(["x"], ws, cwd="/tmp"))


def test_call_site_passes_cwd():
    src = test_support.read_code("tools.py")
    check("execute_shell_command validates against the session cwd",
          "_path_args_are_safe(args_list, base_workspace, cwd=current_cwd)" in src)
    check("the old root-only call is gone",
          "_path_args_are_safe(args_list, base_workspace)" not in src)


def test_terminal_sessions_are_bounded():
    import tools

    tools._terminal_sessions.clear()
    for i in range(tools.MAX_TERMINAL_SESSIONS + 25):
        tools._touch_session(str(i), "/tmp")
    check("the terminal session map is capped",
          len(tools._terminal_sessions) <= tools.MAX_TERMINAL_SESSIONS,
          f"got {len(tools._terminal_sessions)}")
    check("the oldest sessions are the ones evicted",
          "0" not in tools._terminal_sessions)
    check("the newest session survives",
          str(tools.MAX_TERMINAL_SESSIONS + 24) in tools._terminal_sessions)

    tools.reset_session("5")
    check("reset_session removes a chat's cwd", "5" not in tools._terminal_sessions)
    tools._terminal_sessions.clear()


# ── secrets & search ────────────────────────────────────────────────────────
def test_every_secret_is_stripped():
    src = test_support.read_code("tools.py")
    for key in ("TELEGRAM_BOT_TOKEN", "ZAI_API_KEY", "GEMINI_API_KEY",
                "EXA_API_KEY", "DATABASE_URL", "WEBHOOK_SECRET_TOKEN"):
        check(f"{key} is stripped from the subprocess env", f'"{key}"' in src)


def test_exa_falls_back_to_ddgs():
    src = test_support.read_code("tools.py")
    check("an Exa failure no longer short-circuits the search",
          'return [{"error": f"Exa search failed' not in src)
    check("DDGS is still reachable after the Exa block",
          src.index("DDGS is None") > src.index("Exa search error"))


def test_dead_code_is_gone():
    tools_src = test_support.read_code("tools.py")
    bot_src = test_support.read_code("bot.py")
    check("is_env_access_attempt is deleted",
          "def is_env_access_attempt" not in tools_src)
    check("the unimported InputRichMessage branch is deleted",
          "InputRichMessage" not in bot_src)
    check("...along with its send_rich_message guard",
          "send_rich_message" not in bot_src)


# ── task cancellation ordering ──────────────────────────────────────────────
def test_cancel_detaches_before_cancelling():
    import agent

    async def scenario():
        agent._running_tasks.clear()

        async def forever():
            await asyncio.sleep(30)

        chat = 4242
        first = asyncio.create_task(forever())
        agent.register_running_task(chat, first)

        # A task registered while cancellation is in flight used to land in a set
        # that was popped immediately afterwards, orphaning it forever.
        await agent.cancel_running_task(chat)
        late = asyncio.create_task(forever())
        agent.register_running_task(chat, late)

        check("a task registered after a cancel is still reachable",
              late in agent._running_tasks.get(chat, set()))
        check("...and a second /stop actually cancels it",
              await agent.cancel_running_task(chat) is True)
        await asyncio.sleep(0)
        check("the late task really was cancelled", late.cancelled() or late.done())

        for t in (first, late):
            t.cancel()
        agent._running_tasks.clear()

    asyncio.run(scenario())


# ── command gating ──────────────────────────────────────────────────────────
def test_privileged_commands_are_gated():
    src = test_support.read_code("bot.py")

    for cmd in ("cmd_stop", "cmd_skills", "cmd_sessions", "cmd_clear"):
        start = src.index(f"def {cmd}(")
        window = src[start:start + 700]
        check(f"/{cmd.removeprefix('cmd_')} checks is_user_privileged",
              "is_user_privileged" in window)


def test_clear_resets_the_rest_of_the_context():
    src = test_support.read_code("bot.py")
    start = src.index("def cmd_clear(")
    window = src[start:start + 900]
    check("/clear drops inlined skill bodies", "clear_loaded_skills" in window)
    check("/clear resets the shell cwd", "reset_session" in window)
    check("/clear still drops visuals", "clear_visuals" in window)


def test_skills_command_lists_slugs():
    src = test_support.read_code("bot.py")
    start = src.index("def cmd_skills(")
    window = src[start:start + 900]
    check("/skills prints the slug that other commands accept",
          "s['slug']" in window)


def main():
    print("phase 5 — skills & tools hygiene\n")
    for fn in (
        test_skill_slug_identity,
        test_skill_index_is_cached,
        test_loaded_skill_tracking,
        test_loaded_skills_reach_the_prompt,
        test_path_validation_uses_session_cwd,
        test_call_site_passes_cwd,
        test_terminal_sessions_are_bounded,
        test_every_secret_is_stripped,
        test_exa_falls_back_to_ddgs,
        test_dead_code_is_gone,
        test_cancel_detaches_before_cancelling,
        test_privileged_commands_are_gated,
        test_clear_resets_the_rest_of_the_context,
        test_skills_command_lists_slugs,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 5 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 5 checks passed")


if __name__ == "__main__":
    main()
