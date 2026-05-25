"""In-memory display state — the current image, recipe, and page navigation.

Pure state + mutation API. Persistence across restarts is handled by a
separately-wired listener (see `display_persistence.persist_current`
registered from `main.py` via `register_change_listener`) so this
module stays below `library` in the import layering.

BMP serialization lives in `display/image.py`; device telemetry
(battery / heartbeat / alert hysteresis) in `device_telemetry.py`.
"""

import hashlib
import logging
import time
from typing import Any, Callable

from PIL import Image

from rendering.layout import render_recipe
from status_helpers import source_name

log = logging.getLogger(__name__)

# Fires after every mutation (set_recipe / clear). Injected
# at startup so this module doesn't import `library` (or anything
# above it in the layering); typically wired to
# `display_persistence.persist_current`.
_change_listener: Callable[[], None] | None = None


def register_change_listener(fn: Callable[[], None]) -> None:
    """Install the panel-state change listener (typically the persistence
    hook). Called once at server startup. Invoked AFTER each successful
    mutation with no arguments — read current state via `get()`."""
    global _change_listener
    _change_listener = fn


def _notify_changed() -> None:
    """Fire the registered change listener; swallow listener errors so a
    misbehaving listener can't take down the display flow that just
    succeeded. No listener = no-op (useful in unit tests)."""
    if _change_listener is None:
        return
    try:
        _change_listener()
    except Exception:
        log.exception("Display state change listener raised")


# Current display state
_state: dict[str, Any] = {
    "hash": "",             # per-page md5; flips on page nav. Drives the web preview's ?v= cache-buster.
    "content_hash": "",     # md5 over ALL pages; stable across page nav, changes only on a new render.
                            # The device keys its on-flash page cache on this so a page turn can be
                            # served offline and a genuinely new recipe still invalidates the cache.
    "type": "idle",         # "recipe", "idle"
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
# resupply comments on small mutations.
_recipe_inputs: dict[str, Any] = {"recipe": None, "comments": [], "url": None}

# Set to a library row id when set_recipe installs a saved recipe; the
# /image handler consumes it on the first device fetch to bump
# last_displayed_at. Reset on the next set_recipe / clear — so a recipe
# replaced before the device ever wakes is never counted as cooked.
_pending_displayed_bump: int | None = None


def set_recipe(
    recipe: dict,
    comments: list[str],
    recipe_id: int | None = None,
    url: str | None = None,
    *,
    count_display: bool = True,
) -> None:
    """Render and install a recipe as the active display content.
    Renders into a local buffer first and only commits to `_pages` /
    `_recipe_inputs` on success — a render failure leaves the previous
    display content intact rather than half-replacing it.

    `count_display` (default True) arms a pending displayed-bump so the
    next device /image fetch credits the cook. Set False for a silent
    rebuild that must NOT look like a fresh cook — e.g. restoring the
    panel after a container restart (BUG-2), where re-arming the bump
    would mis-increment displayed_count and reset last_displayed_at for
    a recipe nobody re-cooked, corrupting the anniversary scheduler."""
    inputs = {
        "recipe": recipe,
        "comments": list(comments),
        "url": url,
    }
    # Render first (may raise — exception propagates to the caller). The
    # commit below only runs on success, so a failure leaves the previous
    # display content intact.
    new_pages = _render_pages(inputs)
    if not new_pages:
        return

    global _pending_displayed_bump
    _recipe_inputs.update(inputs)
    _pages.clear()
    _pages.update(new_pages)
    # Arm a pending bump for the next device fetch. Overwriting an
    # un-consumed value is intentional: if the previous recipe was
    # replaced before the device ever picked it up, it was never
    # actually cooked, so it shouldn't be counted. Skipped entirely on a
    # silent rebuild (count_display=False) — see the docstring.
    if count_display:
        _pending_displayed_bump = recipe_id
    _update_state(
        content_type="recipe",
        title=recipe.get("title", ""),
        total_pages=len(new_pages),
        lang=recipe.get("lang", "en"),
        recipe_id=recipe_id,
        url=url,
    )
    _notify_changed()


def _render_pages(inputs: dict) -> dict[int, Image.Image]:
    """Render every page from `inputs` into a fresh dict, no side effects.
    Returning a new dict (instead of mutating `_pages`) lets `set_recipe`
    commit atomically."""
    recipe = inputs["recipe"]
    if recipe is None:
        return {}
    comments = inputs["comments"]
    # Pull the source name off the URL the same way the web + bot do, so
    # the panel header matches what those surfaces show.
    source = source_name(inputs.get("url"))

    pages: dict[int, Image.Image] = {}
    first_img, total = render_recipe(
        recipe, page=1, comments=comments, source=source,
    )
    pages[1] = first_img
    for p in range(2, total + 1):
        page_img, _ = render_recipe(
            recipe, page=p, comments=comments, source=source,
        )
        pages[p] = page_img
    return pages


def clear() -> None:
    """Clear the display (idle state)."""
    global _pending_displayed_bump
    _pages.clear()
    _recipe_inputs.update({"recipe": None, "comments": [], "url": None})
    _pending_displayed_bump = None
    _state.update({
        "hash": hashlib.md5(b"idle").hexdigest()[:8],
        "content_hash": hashlib.md5(b"idle").hexdigest()[:8],
        "type": "idle",
        "page": 1,
        "total_pages": 1,
        "updated_at": int(time.time()),
        "title": "",
        "recipe_id": None,
        "url": None,
    })
    _notify_changed()


def get() -> dict:
    """Get current display state."""
    return dict(_state)


def consume_pending_displayed_bump() -> int | None:
    """Return the pending recipe id (if any) and clear it. Single-shot —
    subsequent calls return None until the next set_recipe arms a new
    bump. The /image handler calls this on a device fetch to credit
    last_displayed_at to the moment the panel actually pulled the image
    rather than the moment the server pushed it."""
    global _pending_displayed_bump
    pending = _pending_displayed_bump
    _pending_displayed_bump = None
    return pending


def get_pages() -> dict[int, Image.Image]:
    """Live (read-only) handle to the rendered pages; callers must not
    mutate. Exposed so `display_image` doesn't reach into module-private
    `_pages`."""
    return _pages


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
    _state["content_hash"] = _compute_content_hash()
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


def _compute_content_hash() -> str:
    """md5 over every page's pixels, in page order — a stable identity for
    the whole rendered recipe. Unlike `_compute_hash` it doesn't move when
    only the current page changes, so the device can treat its on-flash
    page cache as valid across page turns and only re-download when this
    value actually changes (a new recipe / re-render)."""
    if not _pages:
        return ""
    h = hashlib.md5()
    for page in sorted(_pages):
        h.update(_pages[page].tobytes())
    return h.hexdigest()[:8]
