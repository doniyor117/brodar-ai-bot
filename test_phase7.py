"""
Phase 7 smoke checks — speech fidelity.

Uses a real ffmpeg to synthesize test tones and round-trip them through
media.extract_audio() where ffmpeg is available; skips those checks (rather
than failing) when it isn't, since ffmpeg isn't guaranteed in every dev
environment. Everything else runs unconditionally.

    python3 test_phase7.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile

import test_support

test_support.install_stubs()

FAILURES = []
SKIPPED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def skip(name, reason):
    print(f"  skip {name} ({reason})")
    SKIPPED.append(name)


def _make_tone(path: str, duration: float, exe: str) -> bool:
    try:
        subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={duration}", "-y", path],
            check=True, capture_output=True, timeout=20,
        )
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception:
        return False


# ── media.extract_audio: FLAC-first, with a bounded lossy fallback ──────────
def test_extract_audio_flac():
    import media

    exe = media.ffmpeg_exe()
    if not exe:
        skip("test_extract_audio_flac", "no ffmpeg in this environment")
        return

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.wav")
            if not _make_tone(src, 3.0, exe):
                skip("test_extract_audio_flac", "couldn't synthesize a test tone")
                return

            data, mime = await media.extract_audio(src)
            check("a normal clip encodes to FLAC, not MP3",
                  mime == "audio/flac", f"got {mime!r}")
            check("FLAC output has real content",
                  len(data) > 100)
            check("...and is actually a FLAC file (magic bytes)",
                  data[:4] == b"fLaC", f"got {data[:4]!r}")

    asyncio.run(scenario())


def test_extract_audio_respects_max_seconds():
    import media

    exe = media.ffmpeg_exe()
    if not exe:
        skip("test_extract_audio_respects_max_seconds", "no ffmpeg in this environment")
        return

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.wav")
            if not _make_tone(src, 5.0, exe):
                skip("test_extract_audio_respects_max_seconds", "couldn't synthesize a test tone")
                return

            full, _ = await media.extract_audio(src)
            short, _ = await media.extract_audio(src, max_seconds=1.0)
            check("capping max_seconds actually shrinks the output",
                  0 < len(short) < len(full), f"full={len(full)} short={len(short)}")

    asyncio.run(scenario())


def test_extract_audio_falls_back_over_budget():
    import media

    exe = media.ffmpeg_exe()
    if not exe:
        skip("test_extract_audio_falls_back_over_budget", "no ffmpeg in this environment")
        return

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.wav")
            if not _make_tone(src, 2.0, exe):
                skip("test_extract_audio_falls_back_over_budget", "couldn't synthesize a test tone")
                return

            original_budget = media.AUDIO_FLAC_MAX_BYTES
            media.AUDIO_FLAC_MAX_BYTES = 8  # nothing survives this budget
            try:
                data, mime = await media.extract_audio(src)
            finally:
                media.AUDIO_FLAC_MAX_BYTES = original_budget

            check("an over-budget FLAC falls back to mp3",
                  mime == "audio/mp3", f"got {mime!r}")
            check("...and the fallback still has real content",
                  len(data) > 100)

    asyncio.run(scenario())


def test_extract_audio_no_ffmpeg_fails_closed():
    import media

    async def scenario():
        original = media.ffmpeg_exe
        media.ffmpeg_exe = lambda: None
        try:
            data, mime = await media.extract_audio("/no/such/file")
        finally:
            media.ffmpeg_exe = original
        check("no ffmpeg binary returns (b'', '') rather than raising",
              data == b"" and mime == "")

    asyncio.run(scenario())


# ── config knobs ──────────────────────────────────────────────────────────
def test_config_knobs():
    import config

    check("MEDIA_MAX_AUDIO_SECONDS was raised to 600",
          config.MEDIA_MAX_AUDIO_SECONDS == 600.0,
          f"got {config.MEDIA_MAX_AUDIO_SECONDS}")
    check("SPEECH_LANGUAGES defaults to all four requested languages",
          config.SPEECH_LANGUAGES == "uz-Latn,uz-Cyrl,ru,en")


# ── bot.py: dynamic mime, honest truncation notices ─────────────────────────
def test_call_sites_use_real_mime_not_hardcoded_mp3():
    src = test_support.read_code("bot.py")
    check("the video-audio path no longer hardcodes audio/mp3",
          '_to_data_url(audio, "audio/mp3")' not in src)
    check("the voice/audio-document path no longer hardcodes audio/mp3",
          '_to_data_url(converted, "audio/mp3")' not in src)
    check("the video-audio path uses the mime extract_audio actually returned",
          "_to_data_url(audio, audio_mime)" in src)
    check("the voice/audio-document path uses the mime extract_audio actually returned",
          "_to_data_url(converted, converted_mime)" in src)


def test_truncation_is_never_silent():
    src = test_support.read_code("bot.py")
    check("the video/video-note path warns when audio is cut short",
          "info.duration > config.MEDIA_MAX_AUDIO_SECONDS" in src)
    check("the voice/audio path warns too, using Telegram's own duration field",
          "src_duration > config.MEDIA_MAX_AUDIO_SECONDS" in src)
    check("both paths phrase it as an honest heard-only-part-of-it note",
          src.count("i heard") >= 2)


# ── agent.py: language hint + honest audio-unsupported message ──────────────
def test_language_hint_content():
    import agent

    hint = agent._speech_language_hint()
    check("the hint names Uzbek", "Uzbek" in hint)
    check("the hint names Russian", "Russian" in hint)
    check("the hint names English", "English" in hint)
    check("the hint explicitly says Uzbek is not Turkish",
          "not turkish" in hint.lower())
    check("the hint tells the model to mark unclear words instead of inventing them",
          "[unclear]" in hint)


def test_language_hint_only_fires_with_audio_in_play():
    src = test_support.read_code("agent.py")
    check("the hint is only appended when a sendable item is kind==audio",
          'i.get("kind") == "audio" for i in current + prior' in src)


def test_dropped_audio_recommends_gemini_by_name():
    src = test_support.read_code("agent.py")
    check("a model that can't hear audio is told to switch to gemini specifically",
          "switch me to gemini" in src)
    check("...distinct from the generic dropped-image message",
          "other_dropped = dropped" in src)


def main():
    print("phase 7 — speech fidelity\n")
    for fn in (
        test_extract_audio_flac,
        test_extract_audio_respects_max_seconds,
        test_extract_audio_falls_back_over_budget,
        test_extract_audio_no_ffmpeg_fails_closed,
        test_config_knobs,
        test_call_sites_use_real_mime_not_hardcoded_mp3,
        test_truncation_is_never_silent,
        test_language_hint_content,
        test_language_hint_only_fires_with_audio_in_play,
        test_dropped_audio_recommends_gemini_by_name,
    ):
        fn()

    print()
    if SKIPPED:
        print(f"{len(SKIPPED)} phase 7 check(s) skipped (no ffmpeg):")
        for s in SKIPPED:
            print(f"  - {s}")
    if FAILURES:
        print(f"{len(FAILURES)} phase 7 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 7 checks passed")


if __name__ == "__main__":
    main()
