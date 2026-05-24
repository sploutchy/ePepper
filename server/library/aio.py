"""Async wrappers around `library.db`.

Every public function in `library.db` is re-exported here as an `async def`
that delegates to `asyncio.to_thread`, so FastAPI routes and aiogram
handlers can call the library without blocking the event loop on SQLite
disk I/O. Signatures mirror `db.py` 1:1 — these are pure trampolines, not
new entry points.

This module is intentionally only the wrapper layer: no caller is migrated
in the same commit that introduced it. Migration happens route-by-route
in follow-up PRs so each change is small and easy to revert.
"""

import asyncio

from library import db


async def normalize_url(*args, **kwargs):
    return await asyncio.to_thread(db.normalize_url, *args, **kwargs)


async def init_db(*args, **kwargs):
    return await asyncio.to_thread(db.init_db, *args, **kwargs)


async def upsert_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.upsert_recipe, *args, **kwargs)


async def set_translated_keywords(*args, **kwargs):
    return await asyncio.to_thread(db.set_translated_keywords, *args, **kwargs)


async def recipes_needing_translation(*args, **kwargs):
    return await asyncio.to_thread(db.recipes_needing_translation, *args, **kwargs)


async def get_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.get_recipe, *args, **kwargs)


async def find_by_url(*args, **kwargs):
    return await asyncio.to_thread(db.find_by_url, *args, **kwargs)


async def save_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.save_recipe, *args, **kwargs)


async def touch_displayed(*args, **kwargs):
    return await asyncio.to_thread(db.touch_displayed, *args, **kwargs)


async def delete_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.delete_recipe, *args, **kwargs)


async def hard_delete_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.hard_delete_recipe, *args, **kwargs)


async def remove_comment(*args, **kwargs):
    return await asyncio.to_thread(db.remove_comment, *args, **kwargs)


async def add_comment(*args, **kwargs):
    return await asyncio.to_thread(db.add_comment, *args, **kwargs)


async def count_saved(*args, **kwargs):
    return await asyncio.to_thread(db.count_saved, *args, **kwargs)


async def pick_anniversary_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.pick_anniversary_recipe, *args, **kwargs)


async def list_tags(*args, **kwargs):
    return await asyncio.to_thread(db.list_tags, *args, **kwargs)


async def list_recipes(*args, **kwargs):
    return await asyncio.to_thread(db.list_recipes, *args, **kwargs)


async def random_recipe(*args, **kwargs):
    return await asyncio.to_thread(db.random_recipe, *args, **kwargs)


async def list_sources(*args, **kwargs):
    return await asyncio.to_thread(db.list_sources, *args, **kwargs)


async def search(*args, **kwargs):
    return await asyncio.to_thread(db.search, *args, **kwargs)


async def get_comments(*args, **kwargs):
    return await asyncio.to_thread(db.get_comments, *args, **kwargs)


async def get_panel_state(*args, **kwargs):
    return await asyncio.to_thread(db.get_panel_state, *args, **kwargs)


async def set_panel_state(*args, **kwargs):
    return await asyncio.to_thread(db.set_panel_state, *args, **kwargs)


async def clear_panel_state(*args, **kwargs):
    return await asyncio.to_thread(db.clear_panel_state, *args, **kwargs)


async def create_session(*args, **kwargs):
    return await asyncio.to_thread(db.create_session, *args, **kwargs)


async def validate_session(*args, **kwargs):
    return await asyncio.to_thread(db.validate_session, *args, **kwargs)


async def delete_session(*args, **kwargs):
    return await asyncio.to_thread(db.delete_session, *args, **kwargs)
