from processing.jsonld import _parse_duration, parse_recipe_jsonld, resolve_url


def test_parse_duration_iso8601():
    assert _parse_duration("PT1H30M") == 90
    assert _parse_duration("PT45M") == 45
    assert _parse_duration("P1DT2H") == 26 * 60
    assert _parse_duration("nonsense") is None
    assert _parse_duration(None) is None
    assert _parse_duration(35) == 35


def test_parse_recipe_from_graph_document():
    doc = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "site"},
            {
                "@type": "Recipe",
                "name": "Tarte",
                "recipeIngredient": ["pâte", "pommes"],
                "recipeInstructions": [
                    {"@type": "HowToSection", "name": "Base",
                     "itemListElement": [{"@type": "HowToStep", "text": "Étaler la pâte."}]},
                    {"@type": "HowToStep", "text": "Cuire 30 minutes."},
                ],
                "totalTime": "PT50M",
                "recipeYield": "4",
                "inLanguage": "fr-CH",
                "url": "https://a.ch/tarte",
            },
        ],
    }
    parsed = parse_recipe_jsonld(doc)
    assert parsed is not None
    recipe, source_url = parsed
    assert recipe["title"] == "Tarte"
    assert recipe["lang"] == "fr"
    assert recipe["total_time"] == 50
    assert source_url == "https://a.ch/tarte"
    assert recipe["instructions"][0] == {"type": "heading", "text": "Base"}
    assert recipe["instructions"][1]["type"] == "step"


def test_parse_returns_none_without_recipe_or_required_fields():
    assert parse_recipe_jsonld({"@type": "WebSite"}) is None
    assert parse_recipe_jsonld({"@type": "Recipe", "name": "T"}) is None


def test_resolve_url_variants():
    recipe = {"title": "T", "ingredients": ["x"], "instructions": []}
    # Real URLs and named cookbook URLs pass through.
    assert resolve_url("https://a.ch/r", recipe) == "https://a.ch/r"
    assert resolve_url("cookbook://ottolenghi/simple", recipe) == "cookbook://ottolenghi/simple"
    # Bare markers collapse to a content hash — and dedupe by content.
    hashed = resolve_url("cookbook://", recipe)
    assert hashed.startswith("cookbook://") and len(hashed) > len("cookbook://")
    assert resolve_url("cookbook://", dict(recipe)) == hashed
    # Empty falls back to the jsonld: hash form.
    assert resolve_url("", recipe).startswith("jsonld:")
