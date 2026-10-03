"""Tests for servings scaling: the quantity arithmetic, the slider's
ingredient partial, and the scaled count reaching the panel."""

from fractions import Fraction

import pytest
from PIL import Image

import library
from display import persistence as display_persistence
from display import state as display_state
from display.push import push_recipe_to_display
from processing.scaling import (
    base_servings, clamp_multiplier, clamp_servings, effective_scale, format_multiplier,
    scale_ingredient, scale_recipe, slider_max,
)


# --- arithmetic -------------------------------------------------------------


@pytest.mark.parametrize("line, factor, expected", [
    ("200 g Mehl", 2, "400 g Mehl"),
    ("200g Mehl", Fraction(3, 2), "300g Mehl"),
    ("3 Eier", Fraction(1, 3), "1 Eier"),
    ("1 Ei", Fraction(1, 2), "½ Ei"),
    ("½ TL Salz", 2, "1 TL Salz"),
    ("1½ cups flour", 2, "3 cups flour"),
    ("1/2 tsp salt", 3, "1 1/2 tsp salt"),
    ("1 1/2 cups milk", Fraction(1, 3), "1/2 cups milk"),
    ("1,5 dl Rahm", Fraction(5, 3), "2,5 dl Rahm"),
    ("0.5 l stock", Fraction(3, 2), "0.8 l stock"),
    ("1.5 dl cream", 2, "3 dl cream"),
    ("2-3 Knoblauchzehen", 2, "4-6 Knoblauchzehen"),
    ("2 – 3 gousses d'ail", 2, "4 – 6 gousses d'ail"),
    ("250 g Butter", Fraction(4, 3), "333 g Butter"),
    ("0.25 tsp cayenne", Fraction(1, 6), "0.04 tsp cayenne"),
])
def test_scale_ingredient(line, factor, expected):
    assert scale_ingredient(line, Fraction(factor)) == expected


def test_only_the_leading_quantity_scales():
    # The 400 g is the can size — doubling the recipe means two cans.
    assert scale_ingredient("1 Dose (400 g) Tomaten", Fraction(2)) == "2 Dose (400 g) Tomaten"


def test_lines_without_a_leading_number_pass_through():
    assert scale_ingredient("Salz, Pfeffer", Fraction(2)) == "Salz, Pfeffer"
    assert scale_ingredient("Olivenöl zum Braten", Fraction(1, 2)) == "Olivenöl zum Braten"


def test_decimals_follow_the_recipe_language():
    assert scale_ingredient("1 dl Milch", Fraction(13, 10), lang="de") == "1,3 dl Milch"
    assert scale_ingredient("1 cup milk", Fraction(13, 10), lang="en") == "1.3 cup milk"


def test_base_servings():
    assert base_servings("4") == 4
    assert base_servings("4 servings") == 4
    assert base_servings("Pour 6 personnes") == 6
    assert base_servings("4-6") == 4
    assert base_servings("une grande poêle") is None
    assert base_servings(None) is None
    assert base_servings("0") is None


def test_slider_max():
    assert slider_max(2) == 12
    assert slider_max(6) == 18
    assert slider_max(20) == 24


def test_clamp_servings():
    assert clamp_servings("8") == 8
    assert clamp_servings("0") is None
    assert clamp_servings("999") is None
    assert clamp_servings("abc") is None
    assert clamp_servings("") is None


def test_scale_recipe():
    recipe = {"servings": "4 Portionen", "lang": "de", "ingredients": ["200 g Mehl", "Salz"]}
    scaled = scale_recipe(recipe, 8)
    assert scaled["ingredients"] == ["400 g Mehl", "Salz"]
    assert scaled["servings"] == "8"
    # Never mutates the stored recipe.
    assert recipe["ingredients"] == ["200 g Mehl", "Salz"]
    # Nothing to do → the very same object back.
    assert scale_recipe(recipe, 4) is recipe
    assert scale_recipe(recipe, None) is recipe
    assert scale_recipe({"servings": None, "ingredients": ["1 Ei"]}, 8)["ingredients"] == ["1 Ei"]


def test_scale_recipe_by_multiplier():
    recipe = {"servings": "une grande poêle", "lang": "fr", "ingredients": ["300 g riz", "Sel"]}
    scaled = scale_recipe(recipe, multiplier=1.5)
    assert scaled["ingredients"] == ["450 g riz", "Sel"]
    assert scaled["servings"] == "une grande poêle"
    assert scaled["scale"] == "×1½"
    assert scale_recipe(recipe, multiplier=1) is recipe
    assert scale_recipe({"ingredients": ["2 Eier"]}, multiplier=0.5)["ingredients"] == ["1 Eier"]


def test_each_knob_only_scales_its_kind_of_recipe():
    counted = {"servings": "4", "ingredients": ["200 g Mehl"]}
    uncounted = {"servings": None, "ingredients": ["200 g Mehl"]}
    assert scale_recipe(counted, multiplier=2) is counted
    assert scale_recipe(uncounted, servings=8) is uncounted
    assert effective_scale(counted, 8, 2.0) == (8, None)
    assert effective_scale(uncounted, 8, 2.0) == (None, 2.0)
    assert effective_scale(uncounted, None, 1.0) == (None, None)


def test_format_multiplier():
    assert format_multiplier(2) == "×2"
    assert format_multiplier(1.5) == "×1½"
    assert format_multiplier(0.5) == "×½"
    assert format_multiplier(4.0) == "×4"


def test_clamp_multiplier():
    assert clamp_multiplier("1.5") == 1.5
    assert clamp_multiplier("4") == 4.0
    assert clamp_multiplier("0.25") is None   # off the half-step grid
    assert clamp_multiplier("5") is None
    assert clamp_multiplier("0") is None
    assert clamp_multiplier("nan") is None
    assert clamp_multiplier("") is None


# --- web --------------------------------------------------------------------


def _saved(servings="4", ingredients=("500 g Zopfmehl", "1 Ei", "Salz")):
    rid = library.upsert_recipe("https://example.ch/zopf", {
        "title": "Zopf",
        "ingredients": list(ingredients),
        "instructions": [{"type": "step", "text": "Backen."}],
        "total_time": 60,
        "servings": servings,
        "lang": "de",
    })
    library.save_recipe(rid)
    return rid


@pytest.fixture
def fake_render(monkeypatch):
    """Capture what reaches the panel renderer instead of drawing it."""
    seen = []

    def render(recipe, page=1, source=None):
        seen.append(recipe)
        return Image.new("1", (8, 8), 1), 1

    monkeypatch.setattr(display_state, "render_recipe", render)
    display_state.clear()
    yield seen
    display_state.clear()


def test_recipe_page_shows_servings_slider(client):
    rid = _saved()
    html = client.get(f"/app/recipes/{rid}").text
    assert 'name="servings"' in html and ">Serves<" in html
    assert 'value="4"' in html
    assert 'hx-include="#scale-range"' in html
    # The count lives on the slider; the static fact line drops it.
    assert "Serves 4" not in html


def test_recipe_without_a_count_gets_a_batch_slider(client):
    rid = _saved(servings="une grande poêle")
    html = client.get(f"/app/recipes/{rid}").text
    assert 'name="multiplier"' in html and ">Batch<" in html
    assert 'min="0.5"' in html and 'max="4.0"' in html and 'value="1"' in html
    assert "×1" in html
    assert 'hx-include="#scale-range"' in html
    # Count-less servings text stays on the facts line.
    assert "une grande poêle" in html


def test_no_slider_without_ingredients(client):
    rid = _saved(ingredients=())
    html = client.get(f"/app/recipes/{rid}").text
    assert 'id="scale-range"' not in html
    assert "hx-include" not in html


def test_ingredients_partial_scales(client):
    rid = _saved()
    html = client.get(f"/app/recipes/{rid}/_ingredients?servings=8").text
    assert "1000 g Zopfmehl" in html and "2 Ei" in html and "Salz" in html


def test_ingredients_partial_ignores_junk(client):
    rid = _saved()
    html = client.get(f"/app/recipes/{rid}/_ingredients?servings=9999").text
    assert "500 g Zopfmehl" in html


def test_ingredients_partial_scales_by_multiplier(client):
    rid = _saved(servings=None)
    html = client.get(f"/app/recipes/{rid}/_ingredients?multiplier=1.5").text
    assert "750 g Zopfmehl" in html and "1½ Ei" in html


def test_share_page_has_no_slider(client):
    rid = _saved()
    link = client.post(f"/app/recipes/{rid}/share").text
    token = link.split("/app/s/")[1].split('"')[0].split("<")[0]
    html = client.get(f"/app/s/{token}").text
    assert "Zopf" in html and 'id="scale-range"' not in html


def test_push_scales_the_panel(client, fake_render):
    rid = _saved()
    r = client.post(f"/app/recipes/{rid}/push", data={"servings": "2"})
    assert r.status_code == 200
    assert fake_render[-1]["ingredients"][0] == "250 g Zopfmehl"
    assert display_state.get()["servings"] == 2
    # The page then opens at the panel's count.
    assert 'value="2"' in client.get(f"/app/recipes/{rid}").text


def test_push_at_a_new_count_re_renders(test_db, fake_render):
    row = library.get_recipe(_saved())
    assert push_recipe_to_display(row, servings=8)
    n = len(fake_render)
    assert push_recipe_to_display(row, servings=8)  # no-op
    assert len(fake_render) == n
    assert push_recipe_to_display(row)  # back to as written
    assert len(fake_render) > n
    assert fake_render[-1]["ingredients"][0] == "500 g Zopfmehl"
    assert display_state.get()["servings"] is None


def test_push_at_the_written_count_is_as_written(test_db, fake_render):
    row = library.get_recipe(_saved())
    assert push_recipe_to_display(row, servings=4)
    assert display_state.get()["servings"] is None


def test_scaled_panel_survives_a_restart(test_db, fake_render, monkeypatch):
    monkeypatch.setattr(display_state, "_change_listener", display_persistence.persist_current)
    row = library.get_recipe(_saved())
    push_recipe_to_display(row, servings=6)
    assert library.get_panel_state()["servings"] == 6

    monkeypatch.setattr(display_state, "_change_listener", None)
    display_state.clear()
    display_persistence.restore_on_startup()
    assert display_state.get()["servings"] == 6
    assert fake_render[-1]["ingredients"][0] == "750 g Zopfmehl"


def test_push_by_multiplier(client, fake_render, monkeypatch):
    monkeypatch.setattr(display_state, "_change_listener", display_persistence.persist_current)
    rid = _saved(servings=None)
    assert client.post(f"/app/recipes/{rid}/push", data={"multiplier": "2"}).status_code == 200
    assert fake_render[-1]["ingredients"][0] == "1000 g Zopfmehl"
    assert fake_render[-1]["scale"] == "×2"
    assert display_state.get()["multiplier"] == 2.0
    assert library.get_panel_state()["multiplier"] == 2.0
    # The page then opens at the panel's multiplier.
    html = client.get(f"/app/recipes/{rid}").text
    assert 'value="2"' in html and "×2" in html

    monkeypatch.setattr(display_state, "_change_listener", None)
    display_state.clear()
    display_persistence.restore_on_startup()
    assert display_state.get()["multiplier"] == 2.0
    assert fake_render[-1]["scale"] == "×2"
