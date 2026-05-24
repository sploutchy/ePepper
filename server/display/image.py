"""BMP serialization of the current panel image.

Split out of `display_state` so the in-memory state module stays
focused on the page+state record and mutations. This module only
reads — it asks `display_state` for the rendered pages and the
current state, then encodes a single page as BMP for the device.
"""

import io

from display import state as display_state
from rendering.layout import render_idle


def get_image_bmp(page: int = 1) -> bytes | None:
    """Get a page as BMP bytes."""
    pages = display_state.get_pages()
    img = pages.get(page)
    if img is None:
        # In the idle state, render a hint panel pointing at the refresh
        # button. Without it the cleared display is blank and there's no
        # cue for which physical key wakes content back up.
        if display_state.get()["type"] == "idle":
            img = render_idle()
        else:
            return None
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()
