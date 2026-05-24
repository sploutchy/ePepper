"""Cached preview of the next Fooby weekly-inspiration recipe.

Pre-loaded by the midnight scheduler so the web status page can show
WHAT recipe will play tomorrow rather than a generic "Fooby will play"
hint. Persisted next to the SQLite library so the preview survives
container restarts.

The on-disk shape is a single-entry :class:`cache.disk.DiskCache` file
keyed by ``"pick"``; the stored value is a small dict:
  for_date: ISO `YYYY-MM-DD` the cached pick was prepared for
  url:      Fooby recipe URL — the same URL the midnight scheduler will
            push when the date arrives (so the preview matches reality)
  title:    Recipe title, pre-parsed so the status page renders without
            re-fetching Fooby on every request

Callers compare `for_date` against today/tomorrow themselves; the cache
intentionally doesn't auto-expire — a stale entry can still tell the
scheduler "this is what was decided last tick" if needed.

This module is a thin shim around :class:`cache.disk.DiskCache` that
keeps the historical ``get()`` / ``set_pick()`` API stable for the
scheduler and web callers.
"""

import logging
from datetime import date

from cache.disk import DiskCache

log = logging.getLogger(__name__)

_KEY = "pick"
_cache = DiskCache("fooby_cache.json")


def get() -> dict | None:
    """Return the cached pick or None if unset / malformed.

    Returns None on any IO or parse error (handled by the underlying
    :class:`DiskCache`) — the feature is a nicety, not
    correctness-critical, so a corrupt cache shouldn't break the status
    page. A subsequent successful write replaces it.
    """
    data = _cache.get(_KEY)
    if not isinstance(data, dict):
        return None
    if not data.get("for_date") or not data.get("url") or not data.get("title"):
        return None
    return data


def set_pick(for_date: date, url: str, title: str) -> None:
    """Persist a preview pick. Best-effort — IO failure is logged, never raised."""
    _cache.set(
        _KEY,
        {
            "for_date": for_date.isoformat(),
            "url": url,
            "title": title,
        },
    )
    log.info("Fooby cache updated: %s → %r", for_date.isoformat(), title)
