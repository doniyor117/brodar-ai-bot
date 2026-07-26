"""
Phase 6 smoke checks — task modes: extraction vs conversation.

    python3 test_phase6.py
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


# ── response_mode.classify ───────────────────────────────────────────────────
def test_extraction_requires_media_and_trigger():
    import response_mode

    check("a trigger word with no media is just conversation",
          response_mode.classify("transcribe this for me", has_media=False) == "conversation")
    check("media with no trigger word is just conversation",
          response_mode.classify("lol look at this", has_media=True) == "conversation")
    check("media plus a trigger word is extraction",
          response_mode.classify("can you transcribe this voice note", has_media=True) == "extraction")
    check("no text and no media is conversation",
          response_mode.classify("", has_media=False) == "conversation")
    check("media with empty text is conversation (nothing to match)",
          response_mode.classify("", has_media=True) == "conversation")


def test_extraction_triggers_are_multilingual():
    import response_mode

    cases = [
        ("translate this please", "english translate"),
        ("what does this say", "english ocr-ish phrasing"),
        ("tarjima qil buni", "uzbek latin"),
        ("matnini yozib ber", "uzbek latin"),
        ("расшифруй это голосовое", "russian"),
        ("переведи текст", "russian"),
    ]
    for text, label in cases:
        check(f"'{text}' ({label}) triggers extraction",
              response_mode.classify(text, has_media=True) == "extraction")


def test_conversation_stays_conversation():
    import response_mode

    for text in ("tldr", "summarize this", "what's this about", "lol nice", "who's in the pic"):
        check(f"'{text}' stays conversation, not extraction",
              response_mode.classify(text, has_media=True) == "conversation")


# ── build_system_prompt mode plumbing ────────────────────────────────────────
def test_extraction_mode_appends_scoped_block():
    import agent

    plain = agent.build_system_prompt(
        persona="p", learned_facts="", avail_skills=[], date_line="2026-07-26",
        mode="conversation",
    )
    extraction = agent.build_system_prompt(
        persona="p", learned_facts="", avail_skills=[], date_line="2026-07-26",
        mode="extraction",
    )
    check("conversation mode carries no extraction block",
          "Task Mode: Extraction" not in plain)
    check("extraction mode appends the scoped override",
          "Task Mode: Extraction" in extraction)
    check("the override forbids [SILENT] in this one reply",
          "[SILENT]" in extraction)
    check("the override forbids inventing unclear words",
          "unclear" in extraction.lower())
    check("extraction block is the LAST block (a scoped override, not a rewrite)",
          extraction.rstrip().endswith(extraction.split("# Task Mode: Extraction")[-1].rstrip()) and
          extraction.index("Task Mode: Extraction") > extraction.index("Who You're Talking To"))


# ── generate_response threads mode through to temperature + prompt ──────────
def test_generate_response_signature_accepts_mode():
    src = test_support.read_code("agent.py")
    check("generate_response accepts a mode kwarg",
          'mode: str = "conversation"' in src)
    check("build_system_prompt is called with mode=mode",
          "mode=mode" in src)
    check("extraction turns get the low-temperature env var",
          "config.EXTRACTION_TEMPERATURE if mode ==" in src)


# ── config knobs exist and are sane ──────────────────────────────────────────
def test_config_knobs():
    import config

    check("EXTRACTION_TRIGGERS is non-empty", bool(config.EXTRACTION_TRIGGERS.strip()))
    check("EXTRACTION_TEMPERATURE is low (greedier than default)",
          0.0 <= config.EXTRACTION_TEMPERATURE <= 0.5)
    check("EXTRACTION_CHUNK_OVERFLOW is a small positive int",
          isinstance(config.EXTRACTION_CHUNK_OVERFLOW, int) and config.EXTRACTION_CHUNK_OVERFLOW > 0)


# ── bot.py wiring: classify → generate_response → post-processing ───────────
def test_bot_computes_mode_before_generating():
    src = test_support.read_code("bot.py")
    check("bot.py imports response_mode",
          "import response_mode" in src)
    check("turn_mode is classified from cleaned_text and media state",
          "response_mode.classify(cleaned_text, has_media or had_prior_media)" in src)
    check("generate_response is called with mode=turn_mode",
          "mode=turn_mode" in src)


def test_extraction_skips_persona_postprocessing():
    src = test_support.read_code("bot.py")
    start = src.index('if turn_mode == "extraction":')
    else_at = src.index("else:", start)
    end_at = src.index("generation_time = time.time()", else_at)
    extraction_branch = src[start:else_at]
    conversation_branch = src[else_at:end_at]

    check("extraction branch does not call enforce_lowercase",
          "enforce_lowercase" not in extraction_branch)
    check("extraction branch does not call parse_control_tokens",
          "parse_control_tokens" not in extraction_branch)
    # The conversational branch still has both — proving they weren't deleted
    # outright, just scoped out of extraction.
    check("...but the conversation branch still has enforce_lowercase",
          "enforce_lowercase" in conversation_branch)
    check("...and still has parse_control_tokens",
          "parse_control_tokens" in conversation_branch)


def test_extraction_overflow_delivers_as_file():
    src = test_support.read_code("bot.py")
    check("send_extraction_result exists",
          "async def send_extraction_result(" in src)
    check("short extraction replies still go through send_long_reply",
          "await send_long_reply(text)" in src or "await send_long_reply(message, text)" in src)
    check("long ones are written under workspace/generated",
          '"generated"' in src)
    check("...and delivered with reply_document",
          "reply_document" in src)
    call_idx = src.index("send_extraction_result(message, bot_reply)")
    check("the call site picks send_extraction_result for extraction turns",
          'if turn_mode == "extraction"' in src[max(0, call_idx - 150):call_idx])


def test_persona_documents_the_job_mode():
    with open("PERSONA.md", encoding="utf-8") as f:
        persona = f.read()
    check("PERSONA.md has the job-mode section",
          "# When You're Given a Job" in persona)
    check("it's in the always-applicable part, not the group-only part",
          persona.index("# When You're Given a Job") < persona.index("<!-- GROUP-ONLY -->"))


def main():
    print("phase 6 — task modes: extraction vs conversation\n")
    for fn in (
        test_extraction_requires_media_and_trigger,
        test_extraction_triggers_are_multilingual,
        test_conversation_stays_conversation,
        test_extraction_mode_appends_scoped_block,
        test_generate_response_signature_accepts_mode,
        test_config_knobs,
        test_bot_computes_mode_before_generating,
        test_extraction_skips_persona_postprocessing,
        test_extraction_overflow_delivers_as_file,
        test_persona_documents_the_job_mode,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 6 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 6 checks passed")


if __name__ == "__main__":
    main()
