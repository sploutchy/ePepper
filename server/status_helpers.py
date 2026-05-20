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


def humanize_ago(ts: int) -> str:
    delta = max(0, int(time.time()) - ts)
    abs_time = datetime.fromtimestamp(ts).strftime("%H:%M")
    if delta < 60:
        return f"just now ({abs_time})"
    if delta < 3600:
        return f"{delta // 60} min ago ({abs_time})"
    if delta < 86400:
        return f"{delta // 3600} h ago ({abs_time})"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


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


def is_external_url(url: str | None) -> bool:
    """True iff `url` is an http(s) URL the browser can actually open.

    Used by templates to decide whether to wrap the source name in a
    link — cookbook:// and jsonld:* URLs are internal markers.
    """
    return bool(url) and (url.startswith("http://") or url.startswith("https://"))
