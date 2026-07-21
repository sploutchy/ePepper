"""Tests for the hidden /recipes/<id>/edit content-fix page.

Covers the persistence layer (library.update_recipe_content) and the
web routes (GET prefill, POST save/validate/redirect), plus the "stays
hidden" constraint — no link to it anywhere in the normal recipe page.
"""
import sqlite3

import pytest

import library
from library import db


def _make_recipe(title="Test Recipe", ingredients=None, instructions=None, lang="en"):
    return {
        "title": title,
        "ingredients": ingredients if ingredients is not None else ["1 cup flour", "2 eggs"],
        "instructions": instructions if instructions is not None else [
            {"type": "step", "text": "Mix everything."},
        ],
        "total_time": 30,
        "servings": "4",
        "lang": lang,
    }


def _save(url, **kwargs):
    recipe_id = library.upsert_recipe(url, _make_recipe(**kwargs))
    library.save_recipe(recipe_id)
    return recipe_id


# --- library.update_recipe_content ------------------------------------------


def test_update_recipe_content_updates_fields(test_db):
    recipe_id = _save("https://example.com/r1", title="Original Title")
    library.set_tags(recipe_id, ["vegi"])
    library.set_translated_keywords(recipe_id, "some stale french blob")

    new_recipe = _make_recipe(title="Fixed Title", ingredients=["1 kg Feigen (gerüstet)"])
    ok = library.update_recipe_content(
        recipe_id, new_recipe, url="https://example.com/r1", source="example",
    )
    assert ok is True

    row = library.get_recipe(recipe_id)
    assert row["title"] == "Fixed Title"
    assert row["recipe"]["ingredients"] == ["1 kg Feigen (gerüstet)"]
    assert row["tags"] == ["vegi"]  # untouched by a content edit


def test_update_recipe_content_nulls_translated_keywords(test_db):
    recipe_id = _save("https://example.com/r2")
    library.set_translated_keywords(recipe_id, "stale blob")

    library.update_recipe_content(
        recipe_id, _make_recipe(), url="https://example.com/r2", source="example",
    )

    with db._connect() as conn:
        row = conn.execute(
            "SELECT translated_keywords FROM recipes WHERE id = ?", (recipe_id,),
        ).fetchone()
    assert row["translated_keywords"] is None


def test_update_recipe_content_rebuilds_fts(test_db):
    recipe_id = _save("https://example.com/r3", title="Old Title")

    library.update_recipe_content(
        recipe_id,
        _make_recipe(title="Renamed Title", ingredients=["gerüstete Feigen"]),
        url="https://example.com/r3", source="example",
    )

    with db._connect() as conn:
        fts_row = conn.execute(
            "SELECT title, ingredients FROM recipes_fts WHERE rowid = ?", (recipe_id,),
        ).fetchone()
    assert fts_row["title"] == "Renamed Title"
    assert "gerüstete Feigen" in fts_row["ingredients"]


def test_update_recipe_content_missing_id_returns_false(test_db):
    ok = library.update_recipe_content(
        999999, _make_recipe(), url="https://example.com/gone", source=None,
    )
    assert ok is False


def test_update_recipe_content_url_collision_raises(test_db):
    _save("https://example.com/a")
    recipe_id_b = _save("https://example.com/b")

    with pytest.raises(sqlite3.IntegrityError):
        library.update_recipe_content(
            recipe_id_b, _make_recipe(), url="https://example.com/a", source="example",
        )


# --- GET /app/recipes/<id>/edit ----------------------------------------------


def test_edit_page_requires_auth(anon_client, test_db):
    recipe_id = _save("https://example.com/r4")
    resp = anon_client.get(f"/app/recipes/{recipe_id}/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/login"


def test_edit_page_404_for_missing_recipe(client, test_db):
    resp = client.get("/app/recipes/999999/edit")
    assert resp.status_code == 404


def test_edit_page_prefills_fields(client, test_db):
    recipe_id = _save(
        "https://example.com/r5",
        title="Feigenkonfitüre",
        ingredients=["1,4 kg Feigen (getrocknet)", "500 g Zucker"],
        instructions=[
            {"type": "heading", "text": "Prep"},
            {"type": "step", "text": "Kochen."},
        ],
    )
    resp = client.get(f"/app/recipes/{recipe_id}/edit")
    assert resp.status_code == 200
    assert "Feigenkonfitüre" in resp.text
    assert "1,4 kg Feigen (getrocknet)" in resp.text
    assert "## Prep" in resp.text
    assert "Kochen." in resp.text


def test_edit_page_shows_cookbook_name_field_for_cookbook_url(client, test_db):
    recipe_id = _save("cookbook://cookbook/feigenkonfiture", title="Feigenkonfitüre")
    resp = client.get(f"/app/recipes/{recipe_id}/edit")
    assert 'name="cookbook_name"' in resp.text


def test_edit_page_omits_cookbook_name_field_for_url_recipes(client, test_db):
    recipe_id = _save("https://example.com/r6")
    resp = client.get(f"/app/recipes/{recipe_id}/edit")
    assert 'name="cookbook_name"' not in resp.text


def test_edit_page_linked_from_recipe_page(client, test_db):
    """Installed PWAs have no address bar, so the edit page needs a real
    (if quiet) link from the recipe page rather than being URL-only."""
    recipe_id = _save("https://example.com/r7")
    resp = client.get(f"/app/recipes/{recipe_id}")
    assert resp.status_code == 200
    assert f'href="/app/recipes/{recipe_id}/edit"' in resp.text


# --- POST /app/recipes/<id>/edit ---------------------------------------------


def _edit_form(**overrides):
    form = {
        "title": "Test Recipe",
        "lang": "en",
        "total_time": "30",
        "servings": "4",
        "ingredients": "1 cup flour\n2 eggs",
        "instructions": "Mix everything.",
        "cookbook_name": "",
    }
    form.update(overrides)
    return form


def test_edit_save_updates_ingredient_and_redirects(client, test_db):
    recipe_id = _save("https://example.com/r8", ingredients=["1,4 kg Feigen (getrocknet)"])

    resp = client.post(
        f"/app/recipes/{recipe_id}/edit",
        data=_edit_form(ingredients="1,4 kg Feigen (gerüstet)"),
    )
    assert resp.status_code == 200
    assert resp.headers["hx-redirect"] == f"/app/recipes/{recipe_id}"

    row = library.get_recipe(recipe_id)
    assert row["recipe"]["ingredients"] == ["1,4 kg Feigen (gerüstet)"]


def test_edit_save_heading_convention_parsed(client, test_db):
    recipe_id = _save("https://example.com/r9")

    resp = client.post(
        f"/app/recipes/{recipe_id}/edit",
        data=_edit_form(instructions="## Prep\nStep one\nStep two"),
    )
    assert resp.status_code == 200

    row = library.get_recipe(recipe_id)
    assert row["recipe"]["instructions"] == [
        {"type": "heading", "text": "Prep"},
        {"type": "step", "text": "Step one"},
        {"type": "step", "text": "Step two"},
    ]


def test_edit_save_empty_title_shows_error_and_leaves_recipe_unchanged(client, test_db):
    recipe_id = _save("https://example.com/r10", title="Keep Me")

    resp = client.post(f"/app/recipes/{recipe_id}/edit", data=_edit_form(title=""))
    assert resp.status_code == 400
    assert "title" in resp.text.lower()

    row = library.get_recipe(recipe_id)
    assert row["title"] == "Keep Me"


def test_edit_save_cookbook_name_updates_url_and_source(client, test_db):
    recipe_id = _save("cookbook://cookbook/feigenkonfiture", title="Feigenkonfitüre")

    resp = client.post(
        f"/app/recipes/{recipe_id}/edit",
        data=_edit_form(title="Feigenkonfitüre", cookbook_name="Kathrin"),
    )
    assert resp.status_code == 200

    row = library.get_recipe(recipe_id)
    assert row["url"] == "cookbook://kathrin/feigenkonfiture"
    with db._connect() as conn:
        source = conn.execute(
            "SELECT source FROM recipes WHERE id = ?", (recipe_id,),
        ).fetchone()["source"]
    assert source == "kathrin"


def test_edit_save_cookbook_name_collision_shows_error(client, test_db):
    # Recipe A already lives at the URL recipe B's rename would produce
    # (same path-slug "feigenkonfiture", target netloc "kathrin").
    _save("cookbook://kathrin/feigenkonfiture", title="Feigenkonfitüre")
    recipe_id_b = _save("cookbook://cookbook/feigenkonfiture", title="Feigenkonfitüre (Kopie)")

    resp = client.post(
        f"/app/recipes/{recipe_id_b}/edit",
        data=_edit_form(title="Feigenkonfitüre (Kopie)", cookbook_name="Kathrin"),
    )
    assert resp.status_code == 400
    assert "already used" in resp.text.lower()

    row = library.get_recipe(recipe_id_b)
    assert row["url"] == "cookbook://cookbook/feigenkonfiture"  # unchanged


def test_edit_save_nulls_translated_keywords(client, test_db):
    recipe_id = _save("https://example.com/r11")
    library.set_translated_keywords(recipe_id, "stale blob")

    client.post(f"/app/recipes/{recipe_id}/edit", data=_edit_form())

    with db._connect() as conn:
        row = conn.execute(
            "SELECT translated_keywords FROM recipes WHERE id = ?", (recipe_id,),
        ).fetchone()
    assert row["translated_keywords"] is None


def test_edit_save_requires_auth(anon_client, test_db):
    recipe_id = _save("https://example.com/r12")
    resp = anon_client.post(
        f"/app/recipes/{recipe_id}/edit", data=_edit_form(), follow_redirects=False,
    )
    assert resp.status_code == 303
