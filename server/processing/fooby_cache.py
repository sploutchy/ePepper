"""Cached weekly snapshot of Fooby's inspiration recipes.

Fooby's "Inspirations de la semaine" block can be edited any day of the
week, and each edit reshuffles or replaces some of the recipes. The
scheduler's `today.weekday() % len(urls)` rotation is only collision-free
when the list is stable for a full week, so we snapshot the list once
per ISO week (Monday) and reuse that frozen list until the next Monday.
Within the week the display is immune to mid-week reshuffles; Fooby's
edits are picked up on the following Monday's tick.

Titles for the URLs are resolved lazily by the midnight prefetch (one
per day, for the URL that will play tomorrow) so the status page's
"Tomorrow" card can render without re-fetching Fooby on every request.

The on-disk shape is a single-entry :class:`cache.disk.DiskCache` file
keyed by ``"week"``; the stored value is::

  week_start: ISO `YYYY-MM-DD` of the Monday this snapshot covers
  urls:       Fooby recipe URLs, in the order they appeared on the page
  titles:     Pre-parsed titles keyed by URL — filled in incrementally
              as the prefetch resolves each day's pick

Callers use `pick_url(day)` and `preview_for(day)` to project the
snapshot onto a specific date; both return None when the snapshot
doesn't cover that day's ISO week, so a stale entry from a previous
week is silently ignored (the next `set_week` replaces it).
"""

import logging
from datetime import date, timedelta

from cache.disk import DiskCache

log = logging.getLogger(__name__)

_KEY = "week"
_cache = DiskCache("fooby_cache.json")


def week_start_of(day: date) -> date:
    """Monday of the ISO week containing `day`."""
    return day - timedelta(days=day.weekday())


def get_week() -> dict | None:
    """Return the stored snapshot dict, or None if unset / malformed.

    Malformed here includes an old-format entry from before the weekly
    snapshot switch — it has no `week_start`/`urls`, so it fails the
    shape check and the next `set_week` overwrites it. Callers are
    expected to compare `week_start` against the ISO Monday they care
    about themselves; this getter does not filter by date.
    """
    data = _cache.get(_KEY)
    if not isinstance(data, dict):
        return None
    if not data.get("week_start") or not isinstance(data.get("urls"), list):
        return None
    if not isinstance(data.get("titles"), dict):
        # Tolerate a snapshot written without titles yet — normalise so
        # callers can always `.get("titles", {}).get(url)` safely.
        data = {**data, "titles": {}}
    return data


def set_week(week_start: date, urls: list[str]) -> None:
    """Replace the snapshot with a fresh URL list for `week_start`.

    Wipes any previously cached titles — a new week's URLs are (mostly)
    new recipes, so titles from the previous week would be irrelevant.
    Best-effort — IO failure is logged inside `DiskCache`, never raised.
    """
    _cache.set(
        _KEY,
        {
            "week_start": week_start.isoformat(),
            "urls": list(urls),
            "titles": {},
        },
    )
    log.info(
        "Fooby cache: stored %d URLs for week starting %s",
        len(urls), week_start.isoformat(),
    )


def set_title(url: str, title: str) -> None:
    """Merge a resolved title into the current snapshot.

    Silently no-ops when the snapshot doesn't include `url` (e.g. the
    week rolled over between prefetch fetch and prefetch write). The
    display still works — the title only feeds the status preview.
    """
    snapshot = get_week()
    if snapshot is None or url not in snapshot["urls"]:
        return
    titles = dict(snapshot["titles"])
    titles[url] = title
    _cache.set(
        _KEY,
        {
            "week_start": snapshot["week_start"],
            "urls": snapshot["urls"],
            "titles": titles,
        },
    )


def pick_url(day: date) -> str | None:
    """Return the URL that will play on `day`, or None if uncovered.

    Uses `day.weekday() % len(urls)` so a week where Fooby publishes
    fewer than 7 recipes still resolves to a URL every day (the tail
    weekdays repeat the head of the list — deterministic and identical
    every week, so the intra-week duplicate is at least predictable).
    Returns None when the stored snapshot doesn't cover `day`'s ISO
    week or the URL list is empty.
    """
    snapshot = get_week()
    if snapshot is None:
        return None
    if snapshot["week_start"] != week_start_of(day).isoformat():
        return None
    urls = snapshot["urls"]
    if not urls:
        return None
    return urls[day.weekday() % len(urls)]


def preview_for(day: date) -> dict | None:
    """Return `{for_date, url, title}` for `day` when both are known.

    Used by the status page's "Tomorrow" card, which only renders the
    Fooby preview when it has both the URL and a title to show. Missing
    title (prefetch not run yet, or parse failed) → return None so the
    card falls back to the generic hint.
    """
    url = pick_url(day)
    if url is None:
        return None
    snapshot = get_week()
    title = snapshot["titles"].get(url) if snapshot else None
    if not title:
        return None
    return {
        "for_date": day.isoformat(),
        "url": url,
        "title": title,
    }
