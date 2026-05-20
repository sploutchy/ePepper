"""SSRF guard — assert that a URL resolves to a public IP before fetching.

Defense-in-depth: even an authenticated caller shouldn't be able to make the
server probe the LAN, loopback, or cloud-metadata endpoints (e.g.
169.254.169.254). Used by the recipe + Fooby fetchers.

Limitations:
- TOCTOU window: a malicious DNS server could return a public IP at check
  time and a private IP at connect time (DNS rebinding). For the threat
  model here (single-user personal app) the simple check is acceptable.
- Redirects aren't re-checked; the aiohttp client follows them with the
  default resolver. Callers that worry about redirect SSRF should also
  cap `max_redirects` or disable auto-redirect.
"""

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class UnsafeUrl(ValueError):
    """The URL is missing a host or resolves to a non-public address."""


async def assert_url_safe(url: str) -> None:
    """Raise UnsafeUrl if `url` lacks a host, fails DNS, or resolves to a
    non-public IP (private, loopback, link-local, multicast, reserved,
    or unspecified)."""
    host = urlparse(url).hostname
    if not host:
        raise UnsafeUrl(f"URL has no host: {url!r}")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as e:
        raise UnsafeUrl(f"DNS lookup failed for {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        # IPv6 scope suffix ("fe80::1%eth0") confuses ip_address; strip it.
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise UnsafeUrl(
                f"URL {url!r} resolves to non-public address: {host} -> {addr}"
            )
