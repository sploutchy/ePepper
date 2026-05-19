"""DB backup to a Telegram chat.

When BACKUP_CHAT_ID is set, library mutations call schedule(); after a
short debounce window the SQLite file is snapshotted (online .backup()
API, safe under concurrent writes), gzipped, and sent as a document.

Debouncing collapses bursts like /save → /rate → /comment into one
upload, which keeps the backup chat tidy.
"""

import asyncio
import gzip
import io
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone

from config import BACKUP_CHAT_ID, BACKUP_DEBOUNCE_S
from library.db import DB_PATH

log = logging.getLogger(__name__)

_bot = None
_task: asyncio.Task | None = None


def set_bot(bot) -> None:
    """Wire the python-telegram-bot Bot instance used to send documents."""
    global _bot
    _bot = bot


def schedule() -> None:
    """Request a backup. Repeated calls within the debounce window collapse."""
    if BACKUP_CHAT_ID is None or _bot is None:
        return
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = asyncio.create_task(_run())


def _snapshot() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(f.name)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with open(f.name, "rb") as g:
            return gzip.compress(g.read())


async def _run() -> None:
    try:
        await asyncio.sleep(BACKUP_DEBOUNCE_S)
    except asyncio.CancelledError:
        return
    try:
        data = await asyncio.to_thread(_snapshot)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"recipes_{ts}.db.gz"
        buf = io.BytesIO(data)
        await _bot.send_document(
            chat_id=BACKUP_CHAT_ID,
            document=buf,
            filename=filename,
            caption=f"ePepper backup · {len(data)} B gzipped",
        )
        log.info("Backup sent to chat %s (%d B gzipped)", BACKUP_CHAT_ID, len(data))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Backup failed")
