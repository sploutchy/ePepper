"""Persist and restore the e-ink panel state across container restarts.

Sits ABOVE `library` and `display_state` in the import layering: the
library layer owns the SQLite row (`display_panel`), `display_state`
owns the in-memory pages and emits lightweight change notifications,
and this module bridges the two by translating those notifications
into library writes and rebuilding `display_state` from the persisted
row at boot.

Splitting persistence out of `display_state` is what lets
`display_state` stay a pure in-memory module — no `import library`
lurking inside a function body to dodge a cycle.

Persistence policy (BUG-5 invariant — DO NOT change without a paired
test):

  - SAVED recipe active (`_state.type == "recipe"` AND `recipe_id is
    not None`)  → mirror (recipe_id, page) onto the singleton row.
  - Explicitly idle (`_state.type == "idle"`)            → delete the row.
  - Unsaved push (`_state.type == "recipe"` AND `recipe_id is None`)
                                                          → LEAVE the
    row alone, so a restart can still restore the previously saved
    recipe. The in-memory unsaved parse can't be recovered without
    re-running the URL/OCR pipeline (including an LLM round-trip), and
    the user hasn't asked us to keep it.
"""

import logging

import display_state
import library

log = logging.getLogger(__name__)


def persist_current() -> None:
    """Mirror the current in-memory display state to the SQLite singleton row.

    Called from display_state after every state mutation
    (set_recipe / set_page / clear). Wrapped in a try/except so a DB
    hiccup never takes down the in-memory display flow that just
    succeeded.
    """
    try:
        state = display_state.get()
        recipe_id = state.get("recipe_id")
        if state.get("type") == "recipe" and recipe_id is not None:
            library.set_panel_state(recipe_id, state.get("page", 1))
        elif state.get("type") == "idle":
            library.clear_panel_state()
        # else: unsaved recipe push — leave the previously persisted
        # saved-recipe row alone so restart can restore it.
    except Exception:
        log.exception("Failed to persist panel state")


def restore_on_startup() -> None:
    """At server boot, re-render whatever was last on the panel.

    Reads the `display_panel` singleton row, looks up the recipe, and
    calls `display_state.set_recipe` + `set_page` to rebuild the
    in-memory pages. A stale row (recipe was deleted while the server
    was down) is cleared. Failures are logged and swallowed — the
    panel comes back idle in the worst case, never crashes startup.
    On any restore failure the persisted row is cleared so a stuck
    recipe (e.g. missing font after upgrade) doesn't retry and fail on
    every container restart.
    """
    try:
        persisted = library.get_panel_state()
        if not persisted:
            return
        row = library.get_recipe(persisted["recipe_id"])
        if row is None:
            log.info(
                "Persisted panel recipe id=%s is gone — clearing stale state",
                persisted["recipe_id"],
            )
            library.clear_panel_state()
            return
        comments = [c["body"] for c in library.get_comments(row["id"])]
        display_state.set_recipe(
            row["recipe"],
            comments=comments,
            recipe_id=row["id"],
            url=row["url"],
        )
        # set_recipe resets page to 1; nudge to the persisted page if it
        # still fits (rendering might paginate differently than last run
        # if the recipe was edited via re-extract).
        target_page = persisted.get("page", 1)
        state = display_state.get()
        if target_page > 1 and target_page <= state["total_pages"]:
            display_state.set_page(target_page)
        state = display_state.get()
        log.info(
            "Restored panel: recipe_id=%s title=%r page=%d/%d",
            row["id"], row["title"], state["page"], state["total_pages"],
        )
    except Exception:
        log.exception("Failed to restore persisted panel state")
        # Clear the persisted row so we don't retry the same broken
        # restore (e.g. a font removed since save time keeps raising
        # in _render_pages) on every container restart.
        try:
            library.clear_panel_state()
            log.info("Cleared persisted panel state after restore failure")
        except Exception:
            log.exception("Failed to clear persisted panel state after restore failure")
