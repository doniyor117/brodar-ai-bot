import os
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

# Database configurations
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Web server configurations
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

def validate_config():
    """Validates that all required environment variables are set."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not ZAI_API_KEY:
        missing.append("ZAI_API_KEY")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not WEBHOOK_URL:
        missing.append("WEBHOOK_URL")
    
    if missing:
        print(f"WARNING: The following environment variables are missing: {', '.join(missing)}")
