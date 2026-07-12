from library.db import normalize_url


def test_lowercases_scheme_and_host():
    assert normalize_url("HTTPS://Example.COM/Recipe") == "https://example.com/Recipe"


def test_strips_fragment():
    assert normalize_url("https://a.ch/r#steps") == "https://a.ch/r"


def test_strips_trailing_slash_but_not_root():
    assert normalize_url("https://a.ch/r/") == "https://a.ch/r"
    assert normalize_url("https://a.ch/") == "https://a.ch/"


def test_drops_tracking_params_keeps_content_params():
    url = "https://a.ch/r?utm_source=x&menge=60&gclid=1&fbclid=2&ref=mail"
    assert normalize_url(url) == "https://a.ch/r?menge=60"


def test_equivalent_forms_collide():
    a = normalize_url("https://Fooby.ch/fr/recettes/123/pizza/?utm_campaign=nl#top")
    b = normalize_url("https://fooby.ch/fr/recettes/123/pizza")
    assert a == b
