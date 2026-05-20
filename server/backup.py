"""DB backup to a Telegram chat.

When BACKUP_CHAT_ID is set, the scheduler's midnight tick uploads a
gzipped snapshot of the SQLite library to the configured chat — but
only if the DB has been written to since the last successful upload.
This collapses an entire day's worth of mutations into one upload
instead of one per mutation.

The dirty check is "DB mtime > last_backup_at": the file's modify
time on disk is the source of truth (survives process restarts, no
in-memory flag to lose), and the last-upload timestamp is persisted
next to the DB so it also survives restarts.

The snapshot uses sqlite3.Connection.backup(), safe under concurrent
writes.
"""

import gzip
import io
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from config import BACKUP_CHAT_ID, DATA_DIR
from library.db import DB_PATH

log = logging.getLogger(__name__)

_bot = None

# Persisted timestamp of the most recent successful upload. Cached on
# first read; rewritten on every send.
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


def has_pending_changes() -> bool:
    """True if the DB file has been written to since the last successful
    upload. Used by the scheduler to skip a tick when nothing changed."""
    try:
        mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return False
    last_ts = get_last_backup_at() or 0
    return mtime > last_ts


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


async def flush_if_dirty() -> bool:
    """Snapshot + upload if the DB has changed since the last upload.

    Returns True when an upload was sent, False otherwise. Wraps every
    failure mode so the scheduler's daily tick is robust to network
    blips and Telegram API hiccups.
    """
    if _bot is None or BACKUP_CHAT_ID is None:
        return False
    if not has_pending_changes():
        log.info("Backup: no changes since last upload; skipping daily tick")
        return False
    try:
        import asyncio
        data = await asyncio.to_thread(_snapshot)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"recipes_{ts}.db.gz"
        buf = io.BytesIO(data)
        await _bot.send_document(
            chat_id=BACKUP_CHAT_ID,
            document=buf,
            filename=filename,
            caption=f"ePepper daily backup · {len(data)} B gzipped",
        )
        _record_success(int(time.time()))
        log.info("Backup sent to chat %s (%d B gzipped)", BACKUP_CHAT_ID, len(data))
        return True
    except Exception:
        log.exception("Daily backup failed; will retry tomorrow")
        return False
