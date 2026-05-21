"""Cached preview of the next Fooby weekly-inspiration recipe.

Pre-loaded by the midnight scheduler so the web status page can show
WHAT recipe will play tomorrow rather than a generic "Fooby will play"
hint. Persisted next to the SQLite library so the preview survives
container restarts.

The file is a small JSON object:
  for_date: ISO `YYYY-MM-DD` the cached pick was prepared for
  url:      Fooby recipe URL — the same URL the midnight scheduler will
            push when the date arrives (so the preview matches reality)
  title:    Recipe title, pre-parsed so the status page renders without
            re-fetching Fooby on every request

Callers compare `for_date` against today/tomorrow themselves; the cache
intentionally doesn't auto-expire — a stale entry can still tell the
scheduler "this is what was decided last tick" if needed.
"""

import json
import logging
import os
from datetime import date

from config import DATA_DIR

log = logging.getLogger(__name__)

_FILE = os.path.join(DATA_DIR, "fooby_cache.json")


def get() -> dict | None:
    """Return the cached pick or None if the file is missing / unreadable.

    Returns None on any IO or parse error — the feature is a nicety, not
    correctness-critical, so a corrupt cache shouldn't break the status
    page. A subsequent successful write replaces it.
    """
    try:
        with open(_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("for_date") or not data.get("url") or not data.get("title"):
        return None
    return data


def set_pick(for_date: date, url: str, title: str) -> None:
    """Persist a preview pick. Best-effort — IO failure is logged, never raised."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_FILE, "w") as f:
            json.dump(
                {
                    "for_date": for_date.isoformat(),
                    "url": url,
                    "title": title,
                },
                f,
            )
        log.info("Fooby cache updated: %s → %r", for_date.isoformat(), title)
    except OSError:
        log.exception("Failed to write Fooby cache")
