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
    "lang": "en",           # recipe language, used by the ESP32 to localize the clock overlay
    "recipe_id": None,      # library row id when the active recipe is saved; None for unsaved or non-recipe
    "url": None,            # source URL of the active recipe (used to identify it across save flows)
}

# Page images: {page_number: PIL.Image}
_pages: dict[int, Image.Image] = {}

# Device status (reported by ESP32)
_device: dict[str, Any] = {
    "battery_mv": 0,
    "rssi": 0,
    "uptime_s": 0,
    "last_seen": 0,
}


def set_image(img: Image.Image, content_type: str = "photo", title: str = "", lang: str = "en") -> None:
    """Set a single-page image as the current display content."""
    _pages.clear()
    _pages[1] = img
    _update_state(content_type=content_type, title=title, total_pages=1, lang=lang, recipe_id=None, url=None)


def set_recipe_pages(
    pages: dict[int, Image.Image],
    title: str = "",
    lang: str = "en",
    recipe_id: int | None = None,
    url: str | None = None,
) -> None:
    """Set multi-page recipe images as the current display content."""
    _pages.clear()
    _pages.update(pages)
    _update_state(
        content_type="recipe",
        title=title,
        total_pages=len(pages),
        lang=lang,
        recipe_id=recipe_id,
        url=url,
    )


def attach_recipe_id(recipe_id: int) -> None:
    """Link the current display to a newly-saved library row.

    Used when the user just saved a recipe that's still on the panel — we
    don't want to re-push pages (nothing visible changes), only flip the
    `recipe_id` so /comment knows which row to write to.
    """
    if _state["type"] == "recipe":
        _state["recipe_id"] = recipe_id


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


def update_device_status(battery_mv: int, rssi: int, uptime_s: int) -> None:
    """Update device status from ESP32 report."""
    _device.update({
        "battery_mv": battery_mv,
        "rssi": rssi,
        "uptime_s": uptime_s,
        "last_seen": int(time.time()),
    })


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
