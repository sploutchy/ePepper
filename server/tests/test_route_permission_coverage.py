"""Every /app route must declare a permission — enforced structurally.

The realistic way access levels go wrong isn't the sixteen routes somebody
just thought about: it's the seventeenth, added six months from now by
someone who didn't know this existed. Because the permission is declared in
the decorator rather than checked in the handler body, it's introspectable,
and a route that forgot to decide fails here instead of shipping open.
"""

import re
from pathlib import Path

import pytest

import authz
from api import web

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"

# Routes reachable while signed out. Adding to this set should take an
# argument, which is the point of listing it here.
PUBLIC_PATHS = {"/app/login", "/app/logout"}


def _declared_permissions(route) -> list[str]:
    """The permissions a route declares via `Depends(requires(...))`."""
    return [
        perm
        for dep in route.dependant.dependencies
        if (perm := getattr(dep.call, "epepper_permission", None)) is not None
    ]


def _app_routes():
    return list(web.router.routes)


def test_there_are_routes_to_check():
    """Guard against the introspection silently finding nothing."""
    assert len(_app_routes()) > 10


@pytest.mark.parametrize(
    "route", _app_routes(), ids=lambda r: f"{sorted(r.methods)[0]} {r.path}",
)
def test_every_route_declares_exactly_one_permission(route):
    declared = _declared_permissions(route)
    assert len(declared) == 1, (
        f"{sorted(route.methods)} {route.path} declares {declared or 'no'} "
        "permission(s) — add dependencies=[Depends(requires(authz.<PERM>))] "
        "to the route decorator (or Depends(public()) if it's reachable "
        "signed out)."
    )


@pytest.mark.parametrize(
    "route", _app_routes(), ids=lambda r: f"{sorted(r.methods)[0]} {r.path}",
)
def test_declared_permissions_are_real(route):
    [declared] = _declared_permissions(route)
    assert declared == web.PUBLIC or declared in authz.PERMISSIONS


def test_only_the_expected_routes_are_public():
    public = {
        route.path
        for route in _app_routes()
        if _declared_permissions(route) == [web.PUBLIC]
    }
    assert public == PUBLIC_PATHS


def test_templates_only_reference_real_permissions():
    """A typo in `can(request, "...")` would hide a control from everyone,
    silently and forever. `Principal.can` raises on an unknown name, so this
    would surface at runtime — but only if someone loads that page."""
    used = set()
    for template in _TEMPLATE_DIR.glob("*.html"):
        used |= set(
            re.findall(r'can\(request,\s*"([^"]+)"\)', template.read_text())
        )
    assert used, "no can() calls found — has the gating regressed?"
    assert used <= set(authz.PERMISSIONS), sorted(used - set(authz.PERMISSIONS))


def test_device_endpoints_that_accept_a_cookie_are_enumerated():
    """Bearer stays admin-only; the cookie is honoured on exactly one device
    endpoint (the status page's <img src="/image">). If that grows, the
    module docstring in api/server.py needs to grow with it."""
    source = (Path(__file__).resolve().parent.parent / "api" / "server.py").read_text()
    cookie_routes = re.findall(r"_check_api_key\(request, cookie_permission=([^)]+)\)", source)
    assert cookie_routes == ["authz.STATUS_VIEW"]
