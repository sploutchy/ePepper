"""DB backup to a Telegram chat.

When BACKUP_CHAT_ID is set, library mutations call schedule(); after a
short debounce window the SQLite file is snapshotted (online .backup()
API, safe under concurrent writes), gzipped, and sent as a document.

Debouncing collapses bursts like /save → /rate → /comment into one
upload, which keeps the backup chat tidy.

The timestamp of the most recent successful upload is persisted next to
the DB so /status can report it across container restarts.
"""

import asyncio
import gzip
import io
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from config import BACKUP_CHAT_ID, BACKUP_DEBOUNCE_S, DATA_DIR
from library.db import DB_PATH

log = logging.getLogger(__name__)

_bot = None
_task: asyncio.Task | None = None

# Persisted timestamp of the most recent successful upload. We cache the
# read so /status doesn't touch the FS on every refresh, and rewrite on
# every send.
_LAST_BACKUP_FILE = os.path.join(DATA_DIR, "last_backup")
_last_backup_at: int | None = None
_last_backup_loaded = False


def is_enabled() -> bool:
    """True iff backups are configured. Used by /status to hide the row
    entirely when the user hasn't wired up a backup chat."""
    return BACKUP_CHAT_ID is not None


def get_last_backup_at() -> int | None:
    """Unix seconds of the last successful upload, or None if never. Reads
    from disk once per process; subsequent calls hit the cache. Returns
    None if the file is missing, unreadable, or contains garbage."""
    global _last_backup_at, _last_backup_loaded
    if not _last_backup_loaded:
        try:
            with open(_LAST_BACKUP_FILE) as f:
                _last_backup_at = int(f.read().strip())
        except (FileNotFoundError, ValueError, OSError):
            _last_backup_at = None
        _last_backup_loaded = True
    return _last_backup_at


def _record_success(ts: int) -> None:
    global _last_backup_at, _last_backup_loaded
    _last_backup_at = ts
    _last_backup_loaded = True
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_LAST_BACKUP_FILE, "w") as f:
            f.write(str(ts))
    except OSError:
        log.exception("Failed to persist last-backup timestamp")


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
        _record_success(int(time.time()))
        log.info("Backup sent to chat %s (%d B gzipped)", BACKUP_CHAT_ID, len(data))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Backup failed")
