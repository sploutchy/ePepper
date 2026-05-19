"""Fetch Fooby's FR homepage and extract the weekly-inspiration recipes.

Used by the midnight scheduler as a fallback when no anniversary recipe
matches today's calendar day. The scheduler rotates through the URLs by
ISO weekday so each entry appears on the same weekday every week.

The block lives near the top of https://fooby.ch/fr.html, behind a short
heading such as "Inspiration pour cette semaine" or "Inspirations de la
semaine". Fooby styles those headings as `<p class="heading--…">` rather
than h1/h2, and the surrounding markup is a CSS grid (no semantic
<section>/<article>), so we locate the heading by text, walk up to the
nearest ancestor that actually contains recipe-detail anchors, and pull
those in document order. When the heading can't be found we fall back
to every recipe-detail link on the page so the feature still produces
something rather than going dark.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Fooby's FR landing page — French is hard-coded per the feature spec.
FOOBY_FR_HOMEPAGE = "https://fooby.ch/fr.html"

# Recipe-detail URLs have a numeric ID after /recettes/. Category and
# theme indexes (e.g. /recettes/recettes-aux-asperges.html) carry no ID
# and don't expose Recipe JSON-LD, so recipe-scrapers can't parse them.
# The leading subdomain group admits `little.fooby.ch` alongside the
# bare `fooby.ch` host, since the weekly block sometimes mixes both.
_RECIPE_URL_RE = re.compile(
    r"^https?://(?:[\w-]+\.)?fooby\.ch/fr/recettes/\d+/",
    re.IGNORECASE,
)

# Heading text that marks the weekly-inspiration block. Tolerant to
# singular/plural and to either phrasing Fooby has used over time.
_INSPIRATION_RE = re.compile(
    r"inspirations?\s+(?:de\s+la|pour\s+cette)\s+semaine",
    re.IGNORECASE,
)

# Fooby styles the section heading as a short <p class="heading--…">, so
# the search has to accept paragraph elements alongside real headings.
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "p"}


async def fetch_weekly_inspiration_urls(homepage_url: str = FOOBY_FR_HOMEPAGE) -> list[str]:
    """Return absolute recipe URLs from the weekly-inspiration block, in document order.

    Raises on network failure. Returns [] when neither the labelled
    section nor the fallback scan finds any recipe links.
    """
    html = await _fetch_html(homepage_url)
    soup = BeautifulSoup(html, "html.parser")

    urls = _extract_from_section(soup, homepage_url)
    if urls:
        log.info("Fooby: found %d weekly-inspiration recipe URLs", len(urls))
        return urls

    fallback = _extract_recipe_links(soup, homepage_url)
    log.info(
        "Fooby: weekly-inspiration heading not located; falling back to %d page-wide recipe links",
        len(fallback),
    )
    return fallback


async def _fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ePepper/1.0; recipe display)",
        "Accept-Language": "fr-CH,fr;q=0.9",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.text()


def _extract_from_section(soup: BeautifulSoup, base_url: str) -> list[str]:
    heading = soup.find(
        lambda tag: tag.name in _HEADING_TAGS
        and tag.get_text(strip=True)
        and _INSPIRATION_RE.search(tag.get_text(strip=True))
    )
    if heading is None:
        return []
    container = _enclosing_recipe_block(heading, base_url)
    if container is None:
        return []
    return _extract_recipe_links(container, base_url)


def _enclosing_recipe_block(node, base_url: str):
    """Climb to the smallest ancestor of `node` that contains recipe links.

    The heading sits in its own narrow column wrapper; the grid row two
    or three ancestors up carries both the heading and the recipe cards.
    Bounded to eight hops as a safety net against runaway climbs into
    page-wide containers.
    """
    parent = node.parent
    for _ in range(8):
        if parent is None:
            return None
        if _extract_recipe_links(parent, base_url):
            return parent
        parent = parent.parent
    return None


def _extract_recipe_links(container, base_url: str) -> list[str]:
    """Recipe URLs inside `container`, deduped, with query + fragment stripped."""
    out: list[str] = []
    seen: set[str] = set()
    for a in container.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"].strip())
        parts = urlparse(absolute)
        clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if not _RECIPE_URL_RE.match(clean):
            continue
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out
