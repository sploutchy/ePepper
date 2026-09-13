"""Tests for signed, expiring, read-only recipe share links.

Three concerns, in order of how much they'd hurt to get wrong: the token
can't be forged or outlived, the shared page hands out only the recipe, and
the link doesn't leak through the side channels that a credential-in-a-URL
has (Referer, search engines, the access log).
"""

import logging
import time

import pytest

import library
import main
import sharing
from config import API_KEY

KEY = "test-signing-key-0123456789"


def _recipe(url="https://example.com/tarte", title="Tarte aux figues"):
    recipe_id = library.upsert_recipe(url, {
        "title": title,
        "ingredients": ["1 kg figues", "1 pâte brisée"],
        "instructions": [{"type": "step", "text": "Enfourner 35 minutes."}],
        "total_time": 45,
        "servings": "6",
        "lang": "fr",
    })
    library.save_recipe(recipe_id)
    library.set_tags(recipe_id, ["dessert"])
    return recipe_id


# --- the token --------------------------------------------------------------


def test_round_trip():
    token = sharing.mint(KEY, 168)
    assert sharing.read(KEY, token) == 168


def test_token_is_short_enough_to_paste():
    """It goes in a message someone reads. 60 chars leaves room for the
    host without the link wrapping over three lines on a phone."""
    assert len(sharing.mint(KEY, 168)) < 60


def test_expires_after_24_hours():
    now = int(time.time())
    token = sharing.mint(KEY, 168, now=now)
    assert sharing.read(KEY, token, now=now + 24 * 3600 - 1) == 168
    with pytest.raises(sharing.ShareTokenExpired):
        sharing.read(KEY, token, now=now + 24 * 3600)


def test_expiry_is_reported_for_the_link_copy():
    now = int(time.time())
    assert sharing.expires_at(KEY, sharing.mint(KEY, 168, now=now)) == now + 86400


@pytest.mark.parametrize("token", [
    "",
    "junk",
    "v1.168",
    "v1.168.9999999999",
    "v1.168.9999999999.short",
    "v2.168.9999999999.uaTOSPTKVnoWxBGgex79kQ",
])
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(sharing.ShareTokenInvalid):
        sharing.read(KEY, token)


def test_cannot_swap_the_recipe_id():
    """The obvious attack: take a link you were given, point it at another
    recipe."""
    token = sharing.mint(KEY, 168)
    with pytest.raises(sharing.ShareTokenInvalid):
        sharing.read(KEY, token.replace(".168.", ".169.", 1))


def test_cannot_extend_the_expiry():
    now = int(time.time())
    token = sharing.mint(KEY, 168, now=now)
    forged = token.replace(f".{now + 86400}.", f".{now + 86400 * 365}.", 1)
    with pytest.raises(sharing.ShareTokenInvalid):
        sharing.read(KEY, forged)


def test_non_canonical_ids_are_rejected():
    """A padded id re-renders to the same integer; verifying over the claims
    as they arrived means the MAC won't match."""
    token = sharing.mint(KEY, 168)
    with pytest.raises(sharing.ShareTokenInvalid):
        sharing.read(KEY, token.replace(".168.", ".0168.", 1))


def test_rotating_the_api_key_invalidates_every_link():
    """The only way to revoke outstanding links, so it had better work."""
    token = sharing.mint(KEY, 168)
    with pytest.raises(sharing.ShareTokenInvalid):
        sharing.read("a-different-api-key-987654", token)


def test_a_session_cookie_is_not_a_share_token():
    """Both are HMACs keyed by API_KEY; domain separation is what stops one
    being replayed as the other."""
    from api.web import session_cookie_value
    with pytest.raises(sharing.ShareTokenInvalid):
        sharing.read(API_KEY, session_cookie_value())


# --- minting, from the app --------------------------------------------------


def test_share_button_mints_a_working_link(client, anon_client, test_db):
    recipe_id = _recipe()
    resp = client.post(f"/app/recipes/{recipe_id}/share")
    assert resp.status_code == 200

    url = resp.text.split('value="', 1)[1].split('"', 1)[0]
    assert "/app/s/v1." in url
    # The minted link opens the recipe for someone with no session at all.
    shared = anon_client.get(url.split("testserver", 1)[1])
    assert shared.status_code == 200
    assert "Tarte aux figues" in shared.text


def test_minting_requires_a_session(anon_client, test_db):
    recipe_id = _recipe()
    resp = anon_client.post(
        f"/app/recipes/{recipe_id}/share", follow_redirects=False,
    )
    assert resp.status_code == 303


def test_minting_a_link_for_a_missing_recipe_404s(client, test_db):
    assert client.post("/app/recipes/9999/share").status_code == 404


def test_the_copy_panel_states_the_expiry_and_that_it_cannot_be_withdrawn(
    client, test_db,
):
    """The one thing a person needs to know before sending it."""
    resp = client.post(f"/app/recipes/{_recipe()}/share")
    assert "tomorrow at" in resp.text
    assert "can't be withdrawn" in resp.text


def test_the_token_is_never_logged(client, test_db, caplog):
    with caplog.at_level(logging.INFO):
        resp = client.post(f"/app/recipes/{_recipe()}/share")
    token = resp.text.split("/app/s/", 1)[1].split('"', 1)[0]
    assert token not in caplog.text
    assert "Share link minted" in caplog.text


# --- the shared page --------------------------------------------------------


def _share_path(client, recipe_id):
    resp = client.post(f"/app/recipes/{recipe_id}/share")
    return "/app/s/" + resp.text.split("/app/s/", 1)[1].split('"', 1)[0]


def test_shared_page_shows_the_recipe(client, anon_client, test_db):
    path = _share_path(client, _recipe())
    html = anon_client.get(path).text
    assert "Tarte aux figues" in html
    assert "1 kg figues" in html
    assert "Enfourner 35 minutes." in html


def test_shared_page_is_a_dead_end(client, anon_client, test_db):
    """No nav, no sign-in prompt, no links into an app the visitor can't
    open — and nothing that would let the link become a session."""
    path = _share_path(client, _recipe())
    resp = anon_client.get(path)
    html = resp.text
    assert 'href="/app/"' not in html
    assert "/app/login" not in html
    assert "/app/add" not in html
    assert "Push to display" not in html
    assert "hx-delete" not in html
    assert not resp.cookies


def test_shared_page_omits_the_owners_kitchen_history(
    client, anon_client, test_db,
):
    """When it was saved and last cooked are facts about a kitchen, not
    about the recipe. Tags are filing, and their links would 404 anyway."""
    recipe_id = _recipe()
    library.touch_displayed(recipe_id)
    html = anon_client.get(_share_path(client, recipe_id)).text
    assert "saved" not in html
    assert "cooked" not in html
    assert "dessert" not in html


def test_shared_page_still_credits_the_source(client, anon_client, test_db):
    html = anon_client.get(_share_path(client, _recipe())).text
    assert "example.com" in html


def test_a_link_opens_only_its_own_recipe(client, anon_client, test_db):
    _recipe("https://example.com/tarte", "Tarte aux figues")
    other = _recipe("https://example.com/soupe", "Soupe de courge")
    html = anon_client.get(_share_path(client, other)).text
    assert "Soupe de courge" in html
    assert "Tarte aux figues" not in html


# --- leak-prevention headers ------------------------------------------------


def test_shared_page_sends_no_referrer(client, anon_client, test_db):
    """Without this the share URL — a credential — travels to Google in the
    Referer of the font request the page makes."""
    resp = anon_client.get(_share_path(client, _recipe()))
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert 'name="referrer" content="no-referrer"' in resp.text


def test_shared_page_is_not_indexable(client, anon_client, test_db):
    resp = anon_client.get(_share_path(client, _recipe()))
    assert "noindex" in resp.headers["X-Robots-Tag"]
    assert resp.headers["Cache-Control"] == "no-store"


def test_access_log_redacts_the_token():
    """uvicorn records the full path; a share path is a working credential.
    Same standard that got the `?key=` query param removed."""
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/app/s/v1.168.1789388694.uaTOSPTKVnoWxBGgex79kQ",
         "1.1", 200),
        None,
    )
    main._RedactShareTokens().filter(record)
    assert "uaTOSPTKVnoWxBGgex79kQ" not in record.getMessage()
    assert "/app/s/<redacted>" in record.getMessage()


def test_access_log_filter_leaves_other_paths_alone():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/app/recipes/168", "1.1", 200), None,
    )
    main._RedactShareTokens().filter(record)
    assert "/app/recipes/168" in record.getMessage()


def test_access_log_filter_survives_unexpected_args():
    """A logging filter that raises takes out the request it describes."""
    for args in (None, (), ("only-one",), ("a", "b", 3)):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, "%s", args, None,
        )
        assert main._RedactShareTokens().filter(record) is True


# --- expired and deleted ----------------------------------------------------


def test_expired_link_says_so(anon_client, test_db):
    recipe_id = _recipe()
    stale = sharing.mint(API_KEY, recipe_id, now=int(time.time()) - 86401)
    resp = anon_client.get(f"/app/s/{stale}")
    assert resp.status_code == 410
    assert "expired" in resp.text.lower()


def test_forged_link_404s(anon_client, test_db):
    assert anon_client.get("/app/s/v1.1.9999999999.notavalidmac").status_code == 404


def test_link_to_a_deleted_recipe_does_not_resurrect_it(
    client, anon_client, test_db,
):
    recipe_id = _recipe()
    path = _share_path(client, recipe_id)
    library.delete_recipe(recipe_id)
    resp = anon_client.get(path)
    assert resp.status_code == 410
    assert "Tarte aux figues" not in resp.text
