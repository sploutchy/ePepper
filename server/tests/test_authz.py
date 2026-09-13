"""Tests for the permission vocabulary and ACCESS_CODES parsing.

Pure unit tests — `authz` deliberately has no FastAPI or config imports, so
nothing here needs a client or a database.
"""

import pytest

import authz


# --- roles ------------------------------------------------------------------


def test_viewer_is_library_only():
    """The requested read-only code: browsing, and nothing else."""
    assert authz.ROLES["viewer"] == {authz.LIBRARY_VIEW}


def test_editor_is_everything_but_firmware():
    assert authz.ROLES["editor"] == authz.ALL_PERMISSIONS - {authz.DEVICE_ADMIN}


def test_device_admin_is_in_no_role_but_admin():
    """Granting device.admin is equivalent to granting admin — the merged
    firmware image carries the raw API_KEY. It must not leak into a role."""
    for name, perms in authz.ROLES.items():
        assert authz.DEVICE_ADMIN not in perms, name


def test_admin_principal_holds_everything():
    admin = authz.admin_principal("some-api-key")
    assert admin.is_admin
    assert all(admin.can(p) for p in authz.PERMISSIONS)
    assert admin.grants == "admin"


# --- Principal --------------------------------------------------------------


def test_viewer_can_only_view():
    viewer = authz.parse_access_codes("guests:zoZ7k2h9Qm4Vt1sY:viewer")[0]
    assert viewer.can(authz.LIBRARY_VIEW)
    assert not viewer.can(authz.LIBRARY_DELETE)
    assert not viewer.can(authz.STATUS_VIEW)
    assert not viewer.is_admin


def test_can_raises_on_unknown_permission():
    """A typo in a route decorator or template must fail loudly rather than
    silently denying — a silent False invites someone to 'fix' it."""
    viewer = authz.parse_access_codes("guests:zoZ7k2h9Qm4Vt1sY:viewer")[0]
    with pytest.raises(ValueError, match="unknown permission"):
        viewer.can("library.veiw")


def test_repr_never_leaks_the_code():
    p = authz.parse_access_codes("guests:zoZ7k2h9Qm4Vt1sY:viewer")[0]
    assert "zoZ7k2h9Qm4Vt1sY" not in repr(p)
    assert "guests" in repr(p)


# --- parse_access_codes: accepted shapes ------------------------------------


def test_parse_empty_yields_nothing():
    """Unset ACCESS_CODES == the behaviour before access levels existed."""
    assert authz.parse_access_codes("") == []
    assert authz.parse_access_codes("   ") == []


def test_parse_role_shorthand():
    [p] = authz.parse_access_codes("guests:zoZ7k2h9Qm4Vt1sY:viewer")
    assert p.label == "guests"
    assert p.code == "zoZ7k2h9Qm4Vt1sY"
    assert p.permissions == authz.ROLES["viewer"]
    assert p.grants == "viewer"


def test_parse_explicit_permission_list():
    [p] = authz.parse_access_codes(
        "kitchen:Ba4dRn8xLe2WqPju:library.view|display.push"
    )
    assert p.permissions == {authz.LIBRARY_VIEW, authz.DISPLAY_PUSH}
    assert p.role is None
    assert p.grants == "display.push|library.view"


def test_parse_multiple_entries_and_whitespace():
    principals = authz.parse_access_codes(
        " guests:zoZ7k2h9Qm4Vt1sY:viewer ; kitchen:Ba4dRn8xLe2WqPju:editor ;"
    )
    assert [p.label for p in principals] == ["guests", "kitchen"]
    assert principals[1].permissions == authz.ROLES["editor"]


# --- parse_access_codes: rejected shapes ------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("guests:zoZ7k2h9Qm4Vt1sY", "expected 'label:code:grants'"),
        ("guests:zoZ7k2h9Qm4Vt1sY:viewer:extra", "expected 'label:code:grants'"),
        ("Guests:zoZ7k2h9Qm4Vt1sY:viewer", "label must be"),
        ("-guests:zoZ7k2h9Qm4Vt1sY:viewer", "label must be"),
        (":zoZ7k2h9Qm4Vt1sY:viewer", "label must be"),
        ("admin:zoZ7k2h9Qm4Vt1sY:viewer", "reserved label"),
        ("guests:short:viewer", "at least 12 characters"),
        ("guests:zoZ7k2h9Qm4Vt1sY:librarian", "unknown permission"),
        ("guests:zoZ7k2h9Qm4Vt1sY:library.veiw", "unknown permission"),
        ("guests:zoZ7k2h9Qm4Vt1sY:", "no permissions granted"),
        ("guests:zoZ7k2h9Qm4Vt1sY:admin", "not grantable"),
    ],
)
def test_parse_rejects(raw, expected):
    with pytest.raises(ValueError, match=expected):
        authz.parse_access_codes(raw)


def test_parse_rejects_duplicate_label():
    with pytest.raises(ValueError, match="duplicate label"):
        authz.parse_access_codes(
            "guests:zoZ7k2h9Qm4Vt1sY:viewer;guests:Ba4dRn8xLe2WqPju:editor"
        )


def test_parse_rejects_duplicate_code():
    with pytest.raises(ValueError, match="already in use"):
        authz.parse_access_codes(
            "guests:zoZ7k2h9Qm4Vt1sY:viewer;kitchen:zoZ7k2h9Qm4Vt1sY:editor"
        )


def test_parse_rejects_code_colliding_with_api_key():
    """Reusing API_KEY as an access code would silently downgrade admin."""
    with pytest.raises(ValueError, match="already in use"):
        authz.parse_access_codes(
            "guests:the-real-api-key:viewer", reserved_codes=("the-real-api-key",),
        )


def test_parse_error_names_the_offending_entry():
    """An operator with six codes needs to know which one is wrong."""
    with pytest.raises(ValueError, match="kitchen"):
        authz.parse_access_codes(
            "guests:zoZ7k2h9Qm4Vt1sY:viewer;kitchen:tooshort:editor"
        )
