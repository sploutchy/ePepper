"""Push a library row to the panel.

Extracted from bot.handlers so non-bot callers (api/web routes, scheduler)
can render to the display without importing the telegram bot module.
"""

import logging

from display import state as display_state
import library

log = logging.getLogger(__name__)


def push_recipe_to_display(row: dict) -> bool:
    """Render the recipe in `row` with its current comments and push to the
    panel. Returns True on success, False if rendering raised — the
    previous display content is preserved in the failure case (atomic commit
    inside `display_state.set_recipe`).

    On success, bumps `last_displayed_at` + `displayed_count` on the
    library row so the "recently cooked" sort and anniversary scheduler
    track actual usage.
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
    library.touch_displayed(row["id"])
    return True
