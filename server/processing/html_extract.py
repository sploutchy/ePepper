"""HTML preprocessing for the URL → recipe LLM fallback.

Two outputs:
  1. A pre-extracted recipe dict, when the page already carries a clean
     schema.org Recipe in an embedded `<script type="application/ld+json">`
     blob. This is a frequent free win — many sites that defeat
     recipe-scrapers (because of unusual CSS or HTML structure) still
     publish valid JSON-LD. Returning it here means we burn zero LLM
     tokens on that recipe.
  2. A compact plain-text representation of the recipe-bearing region of
     the page, suitable to paste into a chat-completions request. The
     goal is squeezing 50-200 KB of raw HTML down to ~3-10 KB of relevant
     text so per-recipe LLM cost stays in the sub-cent range.
"""

import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from processing.jsonld import parse_recipe_jsonld

log = logging.getLogger(__name__)

# Soft cap on the text blob we hand to the LLM. ~30 K chars ≈ 7.5 K
# tokens. Hitting this on a real recipe is a signal that either the
# preprocessor needs help (a site-specific rule) or recipe-scrapers
# should grow a plugin for the site.
_TEXT_CHAR_CAP = 30_000

# Tags that never carry recipe content — strip wholesale before any
# other processing. Saves both bytes and false-positive content-root
# picks (e.g. a sidebar `<aside>` that happens to be larger than the
# real `<main>`).
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "picture",
    "video",
    "audio",
    "button",
    "form",
    "nav",
    "footer",
    "header",
    "aside",
    "template",
)

_DROP_ROLES = {"navigation", "banner", "contentinfo", "complementary"}

# Tags that introduce a line break in the text representation. Everything
# else stays inline so the output reads as flowing prose, not a list of
# isolated words.
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "tr", "br",
    "ul", "ol", "dl", "dt", "dd",
}


def extract(html: str) -> tuple[dict, str] | tuple[None, str]:
    """Preprocess `html` into either a parsed Recipe dict or a text blob.

    Return shape:
      (recipe_dict, source_url) — when embedded JSON-LD carries a Recipe.
        Same shape as parse_recipe_jsonld returns.
      (None, text_blob) — when no embedded recipe was found; the text is
        ready to send to the LLM.

    Never raises on malformed HTML — BeautifulSoup is permissive and
    every failure mode falls through to the text-blob path.
    """
    soup = BeautifulSoup(html, "html.parser")

    recipe = _try_embedded_jsonld(soup)
    if recipe is not None:
        return recipe

    return None, _to_text(soup)


def to_text(html: str) -> str:
    """LLM-ready text blob, ignoring any embedded JSON-LD recipe.

    `extract()` short-circuits to the JSON-LD path as soon as it finds a
    Recipe blob, even one too sparse to use (e.g. bio-mio.ch ships a
    Recipe with ingredients but no instructions). The LLM fallback still
    needs the page text in that case, so it calls this helper instead
    of unpacking `extract()`.
    """
    soup = BeautifulSoup(html, "html.parser")
    return _to_text(soup)


def _try_embedded_jsonld(soup: BeautifulSoup) -> tuple[dict, str] | None:
    """Look for a schema.org Recipe in any `<script type="application/ld+json">`.

    Walks every JSON-LD script in document order; the first one that
    parses and contains a Recipe wins. Tolerant of arrays, `@graph`
    documents, and the usual nesting mistakes — `parse_recipe_jsonld`
    handles those.
    """
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            # A surprising number of sites embed unescaped HTML inside
            # the JSON-LD; try a permissive cleanup before giving up.
            cleaned = text.replace("\r", " ").replace("\n", " ")
            try:
                data = json.loads(cleaned)
            except ValueError:
                continue
        parsed = parse_recipe_jsonld(data)
        if parsed is not None:
            log.info("Embedded JSON-LD Recipe found — skipping LLM call")
            return parsed
    return None


def _to_text(soup: BeautifulSoup) -> str:
    """Strip chrome, find the recipe-bearing region, return compact text.

    Order matters: drop unwanted tags before picking the content root,
    so a sidebar or footer can't be misidentified as the largest text
    block.
    """
    # Capture <title> + meta description before we touch <head>; they
    # often carry servings/time/source name and cost almost nothing.
    preamble = _preamble(soup)

    head = soup.find("head")
    if head is not None:
        head.decompose()

    for name in _DROP_TAGS:
        for el in soup.find_all(name):
            el.decompose()

    for el in soup.find_all(attrs={"role": True}):
        if el.get("role", "").lower() in _DROP_ROLES:
            el.decompose()

    root = _pick_content_root(soup)
    text = _render_text(root)
    text = _collapse(text)

    if preamble:
        text = f"{preamble}\n\n{text}"

    if len(text) > _TEXT_CHAR_CAP:
        log.warning(
            "Preprocessed HTML exceeded cap: %d chars → truncated to %d",
            len(text), _TEXT_CHAR_CAP,
        )
        text = text[:_TEXT_CHAR_CAP] + "\n…[truncated]"

    return text


def _preamble(soup: BeautifulSoup) -> str:
    """Build a 1-3 line preamble from `<title>` + the og: / description metas.

    Recipe pages usually summarise themselves here, so it's cheap context
    to keep even when the main body is large.
    """
    lines: list[str] = []
    title = soup.find("title")
    if title and title.get_text(strip=True):
        lines.append(f"Title: {title.get_text(strip=True)}")
    for prop in ("og:title", "og:description"):
        m = soup.find("meta", attrs={"property": prop})
        if m and m.get("content"):
            lines.append(f"{prop}: {m['content'].strip()}")
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        lines.append(f"description: {desc['content'].strip()}")
    return "\n".join(lines)


def _pick_content_root(soup: BeautifulSoup) -> Tag:
    """Pick the subtree most likely to contain the recipe.

    Order of preference:
      1. <main>
      2. <article>
      3. The descendant <div> with the largest text content. (A long
         recipe is usually the dominant block on the page after chrome
         has been stripped.)
      4. <body>, or the soup itself if no body.
    """
    main = soup.find("main")
    if main is not None and main.get_text(strip=True):
        return main

    article = soup.find("article")
    if article is not None and article.get_text(strip=True):
        return article

    body = soup.find("body") or soup
    best: Tag | None = None
    best_len = 0
    for div in body.find_all("div", recursive=True):
        text_len = len(div.get_text(" ", strip=True))
        if text_len > best_len:
            best_len = text_len
            best = div
    body_text_len = len(body.get_text(" ", strip=True))
    # Only swap to a sub-div when it carries ≥ 60 % of the body's text —
    # otherwise we risk cropping out the recipe's instructions list.
    if best is not None and best_len >= body_text_len * 0.6:
        return best
    return body  # type: ignore[return-value]


def _render_text(root: Tag) -> str:
    """Walk `root`, emitting text with structural newlines on block tags."""
    parts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, NavigableString):
            text = str(node)
            if text:
                parts.append(text)
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower() if node.name else ""
        if name == "br":
            parts.append("\n")
            return
        is_block = name in _BLOCK_TAGS
        if is_block:
            parts.append("\n")
        for child in node.children:
            visit(child)
        if is_block:
            parts.append("\n")

    visit(root)
    return "".join(parts)


_WS_RUN_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _collapse(text: str) -> str:
    """Tighten whitespace without losing paragraph structure."""
    # Normalise per-line whitespace, then drop empty/all-whitespace lines.
    lines = [_WS_RUN_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    # Collapse runs of blank lines (none should remain after the join, but
    # paragraph boundaries from nested blocks can produce them again).
    return _BLANK_LINES_RE.sub("\n\n", text).strip()
