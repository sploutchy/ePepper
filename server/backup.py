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

import argparse
import gzip
import io
import logging
import os
import pathlib
import sqlite3
import sys
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


# ---------------------------------------------------------------------------
# CLI — operator-facing one-shot commands. Intentionally narrow: snapshot
# writes a local .db.gz (no network), restore replaces the live DB from
# such a file (caller is responsible for stopping/starting the container),
# status prints a quick read of the backup bookkeeping.
# ---------------------------------------------------------------------------

# SQLite databases start with this 16-byte magic string — see
# https://www.sqlite.org/fileformat.html. We use it to refuse restoring
# anything that isn't a real DB (e.g. an HTML error page, a truncated
# download, a wrong file the operator typo'd).
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _cli_snapshot() -> int:
    """One-shot snapshot to <DATA_DIR>/recipes_<UTC>.db.gz. Reuses the same
    _snapshot() the scheduler uses, then writes the bytes to disk instead
    of uploading them. Returns a process exit code."""
    try:
        data = _snapshot()
    except Exception as e:
        print(f"snapshot failed: {e}", file=sys.stderr)
        return 1
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = pathlib.Path(DATA_DIR) / f"recipes_{ts}.db.gz"
    try:
        out.write_bytes(data)
    except OSError as e:
        print(f"snapshot failed: {e}", file=sys.stderr)
        return 1
    print(f"wrote {out} ({len(data)} B gzipped)")
    return 0


def _cli_restore(path_str: str) -> int:
    """Replace the live DB with the contents of a .db.gz snapshot. Refuses
    anything that doesn't gunzip to a SQLite-magic-prefixed blob. Removes
    the WAL/SHM sidecar files so SQLite doesn't replay stale journal data
    on top of the freshly restored file."""
    print("Stop the epepper container before running this.")
    src = pathlib.Path(path_str)
    if not src.is_file():
        print(f"restore failed: {src} is not a readable file", file=sys.stderr)
        return 1
    if not src.name.endswith(".db.gz"):
        print(f"restore failed: {src} does not have a .db.gz suffix", file=sys.stderr)
        return 1
    try:
        raw = gzip.decompress(src.read_bytes())
    except (OSError, gzip.BadGzipFile) as e:
        print(f"restore failed: cannot gunzip {src}: {e}", file=sys.stderr)
        return 1
    if not raw.startswith(_SQLITE_MAGIC):
        print(
            f"restore failed: {src} does not look like a SQLite database "
            "(magic header missing)",
            file=sys.stderr,
        )
        return 1
    dst = pathlib.Path(DB_PATH)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
    except OSError as e:
        print(f"restore failed: cannot write {dst}: {e}", file=sys.stderr)
        return 1
    # WAL/SHM left over from the previous DB would let SQLite replay stale
    # journal pages on top of the restored file. Remove them now.
    for sidecar in (dst.with_name("recipes.db-wal"), dst.with_name("recipes.db-shm")):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"warning: could not remove {sidecar}: {e}", file=sys.stderr)
    print(f"restored {dst} ({len(raw)} B uncompressed)")
    print("Start the container now.")
    return 0


def _cli_status() -> int:
    """Print last-backup timestamp + size, current DB size, and pending-changes flag."""
    last = get_last_backup_at()
    if last is None:
        last_str = "never"
        last_size_str = "n/a"
    else:
        last_str = datetime.fromtimestamp(last, tz=timezone.utc).isoformat()
        # We don't keep a per-snapshot size; the bookkeeping file only stores
        # the timestamp. Report n/a rather than re-snapshot just for a number.
        last_size_str = "n/a"
    try:
        db_size = os.path.getsize(DB_PATH)
        db_size_str = f"{db_size} B"
    except OSError:
        db_size_str = "n/a (DB missing)"
    print(f"last_backup_at={last_str}")
    print(f"last_backup_size={last_size_str}")
    print(f"db_size={db_size_str}")
    print(f"has_pending_changes={has_pending_changes()}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="ePepper DB backup operator CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="Write a one-shot .db.gz snapshot to DATA_DIR.")
    p_restore = sub.add_parser(
        "restore",
        help="Restore the live DB from a .db.gz snapshot (stop the container first).",
    )
    p_restore.add_argument("path", help="Path to a .db.gz snapshot.")
    sub.add_parser("status", help="Print last-backup info and pending-changes flag.")
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    if args.cmd == "snapshot":
        sys.exit(_cli_snapshot())
    if args.cmd == "restore":
        sys.exit(_cli_restore(args.path))
    if args.cmd == "status":
        sys.exit(_cli_status())
    sys.exit(2)
