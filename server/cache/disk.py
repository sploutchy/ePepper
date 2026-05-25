"""Minimal JSON-file-backed key/value store with atomic writes.

The whole cache lives in one JSON object on disk: ``{key: value}``.
Keeping everything in a single file (instead of one file per key)
matches how the existing :mod:`fooby_cache` already works and keeps the
on-disk footprint trivially backup-able.

Writes go through a ``.tmp`` sibling + ``fsync`` + ``os.replace`` so a
crash mid-write can never corrupt the live file — the worst case is a
stray ``.tmp`` that the next write overwrites.

Access is serialized with a :class:`threading.Lock`. The existing
fooby_cache call sites are synchronous and infrequent (scheduler tick +
status page render), so a single coarse lock is plenty; we trade
parallelism for an obviously-correct implementation.
"""

import json
import logging
import os
import threading
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)


class DiskCache:
    """JSON-file key/value store backed by a single file under ``DATA_DIR``.

    Parameters
    ----------
    path:
        Filename (or relative path) inside ``DATA_DIR``. Resolved to an
        absolute path once at construction.
    """

    def __init__(self, path: str) -> None:
        self._path = os.path.join(DATA_DIR, path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ IO
    def _load(self) -> dict[str, Any]:
        """Read the on-disk file. Returns ``{}`` on any IO / parse error.

        The cache is a nicety, not correctness-critical: a corrupt file
        shouldn't break callers. The next successful ``set`` overwrites
        it cleanly.
        """
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _dump(self, data: dict[str, Any]) -> None:
        """Atomically replace the on-disk file with ``data``.

        Write to ``<path>.tmp``, ``fsync`` the file descriptor, then
        ``os.replace`` into place — replace is atomic on POSIX, so a
        reader either sees the old file or the new one, never a torn
        write.
        """
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    # ------------------------------------------------------------- public
    def get(self, key: str) -> Any | None:
        """Return the cached value for ``key`` or ``None`` if missing."""
        with self._lock:
            data = self._load()
            return data.get(key)

    def set(self, key: str, value: Any) -> None:
        """Persist ``value`` under ``key``. Best-effort — IO errors are logged."""
        with self._lock:
            data = self._load()
            data[key] = value
            try:
                self._dump(data)
            except OSError:
                log.exception("Failed to write disk cache %s", self._path)
