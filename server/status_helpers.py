"""Formatting helpers shared by the Telegram /status command and the web
status page. Battery curve / RSSI buckets are kept here so the two surfaces
can't drift apart (e.g. one says "fair" and the other "good" for the same
RSSI).
"""

import time
from datetime import datetime
from urllib.parse import urlparse


# LiPo discharge curve, mV → %, piecewise linear between breakpoints.
# Picked so 3.70 V ≈ 50 % and the curve flattens above 4.0 V like real cells.
_BATTERY_CURVE = [(3300, 0), (3500, 25), (3700, 50), (3850, 75), (4200, 100)]


def battery_pct(mv: int) -> int:
    if mv >= _BATTERY_CURVE[-1][0]:
        return 100
    if mv <= _BATTERY_CURVE[0][0]:
        return 0
    for (mv1, p1), (mv2, p2) in zip(_BATTERY_CURVE, _BATTERY_CURVE[1:]):
        if mv1 <= mv <= mv2:
            return int(p1 + (p2 - p1) * (mv - mv1) / (mv2 - mv1))
    return 0


def humanize_date(ts: int | None) -> str:
    """Relative-time phrase for a past Unix timestamp.

    Returns "just now", "5 min ago", "3 h ago", "yesterday", "4 days ago",
    "last week", "2 weeks ago", "last month", "5 months ago", "last year",
    or "3 years ago". No clock annotation — for that variant see
    `humanize_ago`.

    None / 0 / negative deltas (clock skew) fold to a safe placeholder so
    callers don't have to pre-check.
    """
    if not ts:
        return "—"
    delta = max(0, int(time.time()) - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    days = delta // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "last week"
    if days < 30:
        return f"{days // 7} weeks ago"
    if days < 60:
        return "last month"
    if days < 365:
        return f"{days // 30} months ago"
    if days < 730:
        return "last year"
    return f"{days // 365} years ago"


def humanize_ago(ts: int) -> str:
    """Like `humanize_date`, but appends a same-day clock annotation
    "(HH:MM)" for timestamps inside the last 24 h. Used on the status
    page where "Last seen 7 min ago (14:23)" is more actionable than
    just "7 min ago". Older timestamps render bare so a stale device
    reads as "3 days ago" rather than a wall-clock date.
    """
    phrase = humanize_date(ts)
    if ts and max(0, int(time.time()) - ts) < 86400:
        return f"{phrase} ({datetime.fromtimestamp(ts).strftime('%H:%M')})"
    return phrase


def format_long_date(d: datetime) -> str:
    """Editorial long-form date, e.g. "Monday May 24th". English-only —
    used on the status page where the rest of the chrome is also English.
    """
    day = d.day
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{d.strftime('%A %B')} {day}{suffix}"


def rssi_quality(rssi: int) -> str:
    if rssi > -50:
        return "excellent"
    if rssi > -60:
        return "good"
    if rssi > -70:
        return "fair"
    if rssi > -80:
        return "weak"
    return "poor"


def source_name(url: str | None) -> str | None:
    """Humanize a recipe's source from its URL.

    - http(s) URL → second-to-last domain part, capitalized
      ('fooby.ch' → 'Fooby').
    - cookbook://name/slug → the netloc, capitalized
      ('cookbook://nos-recettes-preferees/crepes' →
      'Nos-recettes-preferees'). The caller decides whether to render
      it as a link (cookbook:// isn't browseable; the netloc is just
      a human label).
    - cookbook://<hash> (no netloc-as-name, used for photo-sourced
      recipes that the LLM tagged with a bare 'cookbook://' marker)
      → None.
    - jsonld:<hash> → None.
    - Missing / empty → None.
    """
    if not url:
        return None
    if url.startswith("jsonld:"):
        return None
    parts = urlparse(url)
    if parts.scheme == "cookbook":
        # A named cookbook URL has both a netloc and a path. The bare
        # "cookbook://" marker and the hashed "cookbook://<hash>" form
        # have no path → no human-meaningful name.
        if parts.netloc and parts.path and parts.path != "/":
            return parts.netloc.capitalize()
        return None
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    pieces = host.split(".")
    if len(pieces) >= 2:
        return pieces[-2].capitalize()
    return host or None
