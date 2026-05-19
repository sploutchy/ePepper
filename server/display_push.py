"""Push a library row to the panel.

Extracted from bot.handlers so non-bot callers (api/web routes, scheduler)
can render to the display without importing the telegram bot module.
"""

import display_state
import library


def push_recipe_to_display(row: dict) -> None:
    """Render the recipe in `row` with its current comments + rating and push to the panel."""
    comments = [c["body"] for c in library.get_comments(row["id"])]
    display_state.set_recipe(
        row["recipe"],
        comments=comments,
        rating=row.get("rating"),
        recipe_id=row["id"],
        url=row["url"],
    )
