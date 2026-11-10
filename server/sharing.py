"""Share links — signed, expiring, read-only URLs for a single recipe.

A share token carries its own claims (which recipe, until when) plus an
HMAC over them, keyed by API_KEY. The server stores nothing: it verifies by
recomputing the MAC, exactly like the /app/ session cookie does. That's what
makes a link mintable from a button — no DB write, no config edit, no
restart.

    v1.<recipe_id>.<expires_unix>.<mac>

The claims are readable by whoever holds the link; that's fine, they name a
recipe the holder is being shown anyway. Nothing secret goes in them.

Two consequences worth stating plainly:

* **A link can't be revoked individually.** With no stored record there's
  nothing to delete. The 24-hour lifetime *is* the revocation; rotating
  API_KEY invalidates every outstanding link at once (along with every
  session, so it's a blunt instrument).
* **The MAC is domain-separated** from the session cookie's (a different
  label is mixed in), so neither kind of value can ever be replayed as the
  other even though both are keyed by API_KEY.
"""

import base64
import hashlib
import hmac
import time

# Fixed lifetime. Long enough to send someone a recipe and have them open it
# after work; short enough that an unrevocable link isn't a lasting liability.
SHARE_TTL_SECONDS = 24 * 3600

_VERSION = "v1"
# Mixed into the MAC so a share token and a session cookie can never be
# confused for one another. Bump alongside _VERSION if the claims change.
_MAC_LABEL = b"epepper-share-v1:"
# 128 bits, base64url'd to 22 chars — plenty against forgery, and it keeps
# the link short enough to paste into a message.
_MAC_BYTES = 16


class ShareTokenError(Exception):
    """Base for the two ways a token fails to open anything."""


class ShareTokenInvalid(ShareTokenError):
    """Malformed, or the MAC doesn't verify (tampered, or a stale API_KEY)."""


class ShareTokenExpired(ShareTokenError):
    """Authentic, but past its expiry — worth saying so instead of 404ing.

    Safe to distinguish: the MAC has already verified by the time we look at
    the clock, so "expired" only ever gets told to someone holding a link we
    really did issue.
    """


def _mac(key: str, claims: str) -> str:
    digest = hmac.new(
        key.encode("utf-8"), _MAC_LABEL + claims.encode("utf-8"), hashlib.sha256,
    ).digest()[:_MAC_BYTES]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint(key: str, recipe_id: int, now: int | None = None) -> str:
    """A share token for `recipe_id`, valid for SHARE_TTL_SECONDS."""
    now = int(time.time()) if now is None else now
    claims = f"{_VERSION}.{int(recipe_id)}.{now + SHARE_TTL_SECONDS}"
    return f"{claims}.{_mac(key, claims)}"


def expires_at(key: str, token: str) -> int:
    """The expiry stamp inside a token. Assumes it already verified."""
    return int(token.split(".")[2])


def read(key: str, token: str, now: int | None = None) -> int:
    """The recipe id a token opens.

    Raises ShareTokenInvalid or ShareTokenExpired — never returns a falsy
    sentinel, so a caller can't accidentally treat a failure as recipe 0.
    """
    if not token:
        raise ShareTokenInvalid("empty")
    parts = token.split(".")
    if len(parts) != 4:
        raise ShareTokenInvalid("shape")
    version, raw_id, raw_exp, mac = parts
    if version != _VERSION:
        raise ShareTokenInvalid("version")

    # Verify over the claims exactly as they arrived, not over a re-rendering
    # of the parsed values: that way "v1.007.…" is rejected rather than
    # quietly accepted as an alternative spelling of recipe 7. Constant-time.
    claims = token.rsplit(".", 1)[0]
    if not hmac.compare_digest(mac, _mac(key, claims)):
        raise ShareTokenInvalid("mac")

    try:
        recipe_id = int(raw_id)
        expires = int(raw_exp)
    except ValueError:
        # Unreachable for a token we minted — the MAC has already verified —
        # but parsing can't be assumed to succeed just because a signature did.
        raise ShareTokenInvalid("claims") from None

    if (int(time.time()) if now is None else now) >= expires:
        raise ShareTokenExpired("expired")
    return recipe_id
