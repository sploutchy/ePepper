"""Access levels — the permission vocabulary, roles, and access codes.

ePepper's web app used to have exactly one credential: `API_KEY`, which is
also the device Bearer token. Anyone who could sign in could add, edit,
delete, push to the panel and download firmware. This module adds a second
kind of credential — an *access code* granting a named subset of functions —
without touching the first: `API_KEY` remains an implicit `admin` principal
and the device's Bearer token, so an installation that sets no access codes
behaves exactly as it did before.

Deliberately dependency-free (no FastAPI, no `config`): `config` imports
*this*, so importing it back would cycle. The cookie helpers therefore take
the signing key as an argument instead of reaching for `API_KEY` — see
`api/web.py`, which binds it.

Permissions are a closed vocabulary: a permission per route would be
unmaintainable, and a single read/write flag couldn't express "may put a
recipe on the panel but may not edit it".
"""

import hashlib
import hmac
import re

# --- Permission vocabulary --------------------------------------------------
#
# Name → short human label, used by `--print-config` and the denial page.
# `area.verb`. Anything not listed here is a typo, and both the route
# decorators and the access-code parser treat it as one.

LIBRARY_VIEW = "library.view"
LIBRARY_ADD = "library.add"
LIBRARY_EDIT = "library.edit"
LIBRARY_DELETE = "library.delete"
DISPLAY_PUSH = "display.push"
DISPLAY_CONTROL = "display.control"
STATUS_VIEW = "status.view"
DEVICE_ADMIN = "device.admin"

PERMISSIONS: dict[str, str] = {
    LIBRARY_VIEW: "browse and search the repertoire",
    LIBRARY_ADD: "add recipes from a link or a photo",
    LIBRARY_EDIT: "edit recipe content and tags",
    LIBRARY_DELETE: "delete recipes",
    DISPLAY_PUSH: "push a recipe to the panel",
    DISPLAY_CONTROL: "clear the panel",
    STATUS_VIEW: "see the panel preview and device status",
    DEVICE_ADMIN: "flash firmware over USB",
}

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS)

# --- Roles ------------------------------------------------------------------
#
# Shorthands for the sets people actually want. A code can always spell out
# an explicit permission list instead; these just keep the common cases one
# word long.
#
# `admin` is NOT here on purpose: full access is API_KEY, and there is
# exactly one way to be admin. `parse_access_codes` rejects it as a grant.

ADMIN_LABEL = "admin"

ROLES: dict[str, frozenset[str]] = {
    # The guest code: the repertoire, nothing else. No status page — battery
    # level and Wi-Fi signal aren't a guest's business.
    "viewer": frozenset({LIBRARY_VIEW}),
    # Everything except firmware flashing (see DEVICE_ADMIN's note below).
    "editor": ALL_PERMISSIONS - {DEVICE_ADMIN},
}

# `/app/flash/epepper-merged.bin` serves a firmware image with the Wi-Fi
# password and the raw API_KEY baked in (a deliberate trade — ESP Web Tools
# can only fetch with browser credentials; see api/server.py's docstring).
# Granting DEVICE_ADMIN is therefore equivalent to granting admin: the holder
# can read API_KEY out of the binary. It stays out of every non-admin role,
# and the README says so rather than implying a tighter boundary than exists.

# Access codes shorter than this are rejected. A weak second code is the
# entire attack surface of this feature.
MIN_CODE_LENGTH = 12

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class Principal:
    """A credential and what it may do.

    Frozen by convention (nothing mutates one after construction) and
    hashable by label, so tests and `--print-config` can put them in sets.
    """

    __slots__ = ("label", "code", "permissions", "role")

    def __init__(
        self,
        label: str,
        code: str,
        permissions: frozenset[str],
        role: str | None = None,
    ):
        self.label = label
        self.code = code
        self.permissions = frozenset(permissions)
        # Role name when granted via shorthand, else None (explicit list).
        # Only used for display — `permissions` is the authority.
        self.role = role

    def can(self, permission: str) -> bool:
        """True when this principal holds `permission`.

        Raises on an unknown permission name rather than returning False: a
        typo in a route decorator or a template must fail loudly, not
        silently deny (or, worse, silently allow once someone "fixes" it by
        inverting the check).
        """
        if permission not in PERMISSIONS:
            raise ValueError(f"unknown permission: {permission!r}")
        return permission in self.permissions

    @property
    def is_admin(self) -> bool:
        return self.permissions >= ALL_PERMISSIONS

    @property
    def grants(self) -> str:
        """Human-readable grant summary — a role name, or the permission list."""
        if self.is_admin:
            return ADMIN_LABEL
        if self.role:
            return self.role
        return "|".join(sorted(self.permissions))

    def __repr__(self) -> str:  # never include `code`
        return f"Principal(label={self.label!r}, grants={self.grants!r})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Principal):
            return NotImplemented
        return (
            self.label == other.label
            and self.code == other.code
            and self.permissions == other.permissions
        )

    def __hash__(self) -> int:
        return hash((self.label, self.permissions))


def admin_principal(api_key: str) -> Principal:
    """The implicit principal behind `API_KEY` — every permission there is."""
    return Principal(ADMIN_LABEL, api_key, ALL_PERMISSIONS, role=ADMIN_LABEL)


def _parse_grants(raw: str, entry: str) -> tuple[frozenset[str], str | None]:
    """Resolve an entry's grant field to (permissions, role-name-or-None)."""
    grants = raw.strip()
    if not grants:
        raise ValueError(f"{entry!r}: no permissions granted")
    if grants == ADMIN_LABEL:
        raise ValueError(
            f"{entry!r}: {ADMIN_LABEL!r} is not grantable — full access is API_KEY"
        )
    if grants in ROLES:
        return ROLES[grants], grants
    perms = {p.strip() for p in grants.split("|") if p.strip()}
    unknown = sorted(perms - ALL_PERMISSIONS)
    if unknown:
        known = ", ".join(sorted(ALL_PERMISSIONS))
        roles = ", ".join(sorted(ROLES))
        raise ValueError(
            f"{entry!r}: unknown permission(s) {', '.join(unknown)} "
            f"— expected a role ({roles}) or permissions from: {known}"
        )
    if not perms:
        raise ValueError(f"{entry!r}: no permissions granted")
    return frozenset(perms), None


def parse_access_codes(
    raw: str, reserved_codes: tuple[str, ...] = (),
) -> list[Principal]:
    """Parse the `ACCESS_CODES` env var into principals.

    Format — entries separated by `;`, each `label:code:grants`, where
    `grants` is a role name or a `|`-separated permission list::

        guests:zoZ7k2h9Qm4Vt1sY:viewer
        guests:…:viewer;kitchen:…:library.view|display.push

    Every problem raises `ValueError` naming the offending entry. A typo that
    quietly grants nothing is worse than a container that refuses to start —
    the same stance `config.py` already takes for `API_KEY` and
    `PHOTO_MAX_MB`.

    `reserved_codes` are codes already spoken for (in practice: `API_KEY`).
    """
    principals: list[Principal] = []
    seen_labels: set[str] = set()
    seen_codes: set[str] = set(reserved_codes)

    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue

        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"{entry!r}: expected 'label:code:grants' "
                f"(got {len(parts)} colon-separated field(s))"
            )
        label, code, grants_raw = (p.strip() for p in parts)

        if not _LABEL_RE.match(label):
            raise ValueError(
                f"{entry!r}: label must be 1-32 chars of a-z, 0-9, '_' or '-' "
                "and start with a letter or digit"
            )
        if label == ADMIN_LABEL:
            raise ValueError(f"{entry!r}: {ADMIN_LABEL!r} is a reserved label")
        if label in seen_labels:
            raise ValueError(f"{entry!r}: duplicate label {label!r}")

        if len(code) < MIN_CODE_LENGTH:
            raise ValueError(
                f"{entry!r}: code for {label!r} must be at least "
                f"{MIN_CODE_LENGTH} characters"
            )
        if code in seen_codes:
            raise ValueError(
                f"{entry!r}: code for {label!r} is already in use "
                "(by API_KEY or another entry)"
            )

        permissions, role = _parse_grants(grants_raw, entry)

        seen_labels.add(label)
        seen_codes.add(code)
        principals.append(Principal(label, code, permissions, role))

    return principals


# --- Session cookie ---------------------------------------------------------
#
# Stateless, as before: no session row, validated by recomputation. What's
# new is that the value names its principal, so the server knows *which* code
# signed in.
#
#     v2.<label>.<hmac>
#
# The HMAC is keyed by API_KEY over the label plus a digest of that
# principal's own code. Two properties fall out of that choice:
#
#   * Keying with API_KEY means a guest who knows only their own code still
#     cannot mint an admin cookie — they'd need API_KEY to sign it.
#   * Folding in the code's digest means changing one entry's code logs out
#     only that principal; rotating API_KEY still logs out everyone.
#
# The cookie never carries the code itself, so a cookie leak can't be
# replayed as a login (or, for admin, as the device Bearer token).

_SESSION_LABEL = b"epepper-web-session"
_V2_PREFIX = "v2."


def legacy_session_value(key: str) -> str:
    """The pre-access-levels cookie value: a bare HMAC, implicitly admin.

    Still accepted so deploying this change doesn't sign everyone out.
    TODO: drop this and `_legacy_matches` once outstanding cookies have
    aged out (30-day max age) — no earlier than 2026-04.
    """
    return hmac.new(key.encode("utf-8"), _SESSION_LABEL, hashlib.sha256).hexdigest()


def session_value(key: str, principal: Principal) -> str:
    """Mint the auth-cookie value for `principal`."""
    code_digest = hashlib.sha256(principal.code.encode("utf-8")).hexdigest()
    mac = hmac.new(
        key.encode("utf-8"),
        b"epepper-web-session:v2:"
        + principal.label.encode("utf-8")
        + b":"
        + code_digest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_V2_PREFIX}{principal.label}.{mac}"


def resolve_cookie(
    key: str, principals: list[Principal], cookie: str,
) -> Principal | None:
    """Return the principal a cookie value authenticates, or None.

    `principals` must include the admin principal — it's just another entry
    here, which is what keeps the legacy path and the v2 path from needing
    separate handling downstream.
    """
    if not cookie:
        return None

    if not cookie.startswith(_V2_PREFIX):
        # Legacy bare-hex cookie → admin.
        if hmac.compare_digest(cookie, legacy_session_value(key)):
            for principal in principals:
                if principal.is_admin:
                    return principal
        return None

    try:
        _, label, _mac = cookie.split(".", 2)
    except ValueError:
        return None

    for principal in principals:
        if principal.label != label:
            continue
        # compare_digest on the whole value, so the label has to match too.
        if hmac.compare_digest(cookie, session_value(key, principal)):
            return principal
        return None
    return None


def authenticate(principals: list[Principal], code: str) -> Principal | None:
    """Match a submitted access code against the configured principals.

    Walks the whole list without an early exit so the work done is the same
    whichever code was submitted (and whether or not one matched).
    """
    submitted = code.encode("utf-8")
    matched: Principal | None = None
    for principal in principals:
        if hmac.compare_digest(submitted, principal.code.encode("utf-8")):
            matched = principal
    return matched
