"""
Phase 3 smoke checks — the rebuilt media engine.

    python3 test_phase3.py
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


def img(url="data:image/jpeg;base64,AAA", is_gif=False):
    return {"data_url": url, "kind": "image", "is_gif": is_gif}


def aud(url="data:audio/mp3;base64,BBB"):
    return {"data_url": url, "kind": "audio"}


# ── audio is audio, not an image ────────────────────────────────────────────
def test_media_blocks():
    import agent

    block = agent._media_block(img())
    check("an image becomes an image_url block", block["type"] == "image_url")
    check("the image url is carried through",
          block["image_url"]["url"] == "data:image/jpeg;base64,AAA")

    block = agent._media_block(aud())
    check("audio becomes a file block, NOT image_url", block["type"] == "file")
    check("the audio data url goes in file_data",
          block["file"]["file_data"] == "data:audio/mp3;base64,BBB")

    check("an item with no url produces no block",
          agent._media_block({"kind": "image"}) is None)

    blocks = agent._media_blocks([img(), aud(), img()])
    check("mixed media keeps its order",
          [b["type"] for b in blocks] == ["image_url", "file", "image_url"])


# ── per-model capability filtering ──────────────────────────────────────────
def test_capability_filter():
    import agent
    import models

    text_only = models.MODELS["glm-4.7-flash"]
    multimodal = models.MODELS["gemini-3.5-flash-lite"]

    check("the text model declares no vision", text_only.supports_vision is False)
    check("the text model declares no audio", text_only.supports_audio is False)
    check("gemini declares vision", multimodal.supports_vision is True)
    check("gemini declares audio", multimodal.supports_audio is True)

    items = [img(), aud()]

    sendable, dropped = agent.filter_media_for_model(items, multimodal)
    check("a multimodal model gets everything", len(sendable) == 2 and not dropped)

    sendable, dropped = agent.filter_media_for_model(items, text_only)
    check("a text model gets nothing", sendable == [])
    check("and both kinds are reported dropped", dropped == {"image", "audio"})

    class VisionOnly:
        supports_vision = True
        supports_audio = False
        label = "vision-only"

    sendable, dropped = agent.filter_media_for_model(items, VisionOnly())
    check("a vision-only model keeps the image", len(sendable) == 1
          and sendable[0]["kind"] == "image")
    check("...and reports the audio as dropped", dropped == {"audio"})


# ── attaching media to the conversation ─────────────────────────────────────
def test_attachment():
    import agent

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "what's this?"}]
    agent._attach_media_to_last_user(msgs, [img(), aud()])
    content = msgs[-1]["content"]
    check("the last user message becomes multimodal", isinstance(content, list))
    check("text comes first", content[0]["type"] == "text")
    check("then the media, in order",
          [b["type"] for b in content[1:]] == ["image_url", "file"])

    msgs = [{"role": "user", "content": "hi"}]
    agent._attach_media_to_last_user(msgs, [])
    check("no media leaves the message alone", msgs[0]["content"] == "hi")

    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    agent._insert_context_media(msgs, [img()])
    check("context media is inserted before the current turn",
          isinstance(msgs[1]["content"], list) and msgs[-1]["content"] == "b")


# ── frame counting ──────────────────────────────────────────────────────────
def test_frame_counting():
    import media

    check("a 60s video gets the full budget",
          media.frame_count_for(60, 10, 3) == 10)
    check("a 12s video gets one frame per 3s (min 3)",
          media.frame_count_for(12, 10, 3) == 4)
    check("a 3s video still gets a floor of 3",
          media.frame_count_for(3, 10, 3) == 3)
    check("an hour-long video is capped, not proportional",
          media.frame_count_for(3600, 10, 3) == 10)
    check("unknown duration gets a safe default",
          media.frame_count_for(0, 10, 3) == 3)
    check("the cap is always respected",
          all(media.frame_count_for(d, 5, 3) <= 5 for d in (0, 1, 30, 600, 99999)))
    check("at least one frame is always requested",
          media.frame_count_for(0.1, 10, 3) >= 1)


# ── per-turn budget ─────────────────────────────────────────────────────────
def test_budget():
    import config
    import media
    import bot

    def result_with(items):
        r = media.ExtractionResult()
        r.items = items
        return r

    # Item count cap.
    old_items = config.MEDIA_MAX_ITEMS_PER_TURN
    config.MEDIA_MAX_ITEMS_PER_TURN = 3
    r = result_with([media.MediaItem(f"data:image/jpeg;base64,{i}") for i in range(10)])
    bot._apply_media_budget(r)
    check("the item cap is enforced", len(r.items) == 3)
    check("and the truncation is reported", any("only part" in n for n in r.notes))
    config.MEDIA_MAX_ITEMS_PER_TURN = old_items

    # Audio survives ahead of frames — it carries the actual words.
    config.MEDIA_MAX_ITEMS_PER_TURN = 2
    r = result_with([
        media.MediaItem("data:image/jpeg;base64,1"),
        media.MediaItem("data:image/jpeg;base64,2"),
        media.MediaItem("data:audio/mp3;base64,A", kind="audio"),
    ])
    bot._apply_media_budget(r)
    check("audio is kept when the budget forces a choice",
          any(i.kind == "audio" for i in r.items), f"got {[i.kind for i in r.items]}")
    config.MEDIA_MAX_ITEMS_PER_TURN = old_items

    # Byte cap.
    old_bytes = config.MEDIA_MAX_TURN_BYTES
    config.MEDIA_MAX_TURN_BYTES = 50
    r = result_with([media.MediaItem("x" * 30) for _ in range(5)])
    bot._apply_media_budget(r)
    check("the byte cap is enforced", sum(i.size() for i in r.items) <= 50)
    config.MEDIA_MAX_TURN_BYTES = old_bytes

    # Nothing is dropped when everything fits, and order is preserved.
    r = result_with([media.MediaItem(f"data:image/jpeg;base64,{i}") for i in range(3)])
    bot._apply_media_budget(r)
    check("a small batch passes through untouched", len(r.items) == 3 and not r.notes)
    check("frame order is preserved",
          [i.data_url[-1] for i in r.items] == ["0", "1", "2"])


# ── the retention cap never truncates the current turn ──────────────────────
def test_retention_vs_current_turn():
    import config

    check("the retention cap and the per-turn cap are separate settings",
          config.VISUAL_MEMORY_MAX_IMAGES != config.MEDIA_MAX_ITEMS_PER_TURN
          or config.MEDIA_MAX_ITEMS_PER_TURN >= config.MEDIA_MAX_FRAMES)
    check("a full video's frames fit within the per-turn cap",
          config.MEDIA_MAX_ITEMS_PER_TURN >= config.MEDIA_MAX_FRAMES + 1,
          f"{config.MEDIA_MAX_ITEMS_PER_TURN} vs {config.MEDIA_MAX_FRAMES}+audio")

    src = open("bot.py").read()
    check("this turn's media bypasses the retention limit",
          "media_items = current" in src)
    check("the retention read is widened by the current turn's size",
          "VISUAL_MEMORY_MAX_IMAGES + len(current)" in src)


# ── media.py is async and bounded ───────────────────────────────────────────
def test_media_is_async():
    import inspect
    import media

    check("probe is async", inspect.iscoroutinefunction(media.probe))
    check("extract_frames is async", inspect.iscoroutinefunction(media.extract_frames))
    check("extract_audio is async", inspect.iscoroutinefunction(media.extract_audio))

    # Check executable code only — the module docstring describes the old
    # blocking implementation it replaced, and would otherwise match itself.
    code = test_support.read_code("media.py")
    check("the blocking subprocess module is not even imported",
          "import subprocess" not in code)
    check("nothing calls subprocess.run", "subprocess . run" not in code)
    check("it never uses the shared thread pool", "to_thread" not in code)

    src = open("media.py").read()
    check("it uses cancellable async subprocesses",
          "create_subprocess_exec" in src)
    check("every invocation is time-bounded", "asyncio.wait_for" in src)
    check("overrunning processes are killed", "proc.kill()" in src)
    check("ffmpeg stderr is logged rather than discarded",
          "err.decode" in src)


# ── extraction result helpers ───────────────────────────────────────────────
def test_extraction_result():
    import media

    r = media.ExtractionResult()
    r.items = [
        media.MediaItem("data:image/jpeg;base64,1"),
        media.MediaItem("data:audio/mp3;base64,A", kind="audio"),
        media.MediaItem("data:image/jpeg;base64,2", is_gif=True),
    ]
    check("images() returns only images", len(r.images()) == 2)
    check("audio() returns only audio", len(r.audio()) == 1)
    check("items default to the image kind", media.MediaItem("x").kind == "image")
    check("items are not gif frames by default", media.MediaItem("x").is_gif is False)
    check("size() measures the encoded payload", media.MediaItem("abcd").size() == 4)


# ── album batching ──────────────────────────────────────────────────────────
def test_album_middleware():
    import bot

    class M:
        def __init__(self, mid, group=None, text=None, caption=None):
            self.message_id = mid
            self.media_group_id = group
            self.text = text
            self.caption = caption

    calls = []

    async def handler(event, data):
        calls.append((event, data.get("album")))
        return "handled"

    mw = bot.AlbumMiddleware(window=0.05)

    async def run_album():
        msgs = [M(1, "g1"), M(2, "g1", caption="look at these"), M(3, "g1")]
        return await asyncio.gather(*(mw(handler, m, {}) for m in msgs))

    asyncio.run(run_album())
    check("an album invokes the handler exactly once", len(calls) == 1)
    lead, album = calls[0]
    check("all album members are collected", album is not None and len(album) == 3)
    check("the captioned member leads, so the text isn't lost",
          lead.message_id == 2)

    calls.clear()

    async def run_single():
        return await mw(handler, M(9, None, text="hi"), {})

    asyncio.run(run_single())
    check("a lone message is passed straight through", len(calls) == 1)
    check("...with no album attached", calls[0][1] is None)
    check("no album state is leaked", mw._groups == {})


# ── storage carries the kind through ────────────────────────────────────────
def test_storage_schema():
    db_src = open("db.py").read()
    check("recent_visuals has a kind column",
          "ADD COLUMN IF NOT EXISTS kind" in db_src)
    check("kind is written on insert",
          "data_url, is_gif, kind" in db_src)
    check("kind is read back",
          "SELECT data_url, is_gif, turn, kind" in db_src)
    check("there is a full-clear for /clear",
          "async def clear_recent_visuals" in db_src)
    check("clearing also resets the turn counters",
          "msg_turn = 0, last_visual_turn = 0" in db_src)

    cache_src = open("cache.py").read()
    check("cache exposes a standalone prune", "async def prune_visuals" in cache_src)
    check("cache exposes a clear", "async def clear_visuals" in cache_src)

    bot_src = open("bot.py").read()
    check("/clear drops retained media", "await cache.clear_visuals(chat_id)" in bot_src)
    check("the passive group path prunes too",
          bot_src.count("prune_visuals") >= 2)


if __name__ == "__main__":
    for fn in (test_media_blocks, test_capability_filter, test_attachment,
               test_frame_counting, test_budget, test_retention_vs_current_turn,
               test_media_is_async, test_extraction_result,
               test_album_middleware, test_storage_schema):
        print(f"\n{fn.__name__}:")
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all phase 3 checks passed")
