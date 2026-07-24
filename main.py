import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional, Set

import httpx
import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException, status
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
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

# Log loudly if credentials are missing or still placeholders — but DO NOT exit.
# Crashing here just crash-loops the whole app; instead we stay up (so /health
# works and the webhook can be set/retried) and report problems in the logs.
config.validate_config(exit_on_error=False)

# Initialize Bot and Dispatcher.
# parse_mode is deliberately None: model output is arbitrary text and would
# routinely break HTML/Markdown parsing. Messages that need formatting pass
# parse_mode explicitly (see permissions.py).
bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)
dp = Dispatcher()

# Register bot handlers router
dp.include_router(bot_module.router)

# Strong references to in-flight background tasks. asyncio only holds a weak
# reference to a running task, so without this the garbage collector can
# cancel update processing mid-flight and messages vanish at random.
_background_tasks: Set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Creates a background task and keeps a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _keep_alive_loop():
    """
    Background keep-alive loop that periodically pings WEBHOOK_URL/health
    every 10 minutes (600 seconds) via HTTP request through Render's edge proxy
    to prevent the container from spinning down on Render free tier.
    """
    if not config.webhook_url_is_usable():
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

    # Initialize the Database connection pool and create the schema.
    # If this fails we log CRITICAL but keep the app running: crashing here would
    # take the whole bot offline (and, on free tier, leave it unwakeable). A
    # degraded bot that still answers beats a crash-loop. Chat settings/history
    # simply won't persist until the DB is reachable again.
    if config.DATABASE_URL:
        try:
            await db.init_db_pool()
            import memory
            await memory.sync_memory_from_db()
        except Exception as e:
            logger.critical(
                f"Failed to initialize the database during startup: {e}. "
                "Chat settings and history will NOT persist until DATABASE_URL works. "
                "The bot will still run in a degraded (in-memory) mode.",
                exc_info=True,
            )
    else:
        logger.warning("DATABASE_URL is not set. Database operations will be bypassed.")

    # Initialize bot details (caches bot username)
    await bot_module.init_bot_info(bot)

    # Set up Telegram webhook
    if config.webhook_url_is_usable():
        webhook_endpoint = f"{config.WEBHOOK_URL.rstrip('/')}/webhook"
        logger.info(f"Setting Telegram webhook to: {webhook_endpoint}")
        try:
            await bot.set_webhook(
                url=webhook_endpoint,
                secret_token=config.WEBHOOK_SECRET_TOKEN,
                # Keep updates that arrived while the container was spun down
                # (Render free tier sleeps constantly) instead of discarding them.
                drop_pending_updates=False,
                allowed_updates=dp.resolve_used_update_types()
            )
            info = await bot.get_webhook_info()
            logger.info(f"Telegram webhook set successfully. Pending updates: {info.pending_update_count}")
            if info.last_error_message:
                logger.warning(f"Telegram reports a previous webhook error: {info.last_error_message}")
        except Exception as e:
            # Log loudly but keep running. The app staying up means the webhook
            # can still be (re)set out of band, and /health stays reachable.
            logger.critical(f"Failed to set Telegram webhook: {e}", exc_info=True)
    else:
        logger.critical(
            f"WEBHOOK_URL is unusable (value: {config.WEBHOOK_URL or '<unset>'}). "
            "Telegram will NOT deliver any updates. Set it to your deployed https base URL."
        )

    # Launch background keep-alive ping loop
    keep_alive_task = _spawn_background(_keep_alive_loop())

    try:
        yield # Running app
    finally:
        # 2. Shutdown Logic
        logger.info("Shutting down Telegram AI Bot Service...")
        keep_alive_task.cancel()

        # Let in-flight updates finish before tearing down the bot session.
        pending = [t for t in _background_tasks if t is not keep_alive_task and not t.done()]
        if pending:
            logger.info(f"Waiting for {len(pending)} in-flight update(s) to finish...")
            await asyncio.wait(pending, timeout=10.0)

        # IMPORTANT: do NOT delete the webhook on shutdown.
        # On Render's free tier the container sleeps when idle, which triggers
        # this shutdown path. If we deleted the webhook here, Telegram would have
        # nowhere to deliver updates, so no inbound request would ever arrive to
        # wake the sleeping container — the bot would die permanently. Leaving the
        # webhook registered means Telegram's own delivery attempts wake the app.
        # The webhook is (re)set idempotently on startup, so it stays correct.
        logger.info("Leaving Telegram webhook registered so the app can wake on delivery.")

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
        _spawn_background(_safe_process_update(update))

        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}

async def _safe_process_update(update: Update):
    """Process a Telegram update in the background with full error handling."""
    try:
        await dp.feed_update(bot, update)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Error in background update processing: {e}", exc_info=True)

if __name__ == "__main__":
    # Start the server locally
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
