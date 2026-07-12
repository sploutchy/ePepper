"""Round-trip tests against a throwaway SQLite DB (DATA_DIR is a temp dir,
see conftest.py). Exercises the upsert / save / search / soft-delete flow
and the FTS index staying in sync.
"""

import library


def setup_module(_):
    library.init_db()


def _recipe(title="Zürcher Geschnetzeltes", ingredients=None):
    return {
        "title": title,
        "total_time": 40,
        "servings": "4",
        "ingredients": ingredients or ["Kalbfleisch", "Champignons", "Rahm"],
        "instructions": [{"type": "step", "text": "Fleisch anbraten."}],
        "lang": "de",
    }


def test_upsert_save_find_roundtrip():
    url = "https://example.ch/geschnetzeltes"
    rid = library.upsert_recipe(url, _recipe(), source="Example")
    assert library.save_recipe(rid) is True

    row = library.find_by_url("https://EXAMPLE.ch/geschnetzeltes/?utm_source=x")
    assert row is not None and row["id"] == rid
    assert row["recipe"]["ingredients"][0] == "Kalbfleisch"


def test_fts_search_matches_ingredients_with_prefix():
    url = "https://example.ch/fts-recipe"
    rid = library.upsert_recipe(url, _recipe("Pilzragout", ["Steinpilze", "Zwiebeln"]))
    library.save_recipe(rid)
    hits = library.search("steinpilz")  # prefix match, diacritics-insensitive
    assert any(r["id"] == rid for r in hits)


def test_unsaved_rows_are_not_searchable():
    url = "https://example.ch/unsaved"
    library.upsert_recipe(url, _recipe("Geheimrezept", ["Safran"]))
    assert not any(r["title"] == "Geheimrezept" for r in library.search("safran"))


def test_soft_delete_removes_from_search_and_upsert_restores():
    url = "https://example.ch/deleted"
    rid = library.upsert_recipe(url, _recipe("Apfelwähe", ["Äpfel"]))
    library.save_recipe(rid)
    assert library.delete_recipe(rid) is True
    assert library.get_recipe(rid) is None
    assert not any(r["id"] == rid for r in library.search("apfelwähe"))
    # Re-adding the same URL resurrects the row (and its FTS entry).
    rid2 = library.upsert_recipe(url, _recipe("Apfelwähe", ["Äpfel"]))
    assert rid2 == rid
    assert library.get_recipe(rid) is not None


def test_set_tags_updates_fts_and_list_tags():
    url = "https://example.ch/tagged"
    rid = library.upsert_recipe(url, _recipe("Curry"))
    library.save_recipe(rid)
    assert library.set_tags(rid, ["Vegi", "schnell"]) is True
    assert library.get_recipe(rid)["tags"] == ["vegi", "schnell"]
    assert any(r["id"] == rid for r in library.search("vegi"))
    assert ("vegi", 1) in library.list_tags()


def test_fts_operators_in_query_are_neutralized():
    # FTS5 syntax in user input must not raise.
    assert library.search('AND OR NOT "quoted') == []
    assert library.search("") == []


def test_panel_state_roundtrip():
    url = "https://example.ch/panel"
    rid = library.upsert_recipe(url, _recipe("Panelgericht"))
    library.save_recipe(rid)
    library.set_panel_state(rid, 2)
    assert library.get_panel_state() == {"recipe_id": rid, "page": 2}
    library.clear_panel_state()
    assert library.get_panel_state() is None
