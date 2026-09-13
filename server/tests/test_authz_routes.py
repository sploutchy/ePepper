"""Route-level enforcement: what each access code may actually call.

Hiding a button is cosmetic. These tests go at the HTTP layer, and where a
route has a side effect they assert the side effect *didn't happen* — a 403
that still deleted the recipe would pass a status-code-only test.
"""

import pytest

import authz
import library
from display import state as display_state


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
    return recipe_id


# Every /app route, with the permission it requires. The executable twin of
# the matrix in the README.
ROUTES = [
    ("GET", "/app/", authz.LIBRARY_VIEW, None),
    ("GET", "/app/_search", authz.LIBRARY_VIEW, None),
    ("GET", "/app/recipes/{id}", authz.LIBRARY_VIEW, None),
    ("GET", "/app/add", authz.LIBRARY_ADD, None),
    ("POST", "/app/add/url", authz.LIBRARY_ADD, {"url": "https://example.com/tarte"}),
    ("POST", "/app/add/file", authz.LIBRARY_ADD, {}),
    ("GET", "/app/status", authz.STATUS_VIEW, None),
    ("GET", "/app/_status", authz.STATUS_VIEW, None),
    ("POST", "/app/display/clear", authz.DISPLAY_CONTROL, {}),
    ("POST", "/app/recipes/{id}/tags", authz.LIBRARY_EDIT, {"tags": "vegi"}),
    ("GET", "/app/recipes/{id}/edit", authz.LIBRARY_EDIT, None),
    ("POST", "/app/recipes/{id}/edit", authz.LIBRARY_EDIT, {"title": "x"}),
    ("POST", "/app/recipes/{id}/push", authz.DISPLAY_PUSH, {}),
    ("DELETE", "/app/recipes/{id}", authz.LIBRARY_DELETE, None),
    ("GET", "/app/flash", authz.DEVICE_ADMIN, None),
    ("GET", "/app/flash/manifest.json", authz.DEVICE_ADMIN, None),
]


def _call(client, method, path, data, recipe_id):
    path = path.format(id=recipe_id)
    kwargs = {"follow_redirects": False}
    if data is not None:
        kwargs["data"] = data
    return client.request(method, path, **kwargs)


# --- the viewer matrix ------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, permission, data",
    [pytest.param(*r, id=f"{r[0]} {r[1]}") for r in ROUTES],
)
def test_viewer_reaches_only_the_library(
    viewer_client, test_db, method, path, permission, data,
):
    recipe_id = _recipe()
    resp = _call(viewer_client, method, path, data, recipe_id)
    if permission == authz.LIBRARY_VIEW:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 403


@pytest.mark.parametrize(
    "method, path, permission, data",
    [pytest.param(*r, id=f"{r[0]} {r[1]}") for r in ROUTES],
)
def test_admin_reaches_everything(client, test_db, method, path, permission, data):
    """The mirror assertion: nothing got locked away from the admin code.

    Status codes vary (a missing firmware file 404s, an empty upload 422s);
    the claim under test is only that no route denies admin.
    """
    recipe_id = _recipe()
    resp = _call(client, method, path, data, recipe_id)
    assert resp.status_code != 403


# --- enforcement, not just status codes -------------------------------------


def test_viewer_cannot_delete_a_recipe(viewer_client, test_db):
    recipe_id = _recipe()
    resp = viewer_client.delete(f"/app/recipes/{recipe_id}", follow_redirects=False)
    assert resp.status_code == 403
    assert library.get_recipe(recipe_id) is not None


def test_viewer_cannot_add_a_recipe(viewer_client, test_db):
    """Also the path that spends LLM credit — worth its own assertion."""
    before = library.count_saved()
    resp = viewer_client.post(
        "/app/add/url",
        data={"url": "https://example.com/somewhere-new"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert library.find_by_url("https://example.com/somewhere-new") is None
    assert library.count_saved() == before


def test_viewer_cannot_push_to_the_panel(viewer_client, test_db):
    recipe_id = _recipe()
    before = display_state.get()
    resp = viewer_client.post(
        f"/app/recipes/{recipe_id}/push", follow_redirects=False,
    )
    assert resp.status_code == 403
    assert display_state.get()["hash"] == before["hash"]


def test_viewer_cannot_edit_tags(viewer_client, test_db):
    recipe_id = _recipe()
    library.set_tags(recipe_id, ["vegi"])
    resp = viewer_client.post(
        f"/app/recipes/{recipe_id}/tags",
        data={"tags": "dessert"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert library.get_recipe(recipe_id)["tags"] == ["vegi"]


def test_viewer_cannot_download_the_merged_firmware(viewer_client, test_db):
    """The merged image carries the baked WiFi password + API key, so this
    route is the one that would turn a guest code into full admin."""
    resp = viewer_client.get(
        "/app/flash/epepper-merged.bin", follow_redirects=False,
    )
    assert resp.status_code == 403


def test_viewer_cookie_does_not_unlock_the_panel_render(viewer_client, test_db):
    """/image is the one device endpoint that accepts a session cookie."""
    assert viewer_client.get("/image").status_code == 401


def test_admin_cookie_still_unlocks_the_panel_render(client, test_db):
    assert client.get("/image").status_code != 401


def test_bearer_token_is_unaffected_by_access_codes(anon_client, test_db):
    """API_KEY keeps working exactly as before — the device never sees any
    of this."""
    from config import API_KEY
    resp = anon_client.get("/version", headers={"Authorization": f"Bearer {API_KEY}"})
    assert resp.status_code == 200


# --- partial grants ---------------------------------------------------------


def test_explicit_permission_list_is_honoured(make_client, test_db):
    """A code granted a hand-written matrix gets exactly that matrix."""
    c = make_client(grants="library.view|display.push")
    recipe_id = _recipe()
    assert c.get(f"/app/recipes/{recipe_id}", follow_redirects=False).status_code == 200
    assert c.post(
        f"/app/recipes/{recipe_id}/push", follow_redirects=False,
    ).status_code != 403
    assert c.get("/app/add", follow_redirects=False).status_code == 403
    assert c.get("/app/status", follow_redirects=False).status_code == 403


def test_editor_reaches_everything_but_firmware(make_client, test_db):
    c = make_client(grants="editor")
    recipe_id = _recipe()
    assert c.get("/app/add", follow_redirects=False).status_code == 200
    assert c.get("/app/status", follow_redirects=False).status_code == 200
    assert c.delete(
        f"/app/recipes/{recipe_id}", follow_redirects=False,
    ).status_code != 403
    assert c.get("/app/flash", follow_redirects=False).status_code == 403


# --- signed out -------------------------------------------------------------


def test_anon_is_redirected_to_login(anon_client, test_db):
    resp = anon_client.get("/app/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/login"


def test_anon_htmx_gets_hx_redirect(anon_client, test_db):
    """A 303 would swap the login page into the HTMX target."""
    resp = anon_client.get(
        "/app/_search", headers={"HX-Request": "true"}, follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/app/login"


def test_denied_htmx_request_gets_a_readable_toast(viewer_client, test_db):
    """A tab left open across a permission change shouldn't fail silently."""
    recipe_id = _recipe()
    resp = viewer_client.post(
        f"/app/recipes/{recipe_id}/push",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert "access code" in resp.text.lower()


def test_denied_page_request_renders_the_403_page(viewer_client, test_db):
    """Not a redirect to login: a signed-in guest bounced to the login form
    would read it as 'your code stopped working'."""
    resp = viewer_client.get("/app/status", follow_redirects=False)
    assert resp.status_code == 403
    assert "Not available" in resp.text
    assert "/app/login" not in resp.headers.get("location", "")


# --- login / logout ---------------------------------------------------------


def _login(client, code):
    return client.post(
        "/app/login", data={"api_key": code}, follow_redirects=False,
    )


def test_login_with_an_access_code_signs_in_as_that_principal(make_client, test_db):
    c = make_client(grants="viewer", code="test-access-code-9876")
    c.cookies.clear()
    resp = _login(c, "test-access-code-9876")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/"
    # And the session it minted really is a viewer session.
    assert c.get("/app/add", follow_redirects=False).status_code == 403
    assert c.get("/app/", follow_redirects=False).status_code == 200


def test_login_with_the_api_key_still_signs_in_as_admin(anon_client, test_db):
    from config import API_KEY
    resp = _login(anon_client, API_KEY)
    assert resp.status_code == 303
    assert anon_client.get("/app/add", follow_redirects=False).status_code == 200


def test_login_with_a_wrong_code_sets_no_cookie(anon_client, test_db):
    from api.web import COOKIE_NAME
    resp = _login(anon_client, "definitely-not-a-code")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/login?error=1"
    assert COOKIE_NAME not in resp.cookies


def test_logout_clears_the_session(anon_client, test_db):
    """A full round trip: sign in, confirm access, sign out, confirm it's
    gone. Asserting on the Set-Cookie header alone wouldn't catch attributes
    that don't match the ones login set (the browser then keeps the original).
    """
    from config import API_KEY
    _login(anon_client, API_KEY)
    assert anon_client.get("/app/", follow_redirects=False).status_code == 200

    resp = anon_client.post("/app/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/login"
    assert anon_client.get("/app/", follow_redirects=False).status_code == 303


def test_logout_without_a_session_is_not_an_error(anon_client, test_db):
    assert anon_client.post(
        "/app/logout", follow_redirects=False,
    ).status_code == 303


# --- the no-access-codes default -------------------------------------------


def test_without_access_codes_admin_reaches_everything(client, test_db):
    """The dark-ship guarantee: an installation that sets no ACCESS_CODES
    behaves exactly as it did before access levels existed."""
    import config
    assert config.ACCESS_CODES == []
    recipe_id = _recipe()
    for method, path, _permission, data in ROUTES:
        resp = _call(client, method, path, data, recipe_id)
        assert resp.status_code != 403, f"{method} {path}"
