from processing.recipes import (
    _clean_instructions,
    _coerce_int,
    normalize_recipe_for_render,
    validate_llm_recipe,
)


def test_minimal_valid_recipe():
    out = validate_llm_recipe({
        "title": "Rösti",
        "ingredients": ["1 kg Kartoffeln"],
        "instructions": [{"type": "step", "text": "Reiben."}],
        "lang": "de",
    })
    assert out is not None
    assert out["title"] == "Rösti"
    assert out["lang"] == "de"


def test_rejects_missing_title_and_empty_body():
    assert validate_llm_recipe({"ingredients": ["x"]}) is None
    assert validate_llm_recipe({"title": "T"}) is None
    assert validate_llm_recipe("not a dict") is None


def test_unknown_lang_falls_back_to_en():
    out = validate_llm_recipe({
        "title": "T", "ingredients": ["x"], "instructions": [], "lang": "nl",
    })
    assert out["lang"] == "en"


def test_swissify_replaces_eszett_everywhere():
    out = validate_llm_recipe({
        "title": "Süßkartoffel",
        "servings": "große Portion",
        "ingredients": ["Süßrahm"],
        "instructions": [{"type": "step", "text": "Gießen."}],
        "lang": "de",
    })
    assert out["title"] == "Süsskartoffel"
    assert out["servings"] == "grosse Portion"
    assert out["ingredients"] == ["Süssrahm"]
    assert out["instructions"][0]["text"] == "Giessen."


def test_string_instructions_are_split_into_steps():
    out = validate_llm_recipe({
        "title": "T", "ingredients": [], "instructions": "Step one\nStep two",
    })
    assert [i["text"] for i in out["instructions"]] == ["Step one", "Step two"]


def test_coerce_int():
    assert _coerce_int(45) == 45
    assert _coerce_int("about 30 minutes") == 30
    assert _coerce_int(0) is None
    assert _coerce_int(True) is None
    assert _coerce_int(None) is None


def test_normalize_drops_empty_and_duplicate_headings():
    recipe = {
        "title": "T",
        "instructions": [
            {"type": "heading", "text": "Prep"},
            {"type": "step", "text": "Chop."},
            {"type": "heading", "text": "Prep"},        # duplicate of last kept
            {"type": "step", "text": "Mix."},
            {"type": "heading", "text": "A"},
            {"type": "heading", "text": "B"},           # run: keep only last
            {"type": "step", "text": ""},               # empty: dropped
            {"type": "step", "text": "Bake."},
        ],
    }
    out = normalize_recipe_for_render(recipe)
    assert out["instructions"] == [
        {"type": "heading", "text": "Prep"},
        {"type": "step", "text": "Chop."},
        {"type": "step", "text": "Mix."},
        {"type": "heading", "text": "B"},
        {"type": "step", "text": "Bake."},
    ]
    # Idempotent, and the input is not mutated.
    assert normalize_recipe_for_render(out)["instructions"] == out["instructions"]
    assert len(recipe["instructions"]) == 8


def test_clean_instructions_strips_step_headings_and_numbers():
    text = "Step 1\nChop the onions.\nSchritt 2\nMix everything."
    out = _clean_instructions(text)
    assert [i["text"] for i in out] == ["Chop the onions.", "Mix everything."]
