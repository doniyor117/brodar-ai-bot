import os
import sys
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



# LLM configurations
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "glm-4.7-flash")

# Number of LLM API calls allowed in flight at once. The Z.ai free tier is
# concurrency limited, but 1 means a single slow chat blocks every other chat.
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "2"))

# Hard-enforce brodar's all-lowercase style in code, so a flash model slipping
# and capitalizing can't break character. URLs and code spans are preserved.
FORCE_LOWERCASE = os.getenv("FORCE_LOWERCASE", "true").strip().lower() in ("1", "true", "yes", "on")

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
    if not ZAI_API_KEY or is_placeholder(ZAI_API_KEY):
        problems.append("ZAI_API_KEY is missing or still a placeholder")
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

    if problems:
        print("=" * 72, file=sys.stderr)
        print("CONFIGURATION ERROR — the bot cannot work correctly:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        if exit_on_error:
            raise SystemExit(1)

    return problems
