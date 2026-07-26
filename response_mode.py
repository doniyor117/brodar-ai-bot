"""
Per-turn task-mode classification.

Two modes:
  - "extraction"   the user asked for something pulled verbatim out of media or
                    a file — a transcript, a translation, an OCR read, subtitles.
  - "conversation" everything else, including "tldr", "summary", "what's this",
                    or plain chat with no media at all.

This is decided here, deterministically, by matching the user's own words
against a trigger list — not left to the model. Asked to both judge its own
mode and stay in character, the model reliably picks "in character" and jokes
through a transcript request instead of just producing one.
"""
import re
from typing import Optional

import config

_trigger_re: Optional["re.Pattern"] = None


def _compiled() -> "re.Pattern":
    global _trigger_re
    if _trigger_re is None:
        triggers = [t.strip() for t in config.EXTRACTION_TRIGGERS.split(",") if t.strip()]
        pattern = "|".join(re.escape(t) for t in triggers)
        _trigger_re = re.compile(pattern, re.IGNORECASE)
    return _trigger_re


def classify(text: str, has_media: bool) -> str:
    """
    Returns "extraction" or "conversation".

    Extraction requires BOTH a trigger phrase AND media in play this turn (or
    carried over from a recent one) — "translate this" with nothing attached is
    just chat, and a bare photo with no instructions isn't a transcription
    request either.
    """
    if not has_media or not text:
        return "conversation"
    if _compiled().search(text):
        return "extraction"
    return "conversation"
