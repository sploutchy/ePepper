"""Tests for the midnight-scheduler orchestration helpers (not the
outer `midnight_loop` sleep loop itself — that's exercised by the
`_seconds_until_next_local_*` unit tests in test_scheduler_time.py).

Every collaborator (library, fooby_cache, push_recipe_to_display,
ingest_recipe, process_recipe_url, fetch_weekly_inspiration_urls,
processing.llm, processing.recipes.translate_for_search) is monkeypatched
so these tests exercise scheduler.py's own control flow — fallback
ordering, error containment, cache usage — without touching the DB,
network, or an LLM.
"""

import asyncio
from datetime import date, datetime, timedelta

import processing.llm as llm_module
import processing.recipes as recipes_module
import scheduler


# ---------------------------------------------------------------------------
# _push_anniversary_for
# ---------------------------------------------------------------------------

def test_push_anniversary_returns_false_when_no_candidate(monkeypatch):
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: None)
    assert scheduler._push_anniversary_for(datetime(2026, 7, 12)) is False


def test_push_anniversary_pushes_and_returns_true(monkeypatch):
    row = {"id": 7, "title": "Tarte", "last_displayed_at": 0}
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: row)
    pushed = []
    monkeypatch.setattr(scheduler, "push_recipe_to_display", lambda r: pushed.append(r) or True)
    assert scheduler._push_anniversary_for(datetime(2026, 7, 12)) is True
    assert pushed == [row]


def test_push_anniversary_false_when_render_fails(monkeypatch):
    row = {"id": 8, "title": "Flop", "last_displayed_at": 0}
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: row)
    monkeypatch.setattr(scheduler, "push_recipe_to_display", lambda r: False)
    assert scheduler._push_anniversary_for(datetime(2026, 7, 12)) is False


# ---------------------------------------------------------------------------
# _push_fooby_inspiration_for
# ---------------------------------------------------------------------------

def test_fooby_uses_cached_pick_without_refetching(monkeypatch):
    today = datetime(2026, 7, 12)
    monkeypatch.setattr(
        scheduler.fooby_cache, "get",
        lambda: {"for_date": today.date().isoformat(), "url": "https://fooby.ch/x"},
    )
    fetch_called = []

    async def fake_fetch():
        fetch_called.append(True)
        return []
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    ingest_calls = []

    async def fake_ingest(url, *, push, persist):
        ingest_calls.append((url, push, persist))
        return {"action": "pushed", "recipe": {"title": "X"}, "url": url, "recipe_id": None}
    monkeypatch.setattr(scheduler, "ingest_recipe", fake_ingest)

    asyncio.run(scheduler._push_fooby_inspiration_for(today))
    assert fetch_called == []
    assert ingest_calls == [("https://fooby.ch/x", True, False)]


def test_fooby_fetches_live_and_picks_by_weekday(monkeypatch):
    today = datetime(2026, 7, 12)
    monkeypatch.setattr(scheduler.fooby_cache, "get", lambda: None)
    urls = ["https://fooby.ch/a", "https://fooby.ch/b", "https://fooby.ch/c"]

    async def fake_fetch():
        return urls
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    expected_url = urls[today.weekday() % len(urls)]
    ingest_calls = []

    async def fake_ingest(url, *, push, persist):
        ingest_calls.append(url)
        return {"action": "pushed", "recipe": {"title": "X"}, "url": url, "recipe_id": None}
    monkeypatch.setattr(scheduler, "ingest_recipe", fake_ingest)

    asyncio.run(scheduler._push_fooby_inspiration_for(today))
    assert ingest_calls == [expected_url]


def test_fooby_fetch_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(scheduler.fooby_cache, "get", lambda: None)

    async def fake_fetch():
        raise RuntimeError("network down")
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    asyncio.run(scheduler._push_fooby_inspiration_for(datetime(2026, 7, 12)))  # must not raise


def test_fooby_no_urls_leaves_display_unchanged(monkeypatch):
    monkeypatch.setattr(scheduler.fooby_cache, "get", lambda: None)

    async def fake_fetch():
        return []
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    ingest_calls = []

    async def fake_ingest(*a, **kw):
        ingest_calls.append((a, kw))
        return {}
    monkeypatch.setattr(scheduler, "ingest_recipe", fake_ingest)

    asyncio.run(scheduler._push_fooby_inspiration_for(datetime(2026, 7, 12)))
    assert ingest_calls == []


def test_fooby_ingest_error_is_swallowed(monkeypatch):
    today = datetime(2026, 7, 12)
    monkeypatch.setattr(
        scheduler.fooby_cache, "get",
        lambda: {"for_date": today.date().isoformat(), "url": "https://fooby.ch/x"},
    )

    async def fake_ingest(url, *, push, persist):
        raise scheduler.IngestError("nope")
    monkeypatch.setattr(scheduler, "ingest_recipe", fake_ingest)

    asyncio.run(scheduler._push_fooby_inspiration_for(today))  # must not raise


# ---------------------------------------------------------------------------
# _prefetch_fooby_for
# ---------------------------------------------------------------------------

def test_prefetch_skips_when_anniversary_covers_the_date(monkeypatch):
    target = date(2026, 7, 13)
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: {"id": 1})
    fetch_called = []

    async def fake_fetch():
        fetch_called.append(True)
        return []
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)
    set_calls = []
    monkeypatch.setattr(scheduler.fooby_cache, "set_pick", lambda *a: set_calls.append(a))

    asyncio.run(scheduler._prefetch_fooby_for(target))
    assert fetch_called == []
    assert set_calls == []


def test_prefetch_fetch_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: None)

    async def fake_fetch():
        raise RuntimeError("down")
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    asyncio.run(scheduler._prefetch_fooby_for(date(2026, 7, 13)))  # must not raise


def test_prefetch_no_urls_skips_cache_write(monkeypatch):
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: None)

    async def fake_fetch():
        return []
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)
    set_calls = []
    monkeypatch.setattr(scheduler.fooby_cache, "set_pick", lambda *a: set_calls.append(a))

    asyncio.run(scheduler._prefetch_fooby_for(date(2026, 7, 13)))
    assert set_calls == []


def test_prefetch_parse_failure_skips_cache_write(monkeypatch):
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: None)

    async def fake_fetch():
        return ["https://fooby.ch/only"]
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    async def fake_process(url):
        return None
    monkeypatch.setattr(scheduler, "process_recipe_url", fake_process)
    set_calls = []
    monkeypatch.setattr(scheduler.fooby_cache, "set_pick", lambda *a: set_calls.append(a))

    asyncio.run(scheduler._prefetch_fooby_for(date(2026, 7, 13)))
    assert set_calls == []


def test_prefetch_success_caches_pick(monkeypatch):
    target = date(2026, 7, 13)
    monkeypatch.setattr(scheduler.library, "pick_anniversary_recipe", lambda mmdd, year: None)
    urls = ["https://fooby.ch/a", "https://fooby.ch/b"]

    async def fake_fetch():
        return urls
    monkeypatch.setattr(scheduler, "fetch_weekly_inspiration_urls", fake_fetch)

    expected_url = urls[target.weekday() % len(urls)]

    async def fake_process(url):
        assert url == expected_url
        return {"title": "Tarte aux pommes"}
    monkeypatch.setattr(scheduler, "process_recipe_url", fake_process)

    set_calls = []
    monkeypatch.setattr(
        scheduler.fooby_cache, "set_pick",
        lambda t, u, title: set_calls.append((t, u, title)),
    )

    asyncio.run(scheduler._prefetch_fooby_for(target))
    assert set_calls == [(target, expected_url, "Tarte aux pommes")]


# ---------------------------------------------------------------------------
# backfill_translations
# ---------------------------------------------------------------------------

def test_backfill_skips_when_llm_disabled(monkeypatch):
    monkeypatch.setattr(llm_module, "is_enabled", lambda: False)
    called = []
    monkeypatch.setattr(scheduler.library, "recipes_needing_translation", lambda: called.append(True))
    asyncio.run(scheduler.backfill_translations())
    assert called == []


def test_backfill_noop_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(llm_module, "is_enabled", lambda: True)
    monkeypatch.setattr(scheduler.library, "recipes_needing_translation", lambda: [])
    set_calls = []
    monkeypatch.setattr(scheduler.library, "set_translated_keywords", lambda *a: set_calls.append(a))
    asyncio.run(scheduler.backfill_translations())
    assert set_calls == []


def test_backfill_writes_translated_blob(monkeypatch):
    monkeypatch.setattr(llm_module, "is_enabled", lambda: True)
    row = {"id": 1, "title": "Soup", "recipe": {"ingredients": ["salt"]}, "lang": "en"}
    monkeypatch.setattr(scheduler.library, "recipes_needing_translation", lambda: [row])

    async def fake_translate(payload):
        assert payload["title"] == "Soup"
        assert payload["ingredients"] == ["salt"]
        return "soupe;suppe"
    monkeypatch.setattr(recipes_module, "translate_for_search", fake_translate)

    set_calls = []
    monkeypatch.setattr(
        scheduler.library, "set_translated_keywords",
        lambda rid, blob: set_calls.append((rid, blob)),
    )
    asyncio.run(scheduler.backfill_translations())
    assert set_calls == [(1, "soupe;suppe")]


def test_backfill_falls_back_to_empty_sentinel_on_none(monkeypatch):
    monkeypatch.setattr(llm_module, "is_enabled", lambda: True)
    row = {"id": 2, "title": "X", "recipe": {}, "lang": "en"}
    monkeypatch.setattr(scheduler.library, "recipes_needing_translation", lambda: [row])

    async def fake_translate(payload):
        return None
    monkeypatch.setattr(recipes_module, "translate_for_search", fake_translate)

    set_calls = []
    monkeypatch.setattr(
        scheduler.library, "set_translated_keywords",
        lambda rid, blob: set_calls.append((rid, blob)),
    )
    asyncio.run(scheduler.backfill_translations())
    assert set_calls == [(2, "")]


def test_backfill_survives_translate_crash(monkeypatch):
    monkeypatch.setattr(llm_module, "is_enabled", lambda: True)
    row = {"id": 3, "title": "Y", "recipe": {}, "lang": "en"}
    monkeypatch.setattr(scheduler.library, "recipes_needing_translation", lambda: [row])

    async def boom(payload):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(recipes_module, "translate_for_search", boom)

    set_calls = []
    monkeypatch.setattr(
        scheduler.library, "set_translated_keywords",
        lambda rid, blob: set_calls.append((rid, blob)),
    )
    asyncio.run(scheduler.backfill_translations())  # must not raise
    assert set_calls == []


def test_backfill_survives_db_write_crash(monkeypatch):
    monkeypatch.setattr(llm_module, "is_enabled", lambda: True)
    row = {"id": 4, "title": "Z", "recipe": {}, "lang": "en"}
    monkeypatch.setattr(scheduler.library, "recipes_needing_translation", lambda: [row])

    async def fake_translate(payload):
        return "blob"
    monkeypatch.setattr(recipes_module, "translate_for_search", fake_translate)

    def boom(rid, blob):
        raise RuntimeError("disk full")
    monkeypatch.setattr(scheduler.library, "set_translated_keywords", boom)

    asyncio.run(scheduler.backfill_translations())  # must not raise


# ---------------------------------------------------------------------------
# initial_fooby_prefetch
# ---------------------------------------------------------------------------

def test_initial_prefetch_skips_when_cache_already_current(monkeypatch):
    tomorrow = (datetime.now(scheduler.TZ) + timedelta(days=1)).date()
    monkeypatch.setattr(scheduler.fooby_cache, "get", lambda: {"for_date": tomorrow.isoformat()})
    called = []

    async def fake_prefetch(target):
        called.append(target)
    monkeypatch.setattr(scheduler, "_prefetch_fooby_for", fake_prefetch)

    asyncio.run(scheduler.initial_fooby_prefetch())
    assert called == []


def test_initial_prefetch_runs_when_cache_is_stale(monkeypatch):
    monkeypatch.setattr(scheduler.fooby_cache, "get", lambda: None)
    called = []

    async def fake_prefetch(target):
        called.append(target)
    monkeypatch.setattr(scheduler, "_prefetch_fooby_for", fake_prefetch)

    asyncio.run(scheduler.initial_fooby_prefetch())
    expected = (datetime.now(scheduler.TZ) + timedelta(days=1)).date()
    assert called == [expected]


def test_initial_prefetch_swallows_exception(monkeypatch):
    monkeypatch.setattr(scheduler.fooby_cache, "get", lambda: None)

    async def boom(target):
        raise RuntimeError("nope")
    monkeypatch.setattr(scheduler, "_prefetch_fooby_for", boom)

    asyncio.run(scheduler.initial_fooby_prefetch())  # must not raise
