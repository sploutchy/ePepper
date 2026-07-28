"""Tests for the DB backup/restore subsystem.

Exercises the scheduler-facing helpers (has_pending_changes, _snapshot,
flush_if_dirty) plus the operator CLI (_cli_snapshot / _cli_restore /
_cli_status) against a throwaway DB per test.
"""

import asyncio
import gzip
import os
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

import backup


def _seed_db(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x INT)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    finally:
        conn.close()


class _Ctx:
    def __init__(self, tmp_path, db_path, last_file):
        self.tmp = tmp_path
        self.db_path = db_path
        self.last_file = last_file


@pytest.fixture
def isolated_backup(tmp_path, monkeypatch):
    """Fresh DB + fresh bookkeeping file per test, with all module-level
    state on `backup` reset (`_bot`, `BACKUP_CHAT_ID`, the get_last_backup_at
    cache) so tests don't leak into each other."""
    db_path = tmp_path / "recipes.db"
    _seed_db(db_path)
    last_file = tmp_path / "last_backup"
    monkeypatch.setattr(backup, "DB_PATH", str(db_path))
    monkeypatch.setattr(backup, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(backup, "_LAST_BACKUP_FILE", str(last_file))
    monkeypatch.setattr(backup, "_last_backup_at", None)
    monkeypatch.setattr(backup, "_last_backup_loaded", False)
    monkeypatch.setattr(backup, "_bot", None)
    monkeypatch.setattr(backup, "BACKUP_CHAT_ID", None)
    return _Ctx(tmp_path, db_path, last_file)


def _snap_bytes():
    """A tiny valid gzipped SQLite database, for _cli_restore inputs."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        conn = sqlite3.connect(tmp)
        conn.execute("CREATE TABLE t (x INT)")
        conn.commit()
        conn.close()
        with open(tmp, "rb") as g:
            return gzip.compress(g.read())
    finally:
        os.unlink(tmp)


# --- is_enabled / get_last_backup_at ----------------------------------------


def test_is_enabled_reflects_chat_id(isolated_backup, monkeypatch):
    assert backup.is_enabled() is False
    monkeypatch.setattr(backup, "BACKUP_CHAT_ID", 123)
    assert backup.is_enabled() is True


def test_get_last_backup_at_missing_file(isolated_backup):
    assert backup.get_last_backup_at() is None


def test_get_last_backup_at_reads_file(isolated_backup):
    isolated_backup.last_file.write_text("1234567890")
    assert backup.get_last_backup_at() == 1234567890


def test_get_last_backup_at_garbage_is_none(isolated_backup):
    isolated_backup.last_file.write_text("not-a-number")
    assert backup.get_last_backup_at() is None


def test_get_last_backup_at_is_cached(isolated_backup):
    isolated_backup.last_file.write_text("100")
    assert backup.get_last_backup_at() == 100
    isolated_backup.last_file.write_text("999")
    assert backup.get_last_backup_at() == 100


# --- has_pending_changes -----------------------------------------------------


def test_has_pending_changes_no_db(isolated_backup):
    os.remove(isolated_backup.db_path)
    assert backup.has_pending_changes() is False


def test_has_pending_changes_no_prior_backup(isolated_backup):
    assert backup.has_pending_changes() is True


def test_has_pending_changes_after_backup(isolated_backup):
    watermark = int(os.path.getmtime(isolated_backup.db_path)) + 10
    isolated_backup.last_file.write_text(str(watermark))
    assert backup.has_pending_changes() is False


def test_has_pending_changes_write_after_backup(isolated_backup):
    isolated_backup.last_file.write_text(str(int(time.time()) - 100))
    backup.get_last_backup_at()  # prime the cache
    os.utime(isolated_backup.db_path, None)
    assert backup.has_pending_changes() is True


# --- _snapshot ---------------------------------------------------------------


def test_snapshot_produces_gzipped_sqlite(isolated_backup):
    blob = backup._snapshot()
    raw = gzip.decompress(blob)
    assert raw.startswith(b"SQLite format 3\x00")


# --- flush_if_dirty ----------------------------------------------------------


def test_flush_if_dirty_no_bot(isolated_backup):
    assert asyncio.run(backup.flush_if_dirty()) is False


def test_flush_if_dirty_no_chat(isolated_backup, monkeypatch):
    monkeypatch.setattr(backup, "_bot", AsyncMock())
    assert asyncio.run(backup.flush_if_dirty()) is False


def test_flush_if_dirty_clean_db_skips(isolated_backup, monkeypatch):
    monkeypatch.setattr(backup, "_bot", AsyncMock())
    monkeypatch.setattr(backup, "BACKUP_CHAT_ID", 42)
    watermark = int(os.path.getmtime(isolated_backup.db_path)) + 10
    isolated_backup.last_file.write_text(str(watermark))
    assert asyncio.run(backup.flush_if_dirty()) is False


def test_flush_if_dirty_uploads_and_stamps(isolated_backup, monkeypatch):
    bot = AsyncMock()
    monkeypatch.setattr(backup, "_bot", bot)
    monkeypatch.setattr(backup, "BACKUP_CHAT_ID", 42)

    before = int(time.time())
    assert asyncio.run(backup.flush_if_dirty()) is True
    after = int(time.time())

    bot.send_document.assert_awaited_once()
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["filename"].startswith("recipes_") and kwargs["filename"].endswith(".db.gz")
    assert before <= backup.get_last_backup_at() <= after
    assert isolated_backup.last_file.exists()


def test_flush_if_dirty_watermark_stamped_before_snapshot(isolated_backup, monkeypatch):
    """A write landing DURING the snapshot must still register as pending
    on the next tick. Simulate that by bumping mtime past the recorded
    watermark and asserting has_pending_changes flips back to True."""
    bot = AsyncMock()
    monkeypatch.setattr(backup, "_bot", bot)
    monkeypatch.setattr(backup, "BACKUP_CHAT_ID", 42)

    assert asyncio.run(backup.flush_if_dirty()) is True
    ts = backup.get_last_backup_at()
    os.utime(isolated_backup.db_path, (ts + 5, ts + 5))
    assert backup.has_pending_changes() is True


def test_flush_if_dirty_swallows_send_errors(isolated_backup, monkeypatch):
    bot = AsyncMock()
    bot.send_document.side_effect = RuntimeError("network")
    monkeypatch.setattr(backup, "_bot", bot)
    monkeypatch.setattr(backup, "BACKUP_CHAT_ID", 42)

    assert asyncio.run(backup.flush_if_dirty()) is False
    assert backup.get_last_backup_at() is None


# --- _cli_snapshot -----------------------------------------------------------


def test_cli_snapshot_writes_gzipped_db(isolated_backup, capsys):
    assert backup._cli_snapshot() == 0
    files = list(isolated_backup.tmp.glob("recipes_*.db.gz"))
    assert len(files) == 1
    raw = gzip.decompress(files[0].read_bytes())
    assert raw.startswith(b"SQLite format 3\x00")
    assert "wrote " in capsys.readouterr().out


# --- _cli_restore ------------------------------------------------------------


def test_cli_restore_missing_file(isolated_backup, tmp_path, capsys):
    rc = backup._cli_restore(str(tmp_path / "nope.db.gz"), assume_yes=True)
    assert rc == 1
    assert "not a readable file" in capsys.readouterr().err


def test_cli_restore_wrong_suffix(isolated_backup, tmp_path, capsys):
    p = tmp_path / "snap.txt"
    p.write_bytes(b"whatever")
    rc = backup._cli_restore(str(p), assume_yes=True)
    assert rc == 1
    assert ".db.gz suffix" in capsys.readouterr().err


def test_cli_restore_bad_gzip(isolated_backup, tmp_path, capsys):
    p = tmp_path / "snap.db.gz"
    p.write_bytes(b"not gzipped at all")
    rc = backup._cli_restore(str(p), assume_yes=True)
    assert rc == 1
    assert "cannot gunzip" in capsys.readouterr().err


def test_cli_restore_missing_sqlite_magic(isolated_backup, tmp_path, capsys):
    p = tmp_path / "snap.db.gz"
    p.write_bytes(gzip.compress(b"NOT-A-SQLITE-FILE"))
    rc = backup._cli_restore(str(p), assume_yes=True)
    assert rc == 1
    assert "magic header missing" in capsys.readouterr().err


def test_cli_restore_happy_path_removes_wal_shm(isolated_backup, tmp_path):
    dst = isolated_backup.db_path
    wal = dst.with_name("recipes.db-wal")
    shm = dst.with_name("recipes.db-shm")
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    snap = tmp_path / "snap.db.gz"
    snap.write_bytes(_snap_bytes())

    rc = backup._cli_restore(str(snap), assume_yes=True)
    assert rc == 0
    assert dst.read_bytes().startswith(b"SQLite format 3\x00")
    assert not wal.exists()
    assert not shm.exists()


def test_cli_restore_confirm_no_leaves_db_untouched(isolated_backup, tmp_path, monkeypatch):
    snap = tmp_path / "snap.db.gz"
    snap.write_bytes(_snap_bytes())
    original = isolated_backup.db_path.read_bytes()

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    rc = backup._cli_restore(str(snap), assume_yes=False)
    assert rc == 1
    assert isolated_backup.db_path.read_bytes() == original


def test_cli_restore_confirm_yes_overwrites(isolated_backup, tmp_path, monkeypatch):
    snap = tmp_path / "snap.db.gz"
    snap.write_bytes(_snap_bytes())

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    rc = backup._cli_restore(str(snap), assume_yes=False)
    assert rc == 0
    assert isolated_backup.db_path.read_bytes().startswith(b"SQLite format 3\x00")


def test_cli_restore_eof_declines(isolated_backup, tmp_path, monkeypatch):
    snap = tmp_path / "snap.db.gz"
    snap.write_bytes(_snap_bytes())
    original = isolated_backup.db_path.read_bytes()

    def _raise(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    rc = backup._cli_restore(str(snap), assume_yes=False)
    assert rc == 1
    assert isolated_backup.db_path.read_bytes() == original


# --- _cli_status -------------------------------------------------------------


def test_cli_status_never_backed_up(isolated_backup, capsys):
    assert backup._cli_status() == 0
    out = capsys.readouterr().out
    assert "last_backup_at=never" in out
    assert "has_pending_changes=True" in out


def test_cli_status_with_prior_backup(isolated_backup, capsys):
    watermark = int(os.path.getmtime(isolated_backup.db_path)) + 10
    isolated_backup.last_file.write_text(str(watermark))
    assert backup._cli_status() == 0
    out = capsys.readouterr().out
    assert "last_backup_at=" in out and "never" not in out
    assert "has_pending_changes=False" in out
