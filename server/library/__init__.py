"""Recipe library: persistent storage for pushed recipes, comments."""

from library.db import (
    init_db,
    upsert_recipe,
    get_recipe,
    find_by_url,
    save_recipe,
    touch_displayed,
    add_comment,
    get_comments,
    normalize_url,
    search,
    pick_anniversary_recipe,
    count_saved,
    list_recipes,
    list_sources,
    list_tags,
    random_recipe,
    delete_recipe,
    remove_comment,
    create_session,
    validate_session,
    delete_session,
    set_translated_keywords,
    recipes_needing_translation,
)
from library.db import _connect as _db_connect
from library import llm_calls as _llm_calls


def record_llm_call(
    *,
    kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Append one LLM-call row to the ledger. Errors are swallowed."""
    _llm_calls.record(
        _db_connect,
        kind=kind,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def llm_month_stats(since_ts: int) -> dict:
    """Aggregate calls/tokens/CHF since `since_ts`."""
    return _llm_calls.month_stats(_db_connect, since_ts)


__all__ = [
    "init_db",
    "upsert_recipe",
    "get_recipe",
    "find_by_url",
    "save_recipe",
    "touch_displayed",
    "add_comment",
    "get_comments",
    "normalize_url",
    "search",
    "pick_anniversary_recipe",
    "count_saved",
    "list_recipes",
    "list_sources",
    "list_tags",
    "random_recipe",
    "delete_recipe",
    "remove_comment",
    "create_session",
    "validate_session",
    "delete_session",
    "record_llm_call",
    "llm_month_stats",
    "set_translated_keywords",
    "recipes_needing_translation",
]
