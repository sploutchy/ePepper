"""Unified cache layer (DES-9).

Two small, dependency-free backends sharing the same minimal interface
(`get` / `set` / `delete`, plus optional TTL via an ``{"value": ...,
"expires_at": ...}`` envelope):

- :class:`DiskCache` — atomic-write JSON file persistence. Used by
  :mod:`fooby_cache` to survive container restarts.
- :class:`LRUCache` — in-process OrderedDict-backed LRU. Currently
  unused; provided so a future PR can swap it in for the rendered-page
  cache in :mod:`display_state` without writing one from scratch.

Re-exported here so callers can ``from cache import DiskCache, LRUCache``
without reaching into the backend modules.
"""

from cache.disk import DiskCache
from cache.memory import LRUCache

__all__ = ["DiskCache", "LRUCache"]
