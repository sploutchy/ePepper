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

    Skip-if-active optimization: if `row` is already the live display
    content (same recipe_id), short-circuit with a True return — the
    device would otherwise wake and burn a full e-ink refresh for no
    visible change. `touch_displayed` is also skipped so the
    "recently shown" sort doesn't move on a no-op push. Used to live
    only in the scheduler; lifting it here means every caller (web push,
    bot search-tap, scheduler) gets the same idle-saving behavior.
    """
    state = display_state.get()
    if state.get("type") == "recipe" and state.get("recipe_id") == row["id"]:
        log.info(
            "Recipe id=%s already on display; skipping push", row["id"],
        )
        return True
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
