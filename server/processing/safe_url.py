"""SSRF guard — assert that a URL resolves to a public IP before fetching.

Defense-in-depth: even an authenticated caller shouldn't be able to make the
server probe the LAN, loopback, or cloud-metadata endpoints (e.g.
169.254.169.254). Used by the recipe + Fooby fetchers.

Limitations:
- TOCTOU window: a malicious DNS server could return a public IP at check
  time and a private IP at connect time (DNS rebinding). For the threat
  model here (single-household personal app) the simple check is acceptable.
- Only the initial URL is checked; the HTTP client follows redirects on its
  own, so a public host that 302s to a LAN address would be fetched
  unchecked. Accepted residual risk — the URLs come from two trusted users
  and there's no cloud-metadata endpoint to protect on a home host. Per-hop
  re-validation was removed as disproportionate for this app.
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
    unspecified, or otherwise non-global — e.g. RFC 6598 CGNAT space)."""
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
            # `not is_global` catches RFC 6598 shared/CGNAT space
            # (100.64.0.0/10) and any future special-use range that
            # `ipaddress` reports as neither private nor reserved. The
            # explicit checks above stay for clearer error attribution.
            or not ip.is_global
        ):
            raise UnsafeUrl(
                f"URL {url!r} resolves to non-public address: {host} -> {addr}"
            )
