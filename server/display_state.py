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

log = logging.getLogger(__name__)

# Current display state
_state: dict[str, Any] = {
    "hash": "",
    "type": "idle",         # "photo", "recipe", "idle"
    "page": 1,
    "total_pages": 1,
    "updated_at": 0,
    "title": "",
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


def set_image(img: Image.Image, content_type: str = "photo", title: str = "") -> None:
    """Set a single-page image as the current display content."""
    _pages.clear()
    _pages[1] = img
    _update_state(content_type=content_type, title=title, total_pages=1)


def set_recipe_pages(pages: dict[int, Image.Image], title: str = "") -> None:
    """Set multi-page recipe images as the current display content."""
    _pages.clear()
    _pages.update(pages)
    _update_state(content_type="recipe", title=title, total_pages=len(pages))


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
    })


def get() -> dict:
    """Get current display state."""
    return dict(_state)


def get_image_bmp(page: int = 1) -> bytes | None:
    """Get a page as BMP bytes."""
    img = _pages.get(page)
    if img is None:
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


def _update_state(content_type: str, title: str, total_pages: int) -> None:
    _state["type"] = content_type
    _state["title"] = title
    _state["page"] = 1
    _state["total_pages"] = total_pages
    _state["updated_at"] = int(time.time())
    _state["hash"] = _compute_hash(1)
    log.info("Display updated: type=%s title=%s pages=%d", content_type, title, total_pages)


def _compute_hash(page: int) -> str:
    img = _pages.get(page)
    if img is None:
        return ""
    return hashlib.md5(img.tobytes()).hexdigest()[:8]
