"""In-process LRU cache with optional TTL.

Currently unused — :mod:`display_state` still uses an ad-hoc dict for
its rendered-page cache. This module exists as a drop-in target for a
future PR that wants real bounded LRU semantics there (and for any
parsed-URL recipe cache we might add) without having to write one from
scratch.

# TODO(DES-9): wire this into the recipe path (parsed-URL → recipe dict)
# so a repeat parse within the process lifetime skips the fetch + LLM
# round-trip. Probably belongs next to processing/recipes.py with a
# small `maxsize` (a few dozen entries) and a multi-hour TTL.
"""

from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from typing import Any


class LRUCache:
    """OrderedDict-backed LRU with optional TTL.

    Parameters
    ----------
    maxsize:
        Maximum number of entries kept. When exceeded, the
        least-recently-used entry is evicted. Must be >= 1.
    ttl_seconds:
        Same shape as :class:`cache.disk.DiskCache` — values are wrapped
        in a ``{"value", "expires_at"}`` envelope on ``set`` and expired
        entries surface as ``None`` from ``get`` (and are evicted on
        access).
    """

    def __init__(self, maxsize: int, ttl_seconds: float | None = None) -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = Lock()

    def _wrap(self, value: Any) -> Any:
        if self._ttl_seconds is None:
            return value
        expires_at = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + self._ttl_seconds,
            tz=timezone.utc,
        )
        return {"value": value, "expires_at": expires_at.isoformat()}

    def _unwrap(self, envelope: Any) -> tuple[bool, Any]:
        """Return ``(alive, value)``. ``alive=False`` means the entry expired."""
        if (
            isinstance(envelope, dict)
            and "value" in envelope
            and "expires_at" in envelope
        ):
            try:
                expires_at = datetime.fromisoformat(envelope["expires_at"])
            except (TypeError, ValueError):
                return False, None
            if expires_at <= datetime.now(timezone.utc):
                return False, None
            return True, envelope["value"]
        return True, envelope

    def get(self, key: str) -> Any | None:
        """Return the cached value, refreshing recency. Returns ``None`` if missing/expired."""
        with self._lock:
            if key not in self._data:
                return None
            envelope = self._data[key]
            alive, value = self._unwrap(envelope)
            if not alive:
                # Drop expired entries on access so they don't linger
                # taking up an LRU slot.
                del self._data[key]
                return None
            # Move to the most-recently-used end.
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        """Insert / update ``key``. Evicts the LRU entry if over capacity."""
        with self._lock:
            if key in self._data:
                # Overwrite without changing size; bump recency.
                self._data[key] = self._wrap(value)
                self._data.move_to_end(key)
                return
            self._data[key] = self._wrap(value)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        """Remove ``key`` if present. No-op when absent."""
        with self._lock:
            self._data.pop(key, None)
