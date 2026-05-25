"""ePepper — e-ink recipe display server.

Runs the Telegram bot and FastAPI server concurrently.
"""

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from display import persistence as display_persistence
from display import state as display_state
from api.server import app as fastapi_app
from bot.handlers import create_bot
from library import init_db
from scheduler import (
    backfill_translations,
    initial_fooby_prefetch,
    midnight_loop,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# python-telegram-bot embeds the bot token in URLs; httpx logs full URLs at INFO.
# Bump httpx to WARNING so the token never lands in container logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("epepper")

# Env-derived config keys whose values are token-shaped and must be
# redacted by --print-config. Anything outside this list is printed verbatim.
_REDACTED_KEYS = ("TELEGRAM_BOT_TOKEN", "API_KEY", "LLM_API_KEY")


def _redact(value: str) -> str:
    """Return a token preview: first 4 + last 4 chars with an ellipsis,
    or a fixed marker when the value is too short or empty."""
    if not value:
        return "***unset***"
    if len(value) <= 8:
        return "***set***"
    return f"{value[:4]}…{value[-4:]}"


def _print_config() -> None:
    """Dump the effective env-derived config (one KEY=value per line),
    redacting token-shaped values. Mirrors the names defined in
    server/config.py so operators can sanity-check what the process
    actually sees."""
    import config

    keys = (
        "TELEGRAM_BOT_TOKEN",
        "ALLOWED_USERS",
        "API_HOST",
        "API_PORT",
        "API_KEY",
        "WEB_URL",
        "DATA_DIR",
        "TZ_NAME",
        "DEVICE_WAKE_HOUR_LOCAL",
        "BACKUP_CHAT_ID",
        "LLM_API_URL",
        "LLM_API_KEY",
        "LLM_TEXT_MODEL",
        "LLM_VISION_MODEL",
        "LLM_TRANSLATE_MODEL",
    )
    for key in keys:
        value = getattr(config, key, None)
        if key in _REDACTED_KEYS:
            rendered = _redact("" if value is None else str(value))
        elif value is None:
            rendered = "***unset***"
        else:
            rendered = str(value)
        # An empty ALLOWED_USERS means "reject everyone" — the most common
        # self-inflicted lockout. Flag it loudly in the dump.
        if key == "ALLOWED_USERS" and not value:
            rendered = (
                f"{rendered} (empty — bot rejects ALL users; "
                "alerts fall back to BACKUP_CHAT_ID)"
            )
        print(f"{key}={rendered}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="ePepper server (Telegram bot + FastAPI).",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the effective env-derived config (token values redacted) and exit.",
    )
    return parser.parse_args()


def _warn_if_alerts_have_no_destination() -> None:
    """Loud one-time startup warning for the two operator-hostile
    configurations created by the `_is_allowed` lockdown change:
    - ALLOWED_USERS empty AND BACKUP_CHAT_ID unset → bot rejects everyone
      AND low-battery alerts vanish.
    - ALLOWED_USERS empty but BACKUP_CHAT_ID set → bot still locked but
      alerts at least reach the backup chat.
    """
    from config import ALLOWED_USERS, BACKUP_CHAT_ID

    if not ALLOWED_USERS and BACKUP_CHAT_ID is None:
        log.warning(
            "ALLOWED_USERS is empty AND BACKUP_CHAT_ID is unset — "
            "the Telegram bot will reject every user, and low-battery "
            "alerts have no destination. Set at least one of these env vars."
        )
    elif not ALLOWED_USERS:
        log.warning(
            "ALLOWED_USERS is empty — bot is locked; alerts will go to "
            "BACKUP_CHAT_ID only."
        )


async def main() -> None:
    # Ensure data dir exists
    from config import DATA_DIR

    os.makedirs(DATA_DIR, exist_ok=True)

    # Initialise recipe library DB
    init_db()

    # Wire cross-layer dependencies that the lower layers can't import
    # without creating cycles:
    #   - display_state notifies display_persistence after every mutation
    #     so saved-recipe state survives a container restart.
    display_state.register_change_listener(display_persistence.persist_current)

    # Re-render whatever recipe was on the panel before the restart —
    # only kicks in for saved recipes (see display_persistence docstring).
    display_persistence.restore_on_startup()

    # Surface lockdown / alert-destination problems before the long-running
    # tasks start — easy to miss buried in steady-state logs.
    _warn_if_alerts_have_no_destination()

    # Start Telegram bot
    bot = create_bot()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started")

    # Background scheduler: the midnight daily-chores tick.
    midnight_task = asyncio.create_task(midnight_loop(), name="midnight_loop")
    # Populate the Fooby "Tomorrow" preview if the cache isn't current —
    # otherwise a fresh deploy waits up to 24 h before the status page
    # shows a concrete recipe. Fire-and-forget; failures are logged inside.
    prefetch_task = asyncio.create_task(
        initial_fooby_prefetch(), name="initial_fooby_prefetch"
    )
    # Backfill bilingual FTS keywords for any recipe that pre-dates the
    # translation pass — no-op once the library is fully indexed.
    translate_task = asyncio.create_task(
        backfill_translations(), name="backfill_translations"
    )

    # Start FastAPI server
    from config import API_HOST, API_PORT

    config = uvicorn.Config(
        fastapi_app,
        host=API_HOST,
        port=API_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    log.info("API server starting on %s:%s", config.host, config.port)

    try:
        await server.serve()
    finally:
        log.info("Shutting down...")
        for task in (midnight_task, prefetch_task, translate_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()


if __name__ == "__main__":
    args = _parse_args()
    if args.print_config:
        _print_config()
        sys.exit(0)
    asyncio.run(main())
