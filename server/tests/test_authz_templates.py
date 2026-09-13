"""GUI gating: a function an access code can't use isn't in the page.

Each test has a mirror assertion for admin. The realistic failure mode of a
"hide it" change isn't hiding too little — the route guards in
test_authz_routes.py cover that — it's hiding something from everyone.
"""

import library


def _recipe(url="https://example.com/tarte"):
    recipe_id = library.upsert_recipe(url, {
        "title": "Tarte aux figues",
        "ingredients": ["1 kg figues"],
        "instructions": [{"type": "step", "text": "Enfourner."}],
        "total_time": 45,
        "servings": "6",
        "lang": "fr",
    })
    library.save_recipe(recipe_id)
    library.set_tags(recipe_id, ["dessert"])
    return recipe_id


# --- masthead ---------------------------------------------------------------


def test_viewer_masthead_has_no_add_or_status(viewer_client, test_db):
    html = viewer_client.get("/app/").text
    assert 'href="/app/add"' not in html
    assert 'href="/app/status"' not in html


def test_admin_masthead_keeps_add_and_status(client, test_db):
    html = client.get("/app/").text
    assert 'href="/app/add"' in html
    assert 'href="/app/status"' in html


def test_viewer_sees_its_access_level(viewer_client, test_db):
    assert "role-chip" in viewer_client.get("/app/").text


def test_admin_sees_no_chip(client, test_db):
    """No chip for admin keeps the default masthead exactly as it was."""
    assert "role-chip" not in client.get("/app/").text


def test_everyone_can_sign_out(viewer_client, client, test_db):
    for c in (viewer_client, client):
        assert 'action="/app/logout"' in c.get("/app/").text


# --- recipe page ------------------------------------------------------------


def test_viewer_recipe_page_has_no_actions(viewer_client, test_db):
    recipe_id = _recipe()
    html = viewer_client.get(f"/app/recipes/{recipe_id}").text
    assert "Push to display" not in html
    assert "hx-delete" not in html
    assert f'href="/app/recipes/{recipe_id}/edit"' not in html


def test_admin_recipe_page_keeps_every_action(client, test_db):
    recipe_id = _recipe()
    html = client.get(f"/app/recipes/{recipe_id}").text
    assert "Push to display" in html
    assert "hx-delete" in html
    assert f'href="/app/recipes/{recipe_id}/edit"' in html


def test_viewer_tags_are_read_only_but_still_filter(viewer_client, test_db):
    """The half of a tag that doesn't write anything stays usable."""
    recipe_id = _recipe()
    html = viewer_client.get(f"/app/recipes/{recipe_id}").text
    assert f'hx-post="/app/recipes/{recipe_id}/tags"' not in html
    assert "tag-popover" not in html
    assert 'href="/app/?tag=dessert"' in html


def test_admin_tags_stay_editable(client, test_db):
    recipe_id = _recipe()
    html = client.get(f"/app/recipes/{recipe_id}").text
    assert f'hx-post="/app/recipes/{recipe_id}/tags"' in html
    assert "tag-popover" in html


def test_push_only_code_sees_push_but_not_edit(make_client, test_db):
    """Gating is per permission, not one read/write switch."""
    c = make_client(grants="library.view|display.push")
    recipe_id = _recipe()
    html = c.get(f"/app/recipes/{recipe_id}").text
    assert "Push to display" in html
    assert "hx-delete" not in html
    assert f'href="/app/recipes/{recipe_id}/edit"' not in html


# --- empty library ----------------------------------------------------------


def test_viewer_empty_state_does_not_point_at_the_add_page(viewer_client, test_db):
    """Don't dangle a dead end in front of a code that can't open it."""
    html = viewer_client.get("/app/").text
    assert "Nothing saved yet" in html
    assert "/app/add" not in html


def test_admin_empty_state_still_onboards(client, test_db):
    html = client.get("/app/").text
    assert "Nothing saved yet" in html
    assert "/app/add" in html


# --- status page ------------------------------------------------------------


def test_status_page_hides_clear_and_flash_without_those_permissions(
    make_client, test_db,
):
    c = make_client(grants="status.view")
    html = c.get("/app/status").text
    assert 'hx-post="/app/display/clear"' not in html
    assert 'href="/app/flash"' not in html


def test_admin_status_page_keeps_flash(client, test_db):
    assert 'href="/app/flash"' in client.get("/app/status").text


def test_chip_names_the_role_not_the_whole_matrix(make_client, test_db):
    """A hand-written permission list would otherwise print across the
    masthead; such a code shows its label instead."""
    assert ">viewer<" in make_client(grants="viewer").get("/app/").text
    assert ">guests<" in make_client(
        label="guests", grants="library.view|display.push",
    ).get("/app/").text


def test_403_page_offers_a_way_back_only_when_there_is_one(make_client, test_db):
    viewer = make_client(grants="viewer").get("/app/status", follow_redirects=False)
    assert viewer.status_code == 403
    assert "Back to the repertoire" in viewer.text

    # A code with no library access would otherwise be handed a link
    # straight to another 403 — from the 403 page and from the wordmark.
    no_library = make_client(grants="status.view").get(
        "/app/add", follow_redirects=False,
    )
    assert no_library.status_code == 403
    assert "Back to the repertoire" not in no_library.text
    assert 'href="/app/"' not in no_library.text


def test_wordmark_links_home_for_anyone_who_can_open_it(
    viewer_client, client, test_db,
):
    for c in (viewer_client, client):
        assert '<a href="/app/" class="brand">' in c.get("/app/").text
