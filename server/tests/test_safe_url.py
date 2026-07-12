"""SSRF guard tests — mock DNS resolution (`loop.getaddrinfo`) so no real
network access is needed. Each `asyncio.run` gets a fresh event loop, so
patching the loop's `getaddrinfo` attribute directly (rather than via
`monkeypatch`) is safe: the loop is discarded when the run completes.
"""

import asyncio

import pytest

from processing.safe_url import UnsafeUrl, assert_url_safe


def _info(family: int, addr: str) -> tuple:
    return (family, 1, 6, "", (addr, 0))


def _resolves_to(*addrs: str):
    async def fake_getaddrinfo(host, port):
        return [_info(10 if ":" in a else 2, a) for a in addrs]
    return fake_getaddrinfo


def _run(url: str, fake_getaddrinfo) -> None:
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.getaddrinfo = fake_getaddrinfo
        await assert_url_safe(url)
    asyncio.run(scenario())


def test_missing_host_rejected():
    with pytest.raises(UnsafeUrl):
        asyncio.run(assert_url_safe("not-a-url"))


def test_dns_failure_rejected():
    async def fake_getaddrinfo(host, port):
        raise OSError("no such host")
    with pytest.raises(UnsafeUrl):
        _run("http://nowhere.invalid", fake_getaddrinfo)


def test_public_ipv4_is_safe():
    _run("http://example.com", _resolves_to("93.184.216.34"))


def test_public_ipv6_is_safe():
    _run("http://example.com", _resolves_to("2606:2800:220:1:248:1893:25c8:1946"))


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",           # loopback
        "10.0.0.5",            # private
        "192.168.1.1",         # private
        "169.254.169.254",     # link-local / cloud metadata
        "224.0.0.1",           # multicast
        "0.0.0.0",             # unspecified
        "100.64.0.1",          # RFC 6598 CGNAT — caught by `not is_global`
        "::1",                 # IPv6 loopback
        "fc00::1",             # IPv6 unique local (private)
    ],
)
def test_non_public_addresses_rejected(addr):
    with pytest.raises(UnsafeUrl):
        _run("http://evil.example", _resolves_to(addr))


def test_ipv6_scope_suffix_is_stripped_before_check():
    with pytest.raises(UnsafeUrl):
        _run("http://link-local.example", _resolves_to("fe80::1%eth0"))


def test_any_unsafe_address_in_the_dns_answer_rejects():
    # A domain resolving to both a public and a private address (e.g. a
    # misconfigured DNS or an attacker-controlled record) must still be
    # rejected — any() semantics, not all().
    with pytest.raises(UnsafeUrl):
        _run("http://mixed.example", _resolves_to("93.184.216.34", "127.0.0.1"))


def test_unparseable_address_entry_is_skipped_not_fatal():
    # A malformed getaddrinfo entry shouldn't crash the guard — it's
    # skipped, and a later, valid public address still passes.
    async def fake_getaddrinfo(host, port):
        return [_info(2, "not-an-ip"), _info(2, "93.184.216.34")]
    _run("http://example.com", fake_getaddrinfo)
