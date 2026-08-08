"""Weekly snapshot semantics of `processing.fooby_cache`.

The scheduler leans on two invariants: (1) a picked URL stays the same
for every day inside the ISO week the snapshot was taken for, so Fooby's
mid-week reshuffles don't change what plays; (2) the snapshot silently
stops applying once its `week_start` no longer matches, so a stale
entry from a previous week never leaks into a new week's picks. These
tests pin both.
"""

from datetime import date, timedelta

import pytest

from processing import fooby_cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the module-level DiskCache at a temp file per test."""
    from cache.disk import DiskCache
    monkeypatch.setattr(fooby_cache, "_cache", DiskCache(str(tmp_path / "fooby.json")))


def test_week_start_of_returns_monday():
    # 2026-08-05 is a Wednesday; the ISO Monday is 2026-08-03.
    assert fooby_cache.week_start_of(date(2026, 8, 5)) == date(2026, 8, 3)
    # A Monday returns itself.
    assert fooby_cache.week_start_of(date(2026, 8, 3)) == date(2026, 8, 3)
    # A Sunday returns the preceding Monday.
    assert fooby_cache.week_start_of(date(2026, 8, 9)) == date(2026, 8, 3)


def test_pick_url_stable_across_the_week():
    monday = date(2026, 8, 3)
    urls = [f"https://fooby.ch/fr/recettes/{i}/x" for i in range(7)]
    fooby_cache.set_week(monday, urls)

    # Every day of that ISO week resolves to a distinct URL, matching
    # its weekday index — the whole point of the snapshot.
    for offset in range(7):
        day = monday + timedelta(days=offset)
        assert fooby_cache.pick_url(day) == urls[offset]


def test_pick_url_ignores_snapshot_from_a_different_week():
    fooby_cache.set_week(date(2026, 8, 3), ["https://fooby.ch/fr/recettes/1/x"])
    # A day in the following ISO week is not covered by last week's snapshot.
    assert fooby_cache.pick_url(date(2026, 8, 10)) is None


def test_pick_url_wraps_when_fewer_than_seven_urls():
    monday = date(2026, 8, 3)
    urls = [
        "https://fooby.ch/fr/recettes/1/a",
        "https://fooby.ch/fr/recettes/2/b",
        "https://fooby.ch/fr/recettes/3/c",
    ]
    fooby_cache.set_week(monday, urls)
    # Wed (weekday 2) → urls[2]; Thu (weekday 3) wraps to urls[0]; Fri
    # to urls[1], and so on. Deterministic + repeated every week.
    assert fooby_cache.pick_url(monday + timedelta(days=2)) == urls[2]
    assert fooby_cache.pick_url(monday + timedelta(days=3)) == urls[0]
    assert fooby_cache.pick_url(monday + timedelta(days=4)) == urls[1]


def test_set_week_wipes_titles_from_the_previous_snapshot():
    old_monday = date(2026, 8, 3)
    old_url = "https://fooby.ch/fr/recettes/1/x"
    fooby_cache.set_week(old_monday, [old_url])
    fooby_cache.set_title(old_url, "Old title")
    assert fooby_cache.get_week()["titles"] == {old_url: "Old title"}

    # New week starts — the title map resets so last week's titles can't
    # be shown alongside a new week's URL by mistake.
    new_monday = date(2026, 8, 10)
    new_url = "https://fooby.ch/fr/recettes/2/y"
    fooby_cache.set_week(new_monday, [new_url])
    assert fooby_cache.get_week()["titles"] == {}


def test_set_title_ignores_urls_not_in_current_snapshot():
    fooby_cache.set_week(date(2026, 8, 3), ["https://fooby.ch/fr/recettes/1/x"])
    fooby_cache.set_title("https://fooby.ch/fr/recettes/99/nope", "Stray")
    # The stray URL was silently dropped — no crash, no leak into titles.
    assert fooby_cache.get_week()["titles"] == {}


def test_preview_for_requires_both_pick_and_title():
    monday = date(2026, 8, 3)
    url = "https://fooby.ch/fr/recettes/1/x"
    fooby_cache.set_week(monday, [url])

    # URL present but no title yet → preview withheld so the status page
    # falls back to its generic hint instead of showing "None".
    assert fooby_cache.preview_for(monday) is None

    fooby_cache.set_title(url, "Recipe X")
    assert fooby_cache.preview_for(monday) == {
        "for_date": monday.isoformat(),
        "url": url,
        "title": "Recipe X",
    }


def test_get_week_returns_none_for_malformed_or_missing_entry(tmp_path):
    # Fresh cache: nothing stored.
    assert fooby_cache.get_week() is None
    # Simulate an old-format entry (pre-migration): shape check drops it.
    fooby_cache._cache.set("week", {"for_date": "2026-08-05", "url": "x", "title": "y"})
    assert fooby_cache.get_week() is None
