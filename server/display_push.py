"""Push a library row to the panel.

Extracted from bot.handlers so non-bot callers (api/web routes, scheduler)
can render to the display without importing the telegram bot module.
"""

import logging

import display_state
import library

log = logging.getLogger(__name__)


def push_recipe_to_display(row: dict) -> bool:
    """Render the recipe in `row` with its current comments and push to the
    panel. Returns True on success, False if rendering raised — the
    previous display content is preserved in the failure case (atomic commit
    inside `display_state.set_recipe`).

    Arms a pending bump of `last_displayed_at` + `displayed_count` that
    fires on the first device fetch of the new image (see the /image
    handler), so "recently cooked" reflects when the panel actually
    pulled the recipe rather than when the server installed it.
    """
    comments = [c["body"] for c in library.get_comments(row["id"])]
    try:
        display_state.set_recipe(
            row["recipe"],
            comments=comments,
            recipe_id=row["id"],
            url=row["url"],
        )
    except Exception:
        log.exception("Failed to render recipe id=%s to display", row.get("id"))
        return False
    return True
