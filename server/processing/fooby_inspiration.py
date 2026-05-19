"""Fetch Fooby's FR homepage and extract the "Inspiration de la semaine" recipes.

Used by the midnight scheduler as a fallback when no anniversary recipe
matches today's calendar day. The scheduler rotates through the URLs by
ISO weekday so each entry appears on the same weekday every week.

The block lives near the top of https://fooby.ch/fr.html, with a heading
like "Inspirations de la semaine" or "Inspiration de la semaine". We
locate that heading by text, walk up to the enclosing section, and pull
every `/fr/recettes/...` link from inside it in document order. If the
heading can't be found (Fooby occasionally reshuffles its markup), we
fall back to *all* recipe links on the page so the feature still does
something useful.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Fooby's FR landing page — French is hard-coded per the feature spec.
FOOBY_FR_HOMEPAGE = "https://fooby.ch/fr.html"

# Only accept anchors pointing at recipe detail pages. Categories,
# articles, and login URLs are filtered out.
_RECIPE_URL_RE = re.compile(r"^https?://(?:www\.)?fooby\.ch/fr/recettes/", re.IGNORECASE)

# Heading text that marks the weekly-inspiration block. Tolerant to
# singular/plural and the occasional capitalization tweak.
_INSPIRATION_RE = re.compile(r"inspirations?\s+de\s+la\s+semaine", re.IGNORECASE)


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
        lambda tag: tag.name in {"h1", "h2", "h3", "h4", "h5"}
        and tag.get_text(strip=True)
        and _INSPIRATION_RE.search(tag.get_text(strip=True))
    )
    if heading is None:
        return []
    container = _enclosing_section(heading)
    if container is None:
        return []
    return _extract_recipe_links(container, base_url)


def _enclosing_section(node):
    """Walk up to the nearest semantic wrapper (`section`/`article`/`main`).

    Bounded to six hops — beyond that we're probably climbing out of the
    block entirely. Falls back to the heading's direct parent when no
    semantic ancestor exists.
    """
    parent = node.parent
    for _ in range(6):
        if parent is None:
            return node.parent
        if parent.name in {"section", "article", "main"}:
            return parent
        parent = parent.parent
    return node.parent


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
