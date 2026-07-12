import time

from api.web import _bucket_recipes, _fmt_servings, _sanitize_tag, _sanitize_tier


def test_sanitize_tag():
    assert _sanitize_tag("#Vegi ") == "vegi"
    assert _sanitize_tag("main course") == "main course"
    assert _sanitize_tag("soirée-d'été") is None  # apostrophe rejected
    assert _sanitize_tag("100%") is None          # LIKE wildcard rejected
    assert _sanitize_tag("under_score") == "under_score"
    assert _sanitize_tag("") is None
    assert _sanitize_tag(None) is None


def test_sanitize_tier():
    assert _sanitize_tier("this-week") == "this-week"
    assert _sanitize_tier("<script>") == ""
    assert _sanitize_tier(None) == ""


def test_fmt_servings():
    assert _fmt_servings("4 servings") == "Serves 4"
    assert _fmt_servings("Pour 4 personnes") == "Serves 4"
    assert _fmt_servings("4-6") == "Serves 4-6"
    assert _fmt_servings("une grande poêle") == "une grande poêle"
    assert _fmt_servings("") is None
    assert _fmt_servings(None) is None


def test_bucket_recipes_tiers():
    now = int(time.time())
    day = 86400
    rows = [
        {"id": 1, "last_displayed_at": now - 3600},       # this week
        {"id": 2, "last_displayed_at": now - 10 * day},   # this month
        {"id": 3, "last_displayed_at": now - 100 * day},  # earlier this year
        {"id": 4, "last_displayed_at": now - 400 * day},  # older
        {"id": 5, "last_displayed_at": None},             # never
    ]
    tiers = _bucket_recipes(rows)
    assert [(slug, [r["id"] for r in rs]) for slug, _, rs in tiers] == [
        ("this-week", [1]),
        ("this-month", [2]),
        ("this-year", [3]),
        ("older", [4]),
        ("never", [5]),
    ]


def test_bucket_recipes_drops_empty_tiers():
    now = int(time.time())
    tiers = _bucket_recipes([{"id": 1, "last_displayed_at": now}])
    assert len(tiers) == 1 and tiers[0][0] == "this-week"
