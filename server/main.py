"""ePepper — e-ink recipe display server.

Runs the Telegram bot and FastAPI server concurrently.
"""

import asyncio
import logging
import os

import uvicorn

from api.server import app as fastapi_app
from bot.handlers import create_bot
from library import init_db
from scheduler import anniversary_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# python-telegram-bot embeds the bot token in URLs; httpx logs full URLs at INFO.
# Bump httpx to WARNING so the token never lands in container logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("epepper")


async def main() -> None:
    # Ensure data dir exists
    os.makedirs("/app/data", exist_ok=True)

    # Initialise recipe library DB
    init_db()

    # Start Telegram bot
    bot = create_bot()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started")

    # Background scheduler: midnight anniversary swap
    anniversary_task = asyncio.create_task(anniversary_loop(), name="anniversary_loop")

    # Start FastAPI server
    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=int(os.environ.get("API_PORT", "8080")),
        log_level="info",
    )
    server = uvicorn.Server(config)
    log.info("API server starting on :%s", config.port)

    try:
        await server.serve()
    finally:
        log.info("Shutting down...")
        anniversary_task.cancel()
        try:
            await anniversary_task
        except (asyncio.CancelledError, Exception):
            pass
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
