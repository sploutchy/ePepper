"""Shared display state — tracks current image, recipe, and device status.

Pure in-memory state. The active recipe (and any in-flight unsaved push)
is lost on container restart; saved recipes persist in the SQLite library
and can be re-pushed via /search or the midnight anniversary scheduler.
"""

import hashlib
import io
import logging
import time
from typing import Any

from PIL import Image

from config import DISPLAY_WIDTH, DISPLAY_HEIGHT

log = logging.getLogger(__name__)

# Current display state
_state: dict[str, Any] = {
    "hash": "",
    "type": "idle",         # "photo", "recipe", "idle"
    "page": 1,
    "total_pages": 1,
    "updated_at": 0,
    "title": "",
    "lang": "en",
    "recipe_id": None,      # library row id when the active recipe is saved; None for unsaved or non-recipe
    "url": None,            # source URL of the active recipe (used to identify it across save flows)
}

# Page images: {page_number: PIL.Image}
_pages: dict[int, Image.Image] = {}

# Cached render inputs so push_recipe_to_display callers don't have to
# resupply comments / rating on small mutations (cmd_comment, cmd_rate
# both re-fetch the row and re-set_recipe). Only populated for recipe
# content; photos render once and never reflow.
_recipe_inputs: dict[str, Any] = {
    "recipe": None,
    "comments": [],
    "rating": None,
    "url": None,
}

# Device status (reported by ESP32 on every wake — button press or
# daily timer). Whichever fires more recently overwrites the others.
_device: dict[str, Any] = {
    "battery_mv": 0,
    "rssi": 0,
    "temperature_c": None,
    "humidity_pct": None,
    "last_seen": 0,
}


def set_image(img: Image.Image, content_type: str = "photo", title: str = "", lang: str = "en") -> None:
    """Set a single-page image as the current display content."""
    _pages.clear()
    _recipe_inputs.update({"recipe": None, "comments": [], "rating": None, "url": None})
    _pages[1] = img
    _update_state(content_type=content_type, title=title, total_pages=1, lang=lang, recipe_id=None, url=None)


def set_recipe(
    recipe: dict,
    comments: list[str],
    rating: int | None = None,
    recipe_id: int | None = None,
    url: str | None = None,
) -> None:
    """Render and install a recipe as the active display content.

    Renders into a local buffer first and only commits to `_pages` /
    `_recipe_inputs` on success — a render failure leaves the previous
    display content intact rather than half-replacing it.
    """
    inputs = {
        "recipe": recipe,
        "comments": list(comments),
        "rating": rating,
        "url": url,
    }
    try:
        new_pages = _render_pages(inputs)
    except Exception:
        log.exception("set_recipe: render failed; leaving display unchanged")
        return
    if not new_pages:
        return

    _recipe_inputs.update(inputs)
    _pages.clear()
    _pages.update(new_pages)
    _update_state(
        content_type="recipe",
        title=recipe.get("title", ""),
        total_pages=len(new_pages),
        lang=recipe.get("lang", "en"),
        recipe_id=recipe_id,
        url=url,
    )


def _render_pages(inputs: dict) -> dict[int, Image.Image]:
    """Render every page from `inputs` into a fresh dict, with no side effects.

    Returning a new dict (instead of mutating `_pages`) lets `set_recipe`
    commit atomically.
    """
    # Imported lazily so display_state stays import-cheap (and avoids any chance
    # of a cycle if rendering grows server-side imports).
    from rendering.layout import render_recipe
    from status_helpers import source_name

    recipe = inputs["recipe"]
    if recipe is None:
        return {}
    comments = inputs["comments"]
    rating = inputs["rating"]
    # Pull the source name off the URL the same way the web + bot do, so
    # the panel header matches what those surfaces show.
    source = source_name(inputs.get("url"))

    pages: dict[int, Image.Image] = {}
    first_img, total = render_recipe(
        recipe, page=1, comments=comments, rating=rating, source=source,
    )
    pages[1] = first_img
    for p in range(2, total + 1):
        page_img, _ = render_recipe(
            recipe, page=p, comments=comments, rating=rating, source=source,
        )
        pages[p] = page_img
    return pages


def set_page(page: int) -> bool:
    """Change the current page. Returns True if valid."""
    if page < 1 or page > _state["total_pages"]:
        return False
    _state["page"] = page
    _state["hash"] = _compute_hash(page)
    return True


def clear() -> None:
    """Clear the display (idle state)."""
    _pages.clear()
    _recipe_inputs.update({"recipe": None, "comments": [], "rating": None, "url": None})
    _state.update({
        "hash": hashlib.md5(b"idle").hexdigest()[:8],
        "type": "idle",
        "page": 1,
        "total_pages": 1,
        "updated_at": int(time.time()),
        "title": "",
        "recipe_id": None,
        "url": None,
    })


def get() -> dict:
    """Get current display state."""
    return dict(_state)


def get_image_bmp(page: int = 1) -> bytes | None:
    """Get a page as BMP bytes."""
    img = _pages.get(page)
    if img is None:
        # In the idle state, render a hint panel pointing at the refresh
        # button. Without it the cleared display is blank and there's no
        # cue for which physical key wakes content back up.
        if _state["type"] == "idle":
            from rendering.layout import render_idle
            img = render_idle()
        else:
            return None
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()


# Low-battery alert thresholds. Cross BELOW LOW_BATTERY_MV → fire an alert
# once; only re-arm after the reading climbs back above LOW_BATTERY_MV +
# HYSTERESIS to avoid repeated alerts on a noisy reading near the boundary.
LOW_BATTERY_MV = 3500
LOW_BATTERY_HYSTERESIS_MV = 100

_low_battery_alerted = False

# Heartbeat staleness. Firmware reports on button press + a daily timer wake;
# 25 h gives the daily timer a buffer for clock drift / a slow Wi-Fi reconnect.
# Alert once when crossed; re-arm only when the next POST arrives (handled in
# update_device_status). The check itself runs proactively from scheduler.py
# because the absence of POSTs is exactly what we're detecting.
STALE_HEARTBEAT_S = 25 * 3600

_stale_heartbeat_alerted = False


def update_device_status(
    battery_mv: int,
    rssi: int,
    temperature_c: float | None = None,
    humidity_pct: float | None = None,
) -> dict:
    """Update device status from an ESP32 wake-cycle report.

    `temperature_c` / `humidity_pct` are sent from the SHT40 when the device
    reads it on wake. They default to None when omitted so older firmware
    that doesn't yet report them keeps working.

    Returns `{"low_battery_alert_mv": int | None}`. When non-None, the
    battery just crossed below the threshold and the caller is expected
    to deliver this alert (e.g. via Telegram). Hysteresis prevents the
    alert from firing again until the battery climbs above
    LOW_BATTERY_MV + LOW_BATTERY_HYSTERESIS_MV.
    """
    global _low_battery_alerted, _stale_heartbeat_alerted

    _device.update({
        "battery_mv": battery_mv,
        "rssi": rssi,
        "temperature_c": temperature_c,
        "humidity_pct": humidity_pct,
        "last_seen": int(time.time()),
    })

    # Fresh POST means the device is back — re-arm the staleness alert.
    _stale_heartbeat_alerted = False

    alert_mv: int | None = None
    if battery_mv > 0:
        if battery_mv < LOW_BATTERY_MV and not _low_battery_alerted:
            _low_battery_alerted = True
            alert_mv = battery_mv
        elif battery_mv > LOW_BATTERY_MV + LOW_BATTERY_HYSTERESIS_MV:
            _low_battery_alerted = False

    return {"low_battery_alert_mv": alert_mv}


def check_heartbeat_stale() -> int | None:
    """Return hours-since-last-seen if the heartbeat just went stale, else None.

    Returns None when the device has never reported (last_seen == 0), the
    threshold hasn't been crossed, or we already alerted for this episode.
    The flag is cleared the next time update_device_status() runs.
    """
    global _stale_heartbeat_alerted
    last_seen = _device.get("last_seen", 0)
    if last_seen <= 0:
        return None
    delta_s = int(time.time()) - last_seen
    if delta_s > STALE_HEARTBEAT_S and not _stale_heartbeat_alerted:
        _stale_heartbeat_alerted = True
        return delta_s // 3600
    return None


def get_device_status() -> dict:
    """Get last known device status."""
    return dict(_device)


def _update_state(
    content_type: str,
    title: str,
    total_pages: int,
    lang: str = "en",
    recipe_id: int | None = None,
    url: str | None = None,
) -> None:
    _state["type"] = content_type
    _state["title"] = title
    _state["page"] = 1
    _state["total_pages"] = total_pages
    _state["updated_at"] = int(time.time())
    _state["hash"] = _compute_hash(1)
    _state["lang"] = lang
    _state["recipe_id"] = recipe_id
    _state["url"] = url
    log.info(
        "Display updated: type=%s title=%s pages=%d lang=%s recipe_id=%s url=%s",
        content_type, title, total_pages, lang, recipe_id, url,
    )


def _compute_hash(page: int) -> str:
    img = _pages.get(page)
    if img is None:
        return ""
    return hashlib.md5(img.tobytes()).hexdigest()[:8]
