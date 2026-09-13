"""Tests for the session cookie: minting, validation, and what invalidates it.

The cookie is `v2.<label>.<hmac>`, signed with API_KEY over the label plus a
digest of that principal's own code. These tests pin the two properties that
choice buys — a guest can't forge admin, and rotating one code logs out only
its holder.
"""

import authz

KEY = "test-signing-key-0123456789"
OTHER_KEY = "a-different-api-key-9876543"


def _principals(access_codes: str = "guests:zoZ7k2h9Qm4Vt1sY:viewer"):
    return [authz.admin_principal(KEY), *authz.parse_access_codes(access_codes)]


# --- round trip -------------------------------------------------------------


def test_admin_cookie_round_trip():
    principals = _principals()
    cookie = authz.session_value(KEY, principals[0])
    assert authz.resolve_cookie(KEY, principals, cookie) is principals[0]


def test_access_code_cookie_round_trip():
    principals = _principals()
    guests = principals[1]
    cookie = authz.session_value(KEY, guests)
    resolved = authz.resolve_cookie(KEY, principals, cookie)
    assert resolved is guests
    assert resolved.can(authz.LIBRARY_VIEW)
    assert not resolved.can(authz.LIBRARY_DELETE)


def test_cookie_never_contains_the_code():
    principals = _principals()
    cookie = authz.session_value(KEY, principals[1])
    assert "zoZ7k2h9Qm4Vt1sY" not in cookie
    assert KEY not in cookie


def test_empty_and_junk_cookies_resolve_to_nobody():
    principals = _principals()
    for junk in ("", "nonsense", "v2.", "v2.guests", "v2.guests.deadbeef"):
        assert authz.resolve_cookie(KEY, principals, junk) is None


# --- what invalidates a cookie ---------------------------------------------


def test_rotating_the_changed_code_logs_out_only_that_principal():
    principals = _principals()
    admin_cookie = authz.session_value(KEY, principals[0])
    guest_cookie = authz.session_value(KEY, principals[1])

    rotated = _principals("guests:aBrandNewGuestCode:viewer")

    assert authz.resolve_cookie(KEY, rotated, guest_cookie) is None
    assert authz.resolve_cookie(KEY, rotated, admin_cookie) is not None


def test_unrelated_entry_changing_keeps_you_signed_in():
    principals = _principals(
        "guests:zoZ7k2h9Qm4Vt1sY:viewer;kitchen:Ba4dRn8xLe2WqPju:editor"
    )
    guest_cookie = authz.session_value(KEY, principals[1])

    changed = _principals(
        "guests:zoZ7k2h9Qm4Vt1sY:viewer;kitchen:SomethingElse12345:editor"
    )
    assert authz.resolve_cookie(KEY, changed, guest_cookie) is not None


def test_rotating_api_key_logs_out_everyone():
    principals = _principals()
    cookies = [authz.session_value(KEY, p) for p in principals]
    rotated = [authz.admin_principal(OTHER_KEY), *principals[1:]]
    for cookie in cookies:
        assert authz.resolve_cookie(OTHER_KEY, rotated, cookie) is None


def test_removing_an_entry_logs_its_holder_out():
    principals = _principals()
    guest_cookie = authz.session_value(KEY, principals[1])
    assert authz.resolve_cookie(KEY, _principals(""), guest_cookie) is None


def test_regranting_a_label_does_not_widen_an_old_cookie():
    """A cookie is only as good as the current grant behind its label."""
    principals = _principals()
    guest_cookie = authz.session_value(KEY, principals[1])
    promoted = _principals("guests:zoZ7k2h9Qm4Vt1sY:editor")
    resolved = authz.resolve_cookie(KEY, promoted, guest_cookie)
    # Same label + same code, so the cookie still validates — but the
    # permissions come from the config, never from the cookie.
    assert resolved is not None
    assert resolved.can(authz.LIBRARY_DELETE)


# --- forgery ----------------------------------------------------------------


def test_guest_cannot_forge_an_admin_cookie_with_their_own_code():
    """The signing key is API_KEY, not the principal's code. A guest knows
    their own code and their own label — that must not be enough."""
    principals = _principals()
    guest = principals[1]
    # Everything a guest holds, used as the signing key.
    forged = authz.session_value(guest.code, authz.admin_principal(guest.code))
    forged = f"v2.admin.{forged.split('.', 2)[2]}"
    assert authz.resolve_cookie(KEY, principals, forged) is None


def test_relabelling_a_valid_cookie_is_rejected():
    """Swapping the label in an otherwise-valid cookie must not escalate."""
    principals = _principals()
    guest_cookie = authz.session_value(KEY, principals[1])
    mac = guest_cookie.split(".", 2)[2]
    assert authz.resolve_cookie(KEY, principals, f"v2.admin.{mac}") is None


# --- legacy cookies ---------------------------------------------------------


def test_legacy_cookie_still_signs_you_in_as_admin():
    """Deploying access levels must not sign existing sessions out."""
    principals = _principals()
    legacy = authz.legacy_session_value(KEY)
    resolved = authz.resolve_cookie(KEY, principals, legacy)
    assert resolved is not None and resolved.is_admin


def test_legacy_cookie_dies_with_api_key_rotation():
    principals = _principals()
    legacy = authz.legacy_session_value(KEY)
    rotated = [authz.admin_principal(OTHER_KEY), *principals[1:]]
    assert authz.resolve_cookie(OTHER_KEY, rotated, legacy) is None


# --- login ------------------------------------------------------------------


def test_authenticate_matches_the_right_principal():
    principals = _principals()
    assert authz.authenticate(principals, KEY) is principals[0]
    assert authz.authenticate(principals, "zoZ7k2h9Qm4Vt1sY") is principals[1]


def test_authenticate_rejects_unknown_codes():
    principals = _principals()
    for wrong in ("", "nope", "zoZ7k2h9Qm4Vt1s", "ZOZ7K2H9QM4VT1SY"):
        assert authz.authenticate(principals, wrong) is None


def test_authenticate_handles_non_ascii():
    """compare_digest raises TypeError on non-ASCII str; a junk submission
    must be a failed login, not a 500."""
    assert authz.authenticate(_principals(), "pépère") is None
