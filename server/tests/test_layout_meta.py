"""The panel's meta line (time · servings) under the recipe title.

`render_recipe` draws it with `_tracked`, so these tests capture that
call rather than inspecting pixels — the string is what's under test,
not the letterspacing.
"""

from rendering import layout


def _meta_line(recipe: dict, monkeypatch) -> str | None:
    """Render `recipe` and return the meta line as drawn, or None.

    The meta line is the first `_tracked` call whose font is the 11 px
    bold meta font — the section labels reuse that font, so we key on
    draw order (meta precedes "INGREDIENTS") and take the first hit.
    """
    drawn: list[str] = []
    real_tracked = layout._tracked
    monkeypatch.setattr(
        layout, "_tracked",
        lambda draw, xy, text, font, *a, **kw: (
            drawn.append(text), real_tracked(draw, xy, text, font, *a, **kw)
        )[1],
    )
    layout.render_recipe({
        "title": "Gratin",
        "ingredients": ["1 kg Kartoffeln"],
        "instructions": [{"type": "step", "text": "Backen."}],
        "lang": "fr",
        **recipe,
    })
    return drawn[0] if drawn else None


def test_meta_line_takes_the_leading_servings_count(monkeypatch):
    # Photo OCR puts whole sentences in `servings`; the panel shows the
    # first count, not every digit run together ("64").
    line = _meta_line({
        "servings": "6 personnes en accompagnement, ou pour 4 en plat principal",
    }, monkeypatch)
    assert line == "6 PORTIONS"


def test_meta_line_pairs_time_and_servings(monkeypatch):
    line = _meta_line({"total_time": 45, "servings": "4"}, monkeypatch)
    assert line == "45 MIN  ·  4 PORTIONS"


def test_meta_line_falls_back_to_raw_servings(monkeypatch):
    line = _meta_line({"servings": "une grande poêle"}, monkeypatch)
    assert line == "UNE GRANDE POÊLE"


def test_meta_line_absent_without_time_or_servings(monkeypatch):
    # Nothing to say: the first tracked draw is the section label.
    assert _meta_line({}, monkeypatch) == "INGRÉDIENTS"
