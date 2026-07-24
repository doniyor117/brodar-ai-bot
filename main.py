import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException, status
from aiogram import Bot, Dispatcher
from aiogram.types import Update

import config
import db
import bot as bot_module

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Validate that required configuration is present
config.validate_config()

# Initialize Bot and Dispatcher
# Using HTML parse mode for nice message formatting if needed
bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Register bot handlers router
dp.include_router(bot_module.router)

import asyncio
import httpx

async def _keep_alive_loop():
    """
    Background keep-alive loop that periodically pings WEBHOOK_URL/health
    every 10 minutes (600 seconds) via HTTP request through Render's edge proxy
    to prevent the container from spinning down on Render free tier.
    """
    if not config.WEBHOOK_URL or "CHANGE_ME" in config.WEBHOOK_URL:
        logger.info("WEBHOOK_URL is not configured with a valid domain. Self keep-alive loop disabled.")
        return

    health_url = f"{config.WEBHOOK_URL.rstrip('/')}/health"
    logger.info(f"Starting background keep-alive ping loop targeting {health_url} (every 10m)...")

    # Initial delay before first ping
    await asyncio.sleep(60)

    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(health_url)
                logger.info(f"Keep-alive ping to {health_url} returned HTTP status {res.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

        await asyncio.sleep(600)  # Ping every 10 minutes

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI Lifespan event manager handling startup and shutdown."""
    # 1. Startup Logic
    logger.info("Starting up Telegram AI Bot Service...")
    
    # Initialize the Database connection pool
    if config.DATABASE_URL:
        try:
            await db.init_db_pool()
            import memory
            await memory.sync_memory_from_db()
        except Exception as e:
            logger.error(f"Failed to initialize database pool during startup: {e}")
    else:
        logger.warning("DATABASE_URL is not set. Database operations will be bypassed.")


    # Initialize bot details (caches bot username)
    await bot_module.init_bot_info(bot)

    # Set up Telegram webhook
    if config.WEBHOOK_URL:
        webhook_endpoint = f"{config.WEBHOOK_URL}/webhook"
        logger.info(f"Setting Telegram webhook to: {webhook_endpoint}")
        try:
            await bot.set_webhook(
                url=webhook_endpoint,
                secret_token=config.WEBHOOK_SECRET_TOKEN,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types()
            )
            logger.info("Telegram webhook set successfully.")
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}")
    else:
        logger.warning("WEBHOOK_URL is not set. Webhook registration skipped.")

    # Launch background keep-alive ping loop
    keep_alive_task = asyncio.create_task(_keep_alive_loop())

    try:
        yield # Running app
    finally:
        # 2. Shutdown Logic
        logger.info("Shutting down Telegram AI Bot Service...")
        keep_alive_task.cancel()

        # Remove Telegram Webhook
        try:
            logger.info("Deleting Telegram webhook...")
            await bot.delete_webhook()
        except Exception as e:
            logger.error(f"Failed to delete Telegram webhook: {e}")

        # Close aiogram bot session
        logger.info("Closing bot session...")
        await bot.session.close()

        # Close database connection pool
        await db.close_db_pool()
        logger.info("Shutdown process complete.")


# Initialize FastAPI App
app = FastAPI(
    title="Telegram AI Agent Bot Server",
    description="FastAPI Webhook Server for Telegram AI Agent Bot",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health")
async def health_check():
    """
    DB-free health check endpoint.
    Used by UptimeRobot or Render to keep the web service container awake
    without querying Postgres, allowing the Neon database to scale to zero.
    """
    return {"status": "ok", "service": "telegram-ai-agent-bot"}

@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None, alias="X-Telegram-Bot-Api-Secret-Token")
):
    """
    Telegram Webhook endpoint.
    Validates the custom secret token header and forwards updates to aiogram dispatcher.
    CRITICAL: Processes updates in a background task so the webhook returns 200 immediately.
    Without this, LLM calls (5-30s) block the webhook response, causing Telegram to
    timeout and stop delivering all future updates.
    """
    # 1. Verify Secret Token Header for security
    if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != config.WEBHOOK_SECRET_TOKEN:
        logger.warning("Unauthorized webhook request. Secret token mismatch or missing.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Invalid secret token"
        )

    # 2. Parse and validate the update, then process in background
    try:
        update_json = await request.json()
        update = Update.model_validate(update_json, context={"bot": bot})
        
        # Process update in a background task — return 200 to Telegram IMMEDIATELY.
        # This is critical: if we `await` dp.feed_update here, LLM generation
        # blocks the webhook response for 5-30s. Telegram's webhook timeout
        # triggers retries, and eventually Telegram stops sending updates entirely.
        asyncio.create_task(_safe_process_update(update))
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}

async def _safe_process_update(update: Update):
    """Process a Telegram update in the background with full error handling."""
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Error in background update processing: {e}", exc_info=True)

if __name__ == "__main__":
    # Start the server locally
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
