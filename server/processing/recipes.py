"""Recipe URL processing via recipe-scrapers."""

import logging
import re

from recipe_scrapers import scrape_html
import aiohttp

log = logging.getLogger(__name__)


async def process_recipe_url(url: str) -> dict | None:
    """Fetch a URL and extract structured recipe data.

    Returns a dict with title, total_time, servings, ingredients, instructions, lang.
    Instructions is a list of dicts: {"type": "step"|"heading", "text": "..."}
    """
    log.info("Fetching recipe from: %s", url)

    try:
        html = await _fetch_html(url)
        log.info("Fetched %d bytes of HTML", len(html))

        # Try site-specific parser first, fall back to wild_mode (generic schema.org)
        try:
            scraper = scrape_html(html, org_url=url)
        except Exception:
            log.info("No site-specific parser, trying wild_mode for %s", url)
            scraper = scrape_html(html, org_url=url, wild_mode=True)

        raw_instructions = scraper.instructions()
        log.info("Raw instructions:\n%s", raw_instructions)

        lang = _detect_language(url, raw_instructions, html)

        recipe = {
            "title": scraper.title(),
            "total_time": _safe_call(scraper.total_time),
            "servings": _safe_call(scraper.yields),
            "ingredients": scraper.ingredients(),
            "instructions": _clean_instructions(raw_instructions),
            "lang": lang,
        }

        log.info("Parsed recipe: %s (lang=%s)", recipe["title"], lang)
        log.info("  Ingredients (%d): %s", len(recipe["ingredients"]), recipe["ingredients"])
        log.info("  Instructions (%d): %s", len(recipe["instructions"]), recipe["instructions"])
        log.info("  Time: %s, Servings: %s", recipe["total_time"], recipe["servings"])

        return recipe

    except Exception as e:
        log.warning("Failed to parse recipe from %s: %s", url, e)
        return None


async def _fetch_html(url: str) -> str:
    """Fetch HTML content from a URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ePepper/1.0; recipe display)"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            log.info("HTTP %d from %s", resp.status, url)
            resp.raise_for_status()
            return await resp.text()


def _safe_call(fn):
    """Call a scraper method, return None on failure."""
    try:
        return fn()
    except Exception:
        return None


# Patterns for numbered step headings that should be stripped entirely
_STEP_HEADING_RE = re.compile(
    r"^("
    r"step\s*\d+"             # Step 1, Step 2
    r"|schritt\s*\d+"         # Schritt 1 (German)
    r"|étape\s*\d+"           # Étape 1 (French)
    r"|fase\s*\d+"            # Fase 1 (Italian)
    r"|stap\s*\d+"            # Stap 1 (Dutch)
    r"|\d+\.\s*schritt"       # 1. Schritt
    r"|\d+\.\s*step"          # 1. Step
    r")\.?\s*$",
    re.IGNORECASE
)


def _is_section_heading(line: str, next_line: str | None) -> bool:
    """Detect if a line is a section heading (e.g. 'Farce', 'Liaison', 'Cuisson').

    Heuristics:
    - Short (1-4 words, under 40 chars)
    - No ending punctuation (no period, comma, etc.)
    - Starts with uppercase
    - Next line exists and is longer (actual instruction text)
    """
    if len(line) > 40:
        return False
    word_count = len(line.split())
    if word_count > 4:
        return False
    if line[-1] in ".,:;!?)":
        return False
    if not line[0].isupper():
        return False
    if next_line and len(next_line) > len(line) * 2:
        return True
    # Also match if it's just 1-2 capitalized words
    if word_count <= 2 and next_line:
        return True
    return False


def _clean_instructions(text: str) -> list[dict]:
    """Split and clean instruction text into structured steps.

    Returns a list of dicts:
        {"type": "step", "text": "..."} — a numbered instruction
        {"type": "heading", "text": "..."} — a section sub-heading (bold, not numbered)

    Removes:
    - Empty lines
    - Numbered step headings like "Step 1", "Schritt 2"
    - Leading step numbers if all lines have them
    """
    if not text:
        return []

    lines = [s.strip() for s in text.split("\n") if s.strip()]

    # Remove lines that are just numbered step headings (Step 1, Schritt 2, etc.)
    lines = [line for line in lines if not _STEP_HEADING_RE.match(line)]

    # Strip leading numbers if ALL lines have them
    all_numbered = all(re.match(r"^\d+[\.\)\-]\s+", line) for line in lines)
    if all_numbered:
        lines = [re.sub(r"^\d+[\.\)\-]\s+", "", line) for line in lines]

    # Now classify each line as heading or step
    result: list[dict] = []
    for i, line in enumerate(lines):
        next_line = lines[i + 1] if i + 1 < len(lines) else None
        if _is_section_heading(line, next_line):
            result.append({"type": "heading", "text": line})
        else:
            result.append({"type": "step", "text": line})

    return result


def _detect_language(url: str, instructions: str, html: str) -> str:
    """Detect recipe language from URL, HTML lang attr, and content.

    Returns 'de', 'fr', 'it', 'en', etc.
    """
    # 1. Check HTML lang attribute
    lang_match = re.search(r'<html[^>]*\slang=["\']([a-z]{2})', html[:2000], re.IGNORECASE)
    if lang_match:
        lang = lang_match.group(1).lower()
        if lang in ("de", "fr", "it", "en", "es", "nl", "pt"):
            return lang

    # 2. Check URL for language hints
    url_lower = url.lower()
    for pattern, lang in [
        (r"/de/", "de"), (r"/fr/", "fr"), (r"/it/", "it"),
        (r"/en/", "en"), (r"/es/", "es"), (r"/nl/", "nl"),
        (r"\.de/", "de"), (r"\.fr/", "fr"), (r"\.it/", "it"),
        (r"\.ch/de", "de"), (r"\.ch/fr", "fr"), (r"\.ch/it", "it"),
    ]:
        if re.search(pattern, url_lower):
            return lang

    # 3. Content heuristics — check for common words
    text = instructions.lower()
    scores = {
        "de": sum(1 for w in ["und", "mit", "den", "die", "das", "bei", "ein", "auf", "schälen", "schneiden", "kochen", "mischen", "minuten", "zugeben"] if w in text),
        "fr": sum(1 for w in ["les", "dans", "avec", "une", "sur", "des", "puis", "ajouter", "couper", "mélanger", "cuire", "minutes", "faire"] if w in text),
        "it": sum(1 for w in ["con", "nel", "una", "gli", "per", "aggiungere", "tagliare", "cuocere", "minuti", "mescolare", "olio"] if w in text),
        "en": sum(1 for w in ["the", "and", "with", "into", "add", "cook", "stir", "heat", "minutes", "until", "mix", "cut"] if w in text),
    }
    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best

    return "en"  # default
