import os
import sys
from typing import Optional
from dotenv import load_dotenv

# Load .env file if it exists (for local development)
load_dotenv()

# Telegram configurations
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "super-secret-telegram-token")

# Allowed users for DMs and activation
_allowed_users_raw = os.getenv("ALLOWED_DM_USER_IDS", "2030903420,8116285130")
ALLOWED_DM_USER_IDS = []
if _allowed_users_raw:
    try:
        ALLOWED_DM_USER_IDS = [int(uid.strip()) for uid in _allowed_users_raw.split(",") if uid.strip()]
    except ValueError as e:
        print(f"WARNING: Failed to parse ALLOWED_DM_USER_IDS: {e}")

MAIN_ACCOUNT_ID = None
_main_account_raw = os.getenv("MAIN_ACCOUNT_ID", "")
if _main_account_raw.strip():
    try:
        MAIN_ACCOUNT_ID = int(_main_account_raw.strip())
    except ValueError:
        print("WARNING: Failed to parse MAIN_ACCOUNT_ID")

# LLM configurations
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
# Google AI Studio key for Gemini models (used via LiteLLM).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Default active model — must be a key in models.MODELS. Can be changed at
# runtime with /model; the choice is persisted globally.
MODEL_NAME = os.getenv("MODEL_NAME", "glm-4.7-flash")
# Default model for image generation
IMAGE_MODEL_NAME = os.getenv("IMAGE_MODEL_NAME", "dall-e-3")

# Number of LLM API calls allowed in flight at once. The Z.ai free tier is
# concurrency limited, but 1 means a single slow chat blocks every other chat.
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "2"))

# ── Timeouts ────────────────────────────────────────────────────────────────
# Every blocking dependency needs a bound. Without one, a single stalled call
# holds a concurrency slot / thread / connection forever and the whole bot stops
# responding — which is exactly what was happening in production.
#
# Wall-clock cap on one LLM call. litellm's own default is ~600s, which with
# LLM_CONCURRENCY=2 means two stalled calls freeze every chat for ten minutes.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
# Cap on a web search (Exa or DuckDuckGo). These run in a dedicated thread pool.
SEARCH_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "25"))
# How many searches may run at once. Deliberately its own small pool so a hung
# search can never starve media extraction or the shell tool.
SEARCH_POOL_SIZE = int(os.getenv("SEARCH_POOL_SIZE", "2"))
# Cap on waiting for a free Postgres connection (separate from command_timeout,
# which only bounds the query once a connection is already held). Matches the
# pool's connect timeout, because acquiring may have to open a new connection
# and Neon scales to zero — a shorter bound would fail every cold start.
DB_ACQUIRE_TIMEOUT = float(os.getenv("DB_ACQUIRE_TIMEOUT", "30"))
# How long an approval prompt stays live before it is treated as a DENIAL.
# Raised from 60s: approvals now go to MAIN_ACCOUNT_ID's DM instead of the
# requesting chat, so it needs a human in a DIFFERENT chat to notice and tap —
# 60s was tuned for someone already looking at the conversation.
APPROVAL_TIMEOUT_SECONDS = float(os.getenv("APPROVAL_TIMEOUT_SECONDS", "180"))

# ── Addressing the bot ──────────────────────────────────────────────────────
# Names that count as calling the bot in a mention-only group, in addition to
# @username mentions, text_mention entities, and replies to the bot's messages.
_aliases_raw = os.getenv("BOT_ALIASES", "brodar,simon bro,samy")
BOT_ALIASES = [a.strip().lower() for a in _aliases_raw.split(",") if a.strip()]

# Hard-enforce brodar's all-lowercase style in code, so a flash model slipping
# and capitalizing can't break character. URLs and code spans are preserved.
FORCE_LOWERCASE = os.getenv("FORCE_LOWERCASE", "true").strip().lower() in ("1", "true", "yes", "on")

# Default for whether the bot posts the "🔍 searching..." tool-activity notes in
# a chat. This is the per-chat default; each chat can override it with /toggle_tools.
SHOW_TOOL_NOTES_DEFAULT = os.getenv("SHOW_TOOL_NOTES", "true").strip().lower() in ("1", "true", "yes", "on")

# Conversation history / auto-compaction.
# How many messages to keep in the working window (hard safety cap). Token-based
# compaction normally kicks in first for heavy chats.
HISTORY_MAXLEN = int(os.getenv("HISTORY_MAXLEN", "250"))
# When the context exceeds this many tokens, older messages are summarized into a
# checkpoint and dropped, keeping the most recent ones verbatim.
COMPACT_TOKEN_THRESHOLD = int(os.getenv("COMPACT_TOKEN_THRESHOLD", "100000"))
# How many tokens' worth of the most recent messages to keep verbatim after a
# compaction (the rest get folded into the running summary).
COMPACT_KEEP_TOKENS = int(os.getenv("COMPACT_KEEP_TOKENS", "30000"))
# Never keep fewer than this many recent messages after compaction.
COMPACT_MIN_KEEP_MESSAGES = int(os.getenv("COMPACT_MIN_KEEP_MESSAGES", "8"))

# Visual memory: how many turns a sent image/GIF stays available to the model for
# follow-up questions before it's dropped. 0 disables (send-once). Each retained
# image is re-sent every turn in its window, so keep this modest for token cost.
VISUAL_MEMORY_TURNS = int(os.getenv("VISUAL_MEMORY_TURNS", "8"))
# Hard cap on how many RETAINED images from *earlier* turns may be re-attached.
VISUAL_MEMORY_MAX_IMAGES = int(os.getenv("VISUAL_MEMORY_MAX_IMAGES", "5"))

# ── Media extraction budgets ────────────────────────────────────────────────
# The retention cap above must NOT be used to cap the current turn. It used to
# be: a video producing 10 frames was stored and then read back with
# "ORDER BY id DESC LIMIT 5", silently throwing away the first 60% of the video
# the user had just sent. These are separate numbers for separate jobs.
#
# Frames to pull from one video, and roughly how far apart.
MEDIA_MAX_FRAMES = int(os.getenv("MEDIA_MAX_FRAMES", "10"))
MEDIA_SECONDS_PER_FRAME = float(os.getenv("MEDIA_SECONDS_PER_FRAME", "3"))
# Frames for a short looping clip (gif, video sticker, video note).
MEDIA_LOOP_FRAMES = int(os.getenv("MEDIA_LOOP_FRAMES", "3"))
# Long edge, in pixels, of an extracted frame.
MEDIA_FRAME_MAX_DIM = int(os.getenv("MEDIA_FRAME_MAX_DIM", "384"))
# Longest stretch of audio transcribed from one file. 16kHz mono FLAC (see
# media.extract_audio) runs ~8-10 KB/s, so 600s is ~5-6MB raw — comfortably
# inside Gemini's 20MB inline request cap even before the fallback MP3 path.
MEDIA_MAX_AUDIO_SECONDS = float(os.getenv("MEDIA_MAX_AUDIO_SECONDS", "600"))
# Languages to hint the model toward when a turn carries audio, most-likely
# first. There is no `languageCode` parameter on Gemini's generateContent —
# the prompt is the only channel, and an explicit hint measurably improves
# accuracy on multilingual/accented audio (see response_mode.py's neighbour,
# the language-hint block in agent.generate_response).
SPEECH_LANGUAGES = os.getenv("SPEECH_LANGUAGES", "uz-Latn,uz-Cyrl,ru,en")
# Most images that may be attached to a single request, current turn included.
MEDIA_MAX_ITEMS_PER_TURN = int(os.getenv("MEDIA_MAX_ITEMS_PER_TURN", "12"))
# Ceiling on the total encoded size of one turn's media, in bytes of base64.
MEDIA_MAX_TURN_BYTES = int(os.getenv("MEDIA_MAX_TURN_BYTES", str(12 * 1024 * 1024)))
# How long to wait for the rest of a Telegram album before answering it.
MEDIA_ALBUM_WINDOW = float(os.getenv("MEDIA_ALBUM_WINDOW", "1.2"))

# ── Task modes: extraction vs conversation ─────────────────────────────────
# Phrases (any language the group actually speaks) that mark a turn as asking
# for something pulled verbatim out of media/a file, rather than ordinary chat.
# Matched case-insensitively as a substring against the user's own message text
# by response_mode.classify(). Comma-separated so it's tunable without a deploy.
EXTRACTION_TRIGGERS = os.getenv(
    "EXTRACTION_TRIGGERS",
    "transcribe,transcript,transcription,translate,translation,verbatim,"
    "word for word,ocr,read the text,extract the text,extract this,subtitle,"
    "subtitles,what does it say,what does this say,"
    "transkripsiya,transkript,tarjima qil,tarjima qilib ber,yozib ber,"
    "matnini yoz,matnini chiqar,subtitr,"
    "расшифруй,расшифровка,переведи,перевод,транскрипт,текст с,субтитры,"
    "слово в слово,таржима қил,ёзиб бер,матнини ёз"
)
# Temperature for extraction-mode turns only — low, for greedy/faithful output.
# Conversation turns are unaffected and keep the model's own default. This is a
# documented tradeoff against Google's "leave Gemini 3 at 1.0" guidance, hence
# an env var rather than a hardcoded constant.
EXTRACTION_TEMPERATURE = float(os.getenv("EXTRACTION_TEMPERATURE", "0.2"))
# A chunked reply longer than this many 4096-char Telegram messages is instead
# written to a file and delivered as a document, with a one-line note in chat.
EXTRACTION_CHUNK_OVERFLOW = int(os.getenv("EXTRACTION_CHUNK_OVERFLOW", "3"))

# Database configurations
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Web server configurations
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

# Directory the whitelisted shell tool is allowed to run in. Kept deliberately
# separate from the source tree so tools like `grep -r` can never reach .env.
TOOL_WORKSPACE_DIR = os.getenv(
    "TOOL_WORKSPACE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
)

# ── Web fetch / download (webio.py) ─────────────────────────────────────────
# fetch_url: reads a page/API response as text. Small and short-lived — this
# runs inline in a tool loop the user is waiting on.
FETCH_TIMEOUT_SECONDS = float(os.getenv("FETCH_TIMEOUT_SECONDS", "15"))
FETCH_MAX_BYTES = int(os.getenv("FETCH_MAX_BYTES", str(2 * 1024 * 1024)))
FETCH_MAX_REDIRECTS = int(os.getenv("FETCH_MAX_REDIRECTS", "3"))

# download_url: saves a file into workspace/downloads/<chat_id>/. Bigger and
# slower than a fetch, so it gets its own budget rather than reusing fetch's.
DOWNLOAD_TIMEOUT_SECONDS = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "60"))
DOWNLOAD_MAX_BYTES = int(os.getenv("DOWNLOAD_MAX_BYTES", str(45 * 1024 * 1024)))
# Per-chat total across every file in its downloads/ folder. Oldest-accessed
# files are evicted first once a new download would exceed this.
DOWNLOAD_CHAT_QUOTA_BYTES = int(os.getenv("DOWNLOAD_CHAT_QUOTA_BYTES", str(200 * 1024 * 1024)))

# send_file: pre-upload size checks, so an oversized file fails with a plain
# message instead of a raw Telegram API error. Telegram's own hard ceilings
# for a bot-uploaded file are 10MB (photo) / 50MB (document, audio, video).
SEND_FILE_MAX_PHOTO_BYTES = int(os.getenv("SEND_FILE_MAX_PHOTO_BYTES", str(10 * 1024 * 1024)))
SEND_FILE_MAX_DOCUMENT_BYTES = int(os.getenv("SEND_FILE_MAX_DOCUMENT_BYTES", str(50 * 1024 * 1024)))

# Placeholder values shipped in .env.example / render.yaml. Treating these as
# "configured" is what silently breaks the webhook, so we detect them explicitly.
_PLACEHOLDER_MARKERS = (
    "change_me",
    "your_",
    "your-app-subdomain",
    "example.com",
)


def is_placeholder(value: str) -> bool:
    """True if a config value is still an unedited example/placeholder string."""
    if not value:
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def approval_recipient_id() -> Optional[int]:
    """
    Where approval prompts are sent: MAIN_ACCOUNT_ID's DM, falling back to the
    first ALLOWED_DM_USER_IDS entry if MAIN_ACCOUNT_ID is unset. Never the
    group that made the request — see agent._request_interactive_approval.
    """
    if MAIN_ACCOUNT_ID:
        return MAIN_ACCOUNT_ID
    if ALLOWED_DM_USER_IDS:
        return ALLOWED_DM_USER_IDS[0]
    return None


def webhook_url_is_usable() -> bool:
    """True only if WEBHOOK_URL is a real https base URL we can register with Telegram."""
    return bool(WEBHOOK_URL) and not is_placeholder(WEBHOOK_URL) and WEBHOOK_URL.startswith("https://")


def validate_config(exit_on_error: bool = True) -> list:
    """
    Validates required environment variables.

    Missing or placeholder credentials previously only printed a warning and the
    app booted into a permanently broken state, so this now fails loudly.
    Returns the list of problems found.
    """
    problems = []

    if not TELEGRAM_BOT_TOKEN or is_placeholder(TELEGRAM_BOT_TOKEN):
        problems.append("TELEGRAM_BOT_TOKEN is missing or still a placeholder")
    # At least one model provider key must be set (GLM via Z.ai, or Gemini).
    zai_ok = ZAI_API_KEY and not is_placeholder(ZAI_API_KEY)
    gemini_ok = GEMINI_API_KEY and not is_placeholder(GEMINI_API_KEY)
    if not zai_ok and not gemini_ok:
        problems.append("No model API key set — provide ZAI_API_KEY and/or GEMINI_API_KEY")
    if not DATABASE_URL or is_placeholder(DATABASE_URL):
        problems.append("DATABASE_URL is missing or still a placeholder")
    if not webhook_url_is_usable():
        problems.append(
            f"WEBHOOK_URL is missing, still a placeholder, or not https "
            f"(current value: {WEBHOOK_URL or '<unset>'}). "
            "Telegram cannot deliver updates until this points at your deployed https base URL."
        )
    if WEBHOOK_SECRET_TOKEN == "super-secret-telegram-token":
        problems.append("WEBHOOK_SECRET_TOKEN is still the default value; set a long random string")
    if not ALLOWED_DM_USER_IDS:
        problems.append("ALLOWED_DM_USER_IDS is empty; nobody will be able to DM or activate the bot")

    # Not fatal — approval_recipient_id() falls back to ALLOWED_DM_USER_IDS[0] —
    # but worth a loud warning, since a silently-wrong fallback is exactly what
    # sent approvals to the group in the first place. See /status.
    if not MAIN_ACCOUNT_ID:
        print(
            "WARNING: MAIN_ACCOUNT_ID is not set. Approval prompts will fall back to "
            f"the first ALLOWED_DM_USER_IDS entry ({ALLOWED_DM_USER_IDS[0] if ALLOWED_DM_USER_IDS else 'none — approvals will fail closed'}). "
            "Run /set_main_account to fix this.",
            file=sys.stderr,
        )

    if problems:
        print("=" * 72, file=sys.stderr)
        print("CONFIGURATION ERROR — the bot cannot work correctly:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        if exit_on_error:
            raise SystemExit(1)

    return problems
