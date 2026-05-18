"""Shared display state — tracks current image, recipe, and device status.

Pure in-memory state. Content is lost on container restart — the ESP32 will
pick up whatever gets sent next via Telegram. No database needed for MVP.
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

# Cached render inputs so we can re-render when the battery reading updates.
# Only populated for recipe content; photos render once and never reflow.
_recipe_inputs: dict[str, Any] = {
    "recipe": None,
    "comments": [],
    "rating": None,
}

# Device status (reported by ESP32 on each button-press wake)
_device: dict[str, Any] = {
    "battery_mv": 0,
    "rssi": 0,
    "uptime_s": 0,
    "temperature_c": None,
    "humidity_pct": None,
    "last_seen": 0,
}


def set_image(img: Image.Image, content_type: str = "photo", title: str = "", lang: str = "en") -> None:
    """Set a single-page image as the current display content."""
    _pages.clear()
    _recipe_inputs.update({"recipe": None, "comments": [], "rating": None})
    _pages[1] = img
    _update_state(content_type=content_type, title=title, total_pages=1, lang=lang, recipe_id=None, url=None)


def set_recipe(
    recipe: dict,
    comments: list[str],
    rating: int | None = None,
    recipe_id: int | None = None,
    url: str | None = None,
) -> None:
    """Render and install a recipe. Re-renders later if the battery reading changes."""
    _recipe_inputs["recipe"] = recipe
    _recipe_inputs["comments"] = list(comments)
    _recipe_inputs["rating"] = rating

    total = _render_pages_from_inputs()
    _update_state(
        content_type="recipe",
        title=recipe.get("title", ""),
        total_pages=total,
        lang=recipe.get("lang", "en"),
        recipe_id=recipe_id,
        url=url,
    )


def _render_pages_from_inputs() -> int:
    """Render every page from the cached recipe inputs into `_pages`. Returns total pages."""
    # Imported lazily so display_state stays import-cheap (and avoids any chance
    # of a cycle if rendering grows server-side imports).
    from rendering.layout import render_recipe

    recipe = _recipe_inputs["recipe"]
    if recipe is None:
        return 0
    comments = _recipe_inputs["comments"]
    rating = _recipe_inputs["rating"]

    first_img, total = render_recipe(
        recipe, page=1, comments=comments, rating=rating,
    )
    _pages.clear()
    _pages[1] = first_img
    for p in range(2, total + 1):
        page_img, _ = render_recipe(
            recipe, page=p, comments=comments, rating=rating,
        )
        _pages[p] = page_img
    return total


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
    _recipe_inputs.update({"recipe": None, "comments": [], "rating": None})
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
        # In the idle state, hand the ESP32 a blank white panel so it can
        # actually paint the cleared screen instead of getting a 204 and
        # leaving the previous recipe up.
        if _state["type"] == "idle":
            img = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 1)
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


def update_device_status(
    battery_mv: int,
    rssi: int,
    uptime_s: int,
    temperature_c: float | None = None,
    humidity_pct: float | None = None,
) -> dict:
    """Update device status from an ESP32 wake-cycle report.

    `temperature_c` / `humidity_pct` are sent from the SHT40 when the device
    reads it on wake. They default to None when omitted so older firmware
    that doesn't yet report them keeps working.

    Returns a dict that may include `low_battery_alert_mv`: the battery
    reading that just crossed below the threshold. The caller is expected
    to deliver this alert (e.g. via Telegram). Hysteresis ensures we don't
    fire again until the battery has been charged back above the threshold.
    """
    global _low_battery_alerted

    _device.update({
        "battery_mv": battery_mv,
        "rssi": rssi,
        "uptime_s": uptime_s,
        "temperature_c": temperature_c,
        "humidity_pct": humidity_pct,
        "last_seen": int(time.time()),
    })

    alert_mv: int | None = None
    if battery_mv > 0:
        if battery_mv < LOW_BATTERY_MV and not _low_battery_alerted:
            _low_battery_alerted = True
            alert_mv = battery_mv
        elif battery_mv > LOW_BATTERY_MV + LOW_BATTERY_HYSTERESIS_MV:
            _low_battery_alerted = False

    return {"low_battery_alert_mv": alert_mv}


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
