"""Unified cache layer (DES-9).

A small, dependency-free backend with a minimal interface (`get` / `set`
/ `delete`, plus optional TTL via an ``{"value": ..., "expires_at": ...}``
envelope):

- :class:`DiskCache` — atomic-write JSON file persistence. Used by
  :mod:`fooby_cache` to survive container restarts.

Re-exported here so callers can ``from cache import DiskCache`` without
reaching into the backend module.
"""

from cache.disk import DiskCache

__all__ = ["DiskCache"]
