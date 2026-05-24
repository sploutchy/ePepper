"""JSON-file-backed cache with atomic writes and optional TTL.

The whole cache lives in one JSON object on disk: ``{key: envelope}``
where each envelope is either the raw value or, when ``ttl_seconds`` is
set on the cache, ``{"value": ..., "expires_at": <iso8601>}``. Keeping
everything in a single file (instead of one file per key) matches how
the existing :mod:`fooby_cache` already works and keeps the on-disk
footprint trivially backup-able.

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
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiskCache:
    """JSON-file cache backed by a single file under ``DATA_DIR``.

    Parameters
    ----------
    path:
        Filename (or relative path) inside ``DATA_DIR``. Resolved to an
        absolute path once at construction.
    ttl_seconds:
        When set, ``set`` wraps values in a ``{"value", "expires_at"}``
        envelope and ``get`` returns ``None`` past expiry. When ``None``
        (default), values are stored verbatim and never expire.
    """

    def __init__(self, path: str, ttl_seconds: float | None = None) -> None:
        self._path = os.path.join(DATA_DIR, path)
        self._ttl_seconds = ttl_seconds
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

    # ------------------------------------------------------------ envelope
    def _unwrap(self, envelope: Any) -> Any:
        """Return the inner value, or ``None`` if the envelope has expired.

        Non-TTL caches store values verbatim, so anything that doesn't
        look like a TTL envelope is returned as-is. This means a caller
        who legitimately stored a dict with both ``value`` and
        ``expires_at`` keys would be misinterpreted — acceptable given
        the envelope shape is an internal contract.
        """
        if (
            isinstance(envelope, dict)
            and "value" in envelope
            and "expires_at" in envelope
        ):
            try:
                expires_at = datetime.fromisoformat(envelope["expires_at"])
            except (TypeError, ValueError):
                return None
            if expires_at <= datetime.now(timezone.utc):
                return None
            return envelope["value"]
        return envelope

    def _wrap(self, value: Any) -> Any:
        if self._ttl_seconds is None:
            return value
        expires_at = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + self._ttl_seconds,
            tz=timezone.utc,
        )
        return {"value": value, "expires_at": expires_at.isoformat()}

    # ------------------------------------------------------------- public
    def get(self, key: str) -> Any | None:
        """Return the cached value for ``key`` or ``None`` if missing/expired."""
        with self._lock:
            data = self._load()
            if key not in data:
                return None
            return self._unwrap(data[key])

    def set(self, key: str, value: Any) -> None:
        """Persist ``value`` under ``key``. Best-effort — IO errors are logged."""
        with self._lock:
            data = self._load()
            data[key] = self._wrap(value)
            try:
                self._dump(data)
            except OSError:
                log.exception("Failed to write disk cache %s", self._path)

    def delete(self, key: str) -> None:
        """Remove ``key`` if present. No-op when absent."""
        with self._lock:
            data = self._load()
            if key not in data:
                return
            del data[key]
            try:
                self._dump(data)
            except OSError:
                log.exception("Failed to write disk cache %s", self._path)

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            try:
                self._dump({})
            except OSError:
                log.exception("Failed to clear disk cache %s", self._path)

    def keys(self) -> list[str]:
        """Return currently-stored keys (skipping expired TTL entries)."""
        with self._lock:
            data = self._load()
            if self._ttl_seconds is None:
                return list(data.keys())
            # Filter out expired entries so callers don't see ghost keys.
            return [k for k, v in data.items() if self._unwrap(v) is not None]
